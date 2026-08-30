from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


# ---------------- agent.py ----------------
p = Path("agent.py")
a = p.read_text(encoding="utf-8")

# Explicit age/profile facts in a sentence with a real task must not short-circuit
# an already-active eligibility clarify before the new task can be routed.
a = replace_once(
    a,
    '''        if explicit_age is not None:\n            has_task_request = bool(re.search(\n                r"정책|추천|자격|설명|알려|가능|신청|지원|봐\\s*줘|찾아",\n                answer,\n            ))\n            if has_task_request and not state.get("active_clarify"):\n                update_profile(state, {"age": explicit_age})\n            else:\n                response = resume_after_age_update(\n                    state, explicit_age, collection, bundles, profile_text=answer\n                )\n                state["messages"].append({"role": "assistant", "content": response})\n                return state, response\n''',
    '''        if explicit_age is not None:\n            has_task_request = bool(re.search(\n                r"정책|추천|자격|설명|알려|가능|신청|지원|봐\\s*줘|찾아",\n                answer,\n            ))\n            if has_task_request:\n                # 새 Task가 명시된 문장에서는 나이/Profile 사실만 먼저 저장하고\n                # 아래 Action/Task 라우팅을 계속한다. 기존 Clarify가 새 요청을\n                # 가로채지 못하게 하는 것이 핵심이다.\n                update_profile(state, {"age": explicit_age})\n            else:\n                response = resume_after_age_update(\n                    state, explicit_age, collection, bundles, profile_text=answer\n                )\n                state["messages"].append({"role": "assistant", "content": response})\n                return state, response\n''',
    "explicit age should not swallow new task",
)

# Personalized recommendation results always offer a profile-review entry point.
a = replace_once(
    a,
    '''    lines.append("\\n더 자세히 알고 싶은 정책이 있으면 정책명을 말씀해주세요!")\n    \n    return "\\n".join(lines)\n\n\n# === ELIGIBILITY ===\n''',
    '''    lines.append("\\n더 자세히 알고 싶은 정책이 있으면 정책명을 말씀해주세요!")\n    lines.append("\\n[ACTION_BTN:RESET_RECOMMEND_PROFILE:프로필 다시 설정하기]")\n    \n    return "\\n".join(lines)\n\n\n# === ELIGIBILITY ===\n''',
    "recommend profile reset action",
)

# Give the frontend the canonical selected policy name with every additional batch.
a = replace_once(
    a,
    '''        state["_active_additional_q"] = {\n            "policy_id": policy_id,\n            "questions": [\n''',
    '''        state["_active_additional_q"] = {\n            "policy_id": policy_id,\n            "policy_name": bundle["policy_name"],\n            "questions": [\n''',
    "additional question policy context",
)

p.write_text(a, encoding="utf-8")


# ---------------- server.py ----------------
p = Path("server.py")
s = p.read_text(encoding="utf-8")

# Expose the selected eligibility policy name to every card state.
s = replace_once(
    s,
    '''def public_state(state):\n    return {\n        "focus_policy_id": state.get("focus_policy_id"),\n''',
    '''def public_state(state):\n    eligibility_policy_id = (\n        state.get("_eligibility_policy_id")\n        or state.get("selected_policy_id")\n        or state.get("current_policy_id")\n    )\n    eligibility_policy_name = next(\n        (\n            bundle.get("policy_name") for bundle in bundles\n            if bundle.get("policy_id") == eligibility_policy_id\n        ),\n        None,\n    )\n    return {\n        "focus_policy_id": state.get("focus_policy_id"),\n''',
    "public eligibility policy name setup",
)
s = replace_once(
    s,
    '''        "eligibility_policy_id": state.get("_eligibility_policy_id"),\n        "eligibility_profile_fields": state.get("_eligibility_profile_fields", []),\n''',
    '''        "eligibility_policy_id": state.get("_eligibility_policy_id"),\n        "eligibility_policy_name": eligibility_policy_name,\n        "eligibility_profile_fields": state.get("_eligibility_profile_fields", []),\n''',
    "public eligibility policy name field",
)

# A new eligibility unit reuses the saved common Profile. Only genuinely missing
# fields should show a Profile card; sufficient Profile goes straight to policy-specific questions.
old_block = '''        # 새 정책의 자격조회는 기존 공통 Profile 값을 기억하되 카드 자체는\n        # 항상 다시 확인한다. 필요한 필드를 잠시 비워 Workflow를 멈춘 뒤,\n        # 응답 직전에 원래 값을 복원하여 프론트 카드의 prefill로 사용한다.\n        eligibility_profile_snapshot = None\n        eligibility_needed_fields = []\n        if isinstance(input_action, dict) and not profile_data and not ui_event:\n            action_kind = input_action.get("action")\n            action_tasks = input_action.get("tasks") or []\n            target_policy = _resolve_action_policy_id(input_action, message)\n            is_new_eligibility_unit = (\n                "ELIGIBILITY" in action_tasks\n                and target_policy\n                and (\n                    action_kind in {"NORMAL", "SHOW_ALTERNATIVES"}\n                    or input_action.get("use_previous_context") is not True\n                )\n            )\n            if is_new_eligibility_unit:\n                target_bundle = _eligibility_bundle(target_policy)\n                if target_bundle:\n                    input_action = dict(input_action)\n                    input_action["policy_id"] = target_policy\n                    eligibility_needed_fields = get_policy_needed_fields(target_bundle)\n                    eligibility_profile_snapshot = {\n                        field: state.get("profile", {}).get(field)\n                        for field in eligibility_needed_fields\n                    }\n                    for field in eligibility_needed_fields:\n                        if field in state.get("profile", {}):\n                            state["profile"][field] = None\n                    state["profile_status"] = get_profile_status(state["profile"])\n                    # 새 자격조회 단위는 그 정책의 과거 추가질문 답변을 재사용하지 않는다.\n                    state.setdefault("policy_answers", {}).pop(target_policy, None)\n'''
new_block = '''        # 새 정책 자격조회는 세션 공통 Profile을 그대로 재사용한다.\n        # 필요한 기본 Profile이 이미 있으면 곧바로 정책별 Additional Question으로\n        # 진행하고, 실제로 비어 있는 필드만 Agent가 CLARIFY_PROFILE로 요청한다.\n        if isinstance(input_action, dict) and not profile_data and not ui_event:\n            action_kind = input_action.get("action")\n            action_tasks = input_action.get("tasks") or []\n            target_policy = _resolve_action_policy_id(input_action, message)\n            is_new_eligibility_unit = (\n                "ELIGIBILITY" in action_tasks\n                and target_policy\n                and (\n                    action_kind in {"NORMAL", "SHOW_ALTERNATIVES"}\n                    or input_action.get("use_previous_context") is not True\n                )\n            )\n            if is_new_eligibility_unit:\n                target_bundle = _eligibility_bundle(target_policy)\n                if target_bundle:\n                    input_action = dict(input_action)\n                    input_action["policy_id"] = target_policy\n                    # 새 자격조회 단위에서는 해당 정책의 과거 Additional 답변만 초기화한다.\n                    # 공통 Profile은 보존하여 버튼/자연어 진입 결과를 동일하게 만든다.\n                    state.setdefault("policy_answers", {}).pop(target_policy, None)\n'''
s = replace_once(s, old_block, new_block, "reuse profile for new eligibility")

old_call = '''        try:\n            async with agent_slots:\n                state, response = await run_in_threadpool(\n                    handle_turn,\n                    state,\n                    message,\n                    collection,\n                    bundles,\n                    ui_event,\n                    input_action,\n                )\n        finally:\n            # 새 자격조회 시작 전에 잠시 비웠던 공통 Profile은 항상 복원한다.\n            # Workflow는 CLARIFY_PROFILE에서 멈춰 있으므로 사용자는 값을 확인/수정한 뒤 제출한다.\n            if eligibility_profile_snapshot is not None:\n                for field, value in eligibility_profile_snapshot.items():\n                    if field in state.get("profile", {}):\n                        state["profile"][field] = value\n                state["profile_status"] = get_profile_status(state["profile"])\n                if (\n                    state.get("active_clarify") == "CLARIFY_PROFILE"\n                    and "ELIGIBILITY" in state.get("pending_tasks", [])\n                ):\n                    # Agent의 3개 단위 배치 제한 대신 이 정책이 실제 사용하는\n                    # 기본 Profile 필드를 한 카드에 모두 보여준다.\n                    state["_missing_fields"] = list(eligibility_needed_fields)\n'''
new_call = '''        async with agent_slots:\n            state, response = await run_in_threadpool(\n                handle_turn,\n                state,\n                message,\n                collection,\n                bundles,\n                ui_event,\n                input_action,\n            )\n'''
s = replace_once(s, old_call, new_call, "remove eligibility profile snapshot restore")

# Eligibility policy dropdown must contain exactly application-capable policies.
s = replace_once(
    s,
    '''    return JSONResponse({\n        "policies": all_policies,\n        "eligibility_policies": [\n            item for item in all_policies\n            if item["policy_id"].startswith("NYJ-YOUTH-")\n        ],\n        "explain_only": info_only,\n    })\n''',
    '''    eligibility_ids = {\n        bundle.get("policy_id") for bundle in bundles\n        if bundle.get("eligibility_mode") != "INFO_ONLY"\n    }\n    return JSONResponse({\n        "policies": all_policies,\n        "eligibility_policies": [\n            item for item in all_policies\n            if item["policy_id"] in eligibility_ids\n        ],\n        "explain_only": info_only,\n    })\n''',
    "eligibility list must be 25 application policies",
)

p.write_text(s, encoding="utf-8")


# ---------------- static/index.html ----------------
p = Path("static/index.html")
h = p.read_text(encoding="utf-8")

# Recommendation result action button.
h = replace_once(
    h,
    '''    html = html.replace(/\\[ACTION_BTN:RESET_CHAT:([^\\]]+)\\]/g, '<button class="elig-btn" onclick="resetChat()">🔄 $1</button>');\n''',
    '''    html = html.replace(/\\[ACTION_BTN:RESET_CHAT:([^\\]]+)\\]/g, '<button class="elig-btn" onclick="resetChat()">🔄 $1</button>');\n    html = html.replace(/\\[ACTION_BTN:RESET_RECOMMEND_PROFILE:([^\\]]+)\\]/g, '<button class="elig-btn" onclick="reopenRecommendProfile(this)">👤 $1</button>');\n''',
    "recommend reset action parser",
)

# Make the selected interest visible on the Profile confirmation card.
h = replace_once(
    h,
    '''    const ageVal = pcSelections.age || '';\n    let html = `\n        <div class="chat-card-title">🎯 추천 조건 확인</div>\n''',
    '''    const ageVal = pcSelections.age || '';\n    const interestLabel = pendingInterest && pendingInterest.trim() ? pendingInterest.trim() : '전체';\n    let html = `\n        <div class="chat-card-title">🎯 추천 조건 확인</div>\n        <div style="font-size:13px;color:#475569;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:9px 11px;margin-bottom:12px;">\n            📌 관심 분야: <strong>${escapeHtml(interestLabel)}</strong>\n        </div>\n''',
    "recommend card interest context",
)

# Reopen saved Profile after a personalized recommendation without losing interest.
insert_marker = '''async function exploreWithoutProfile(btn) {\n'''
insert_fn = '''function reopenRecommendProfile(btn) {\n    if (btn) btn.disabled = true;\n    deactivateAllCards();\n    addBotMessage('현재 관심 분야는 유지하고 추천 조건을 다시 확인해주세요. 기존 프로필 값이 미리 선택되어 있어요.');\n    showRecommendProfileCard();\n}\n\n'''
h = replace_once(h, insert_marker, insert_fn + insert_marker, "reopen recommendation profile")

# Track selected eligibility policy name in the shared renderer.
h = replace_once(
    h,
    '''    if (st.eligibility_policy_id) {\n        lastEligibilityPolicyId = st.eligibility_policy_id;\n    }\n''',
    '''    if (st.eligibility_policy_id) {\n        lastEligibilityPolicyId = st.eligibility_policy_id;\n    }\n    if (st.eligibility_policy_name) {\n        lastEligibilityPolicyName = st.eligibility_policy_name;\n    }\n''',
    "track eligibility policy name",
)

# Eligibility Profile cards should also show which policy is currently fixed.
h = replace_once(
    h,
    '''    card.innerHTML = `\n        <div class="chat-card-title">${title}</div>\n        ${fieldsHtml}\n''',
    '''    const eligibilityContext = isPartial && lastEligibilityPolicyName\n        ? `<div style="font-size:13px;color:#475569;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:9px 11px;margin-bottom:12px;">✅ 선택 정책: <strong>${escapeHtml(lastEligibilityPolicyName)}</strong></div>`\n        : '';\n\n    card.innerHTML = `\n        <div class="chat-card-title">${title}</div>\n        ${eligibilityContext}\n        ${fieldsHtml}\n''',
    "eligibility profile policy context",
)

# Additional question card: selected policy context + profile reset + policy switch.
h = replace_once(
    h,
    '''function showAdditionalQuestionCard(aq) {\n    aqSelections = {};\n    currentAqPolicyId = aq.policy_id || null;\n    const container = document.getElementById('chatMessages');\n''',
    '''function showAdditionalQuestionCard(aq) {\n    aqSelections = {};\n    currentAqPolicyId = aq.policy_id || null;\n    const policyName = aq.policy_name || lastEligibilityPolicyName || '';\n    if (policyName) lastEligibilityPolicyName = policyName;\n    const container = document.getElementById('chatMessages');\n''',
    "additional selected policy setup",
)
h = replace_once(
    h,
    '''    card.innerHTML = `\n        <div class="chat-card-title">📝 추가 자격 조건 확인</div>\n        <div style="font-size:13px;color:#64748b;margin:2px 0 12px;">\n''',
    '''    const policyContext = policyName\n        ? `<div style="font-size:13px;color:#334155;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px 11px;margin-bottom:10px;">✅ 선택 정책: <strong>${escapeHtml(policyName)}</strong></div>`\n        : '';\n    card.innerHTML = `\n        <div class="chat-card-title">📝 추가 자격 조건 확인</div>\n        ${policyContext}\n        <div style="display:flex;gap:8px;margin-bottom:12px;">\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="resetAndShowFullCard('${aq.policy_id || ''}', this)">프로필 다시 설정하기</button>\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="startEligibility()">다른 정책 선택</button>\n        </div>\n        <div style="font-size:13px;color:#64748b;margin:2px 0 12px;">\n''',
    "additional card context actions",
)

# Keep name alongside id/fields.
h = replace_once(
    h,
    '''let lastEligibilityPolicyId = null;\nlet lastEligibilityFields = [];\nlet profileResumePolicyId = null;\n''',
    '''let lastEligibilityPolicyId = null;\nlet lastEligibilityPolicyName = '';\nlet lastEligibilityFields = [];\nlet profileResumePolicyId = null;\n''',
    "eligibility policy name global",
)

# Reset name on full chat reset UI command.
h = replace_once(
    h,
    '''            lastEligibilityPolicyId = null;\n            lastEligibilityFields = [];\n            profileResumePolicyId = null;\n''',
    '''            lastEligibilityPolicyId = null;\n            lastEligibilityPolicyName = '';\n            lastEligibilityFields = [];\n            profileResumePolicyId = null;\n''',
    "reset eligibility policy name",
)

p.write_text(h, encoding="utf-8")


# ---------------- README.md ----------------
p = Path("README.md")
r = p.read_text(encoding="utf-8")
r = r.replace(
    '자연어의 새 `NORMAL + RECOMMEND` 요청도 관심 분야가 이미 명시된 경우에는 관심 분야 선택 단계를 생략할 수 있지만, **일반 맞춤 추천은 최종 결과 전에 추천 조건 카드를 보여주어 현재 Profile을 다시 확인할 수 있게 합니다.**',
    '자연어의 새 `NORMAL + RECOMMEND` 요청에서 `취업`, `주거`처럼 관심 분야가 이미 명시되면 관심 분야 선택 카드는 생략하고, **해당 분야를 표시한 추천 조건 카드에서 저장 Profile을 확인한 뒤 추천**합니다. 분야가 없는 `맞춤추천`만 관심 분야 카드부터 시작합니다.'
)
r = r.replace(
    '- `[이 조건으로 추천받기]` → 현재 카드의 Profile을 저장·사용해 맞춤 추천\n- `[조건 없이 찾아보기]` → Profile은 저장해 둔 채 **이번 추천에서만 Profile을 무시**하고 정책 자체를 탐색\n',
    '- `[이 조건으로 추천받기]` → 현재 카드의 Profile을 저장·사용해 맞춤 추천\n- `[조건 없이 찾아보기]` → Profile은 저장해 둔 채 **이번 추천에서만 Profile을 무시**하고 정책 자체를 탐색\n- 추천 결과의 `[프로필 다시 설정하기]` → 관심 분야는 유지하고 저장 Profile이 prefill된 추천 조건 카드로 복귀\n'
)
r = r.replace(
    '정책마다 실제 판정에 필요한 필드만 요청합니다. 기본 조건에서 명확한 불충족이 확인되면 규칙 결과를 우선하며, LLM이 이를 뒤집지 않습니다.\n',
    '정책마다 실제 판정에 필요한 필드만 요청합니다. **새 정책을 선택해도 공통 Profile은 재사용**하므로 필요한 값이 이미 있으면 Profile 카드를 반복하지 않고 정책별 추가 자격 질문으로 바로 진행합니다. 값이 부족할 때만 누락된 기본 Profile을 요청합니다. 추가 질문 카드에는 현재 선택 정책과 `[프로필 다시 설정하기]`, `[다른 정책 선택]`을 함께 표시합니다. 기본 조건에서 명확한 불충족이 확인되면 규칙 결과를 우선하며, LLM이 이를 뒤집지 않습니다.\n'
)
r = r.replace(
    '- 이전 대화 Context는 필요할 때만 재사용하고, **현재 사용자가 명시한 정책·분야를 우선**한다.\n',
    '- 이전 대화 Context는 필요할 때만 재사용하고, **현재 사용자가 명시한 새 Task·정책·분야를 진행 중 Clarify보다 우선**한다. 예를 들어 자격 추가질문 중 `만 25세야 ... 취업 정책 추천해줘`라고 하면 Profile 사실을 저장한 뒤 기존 자격 질문을 닫고 취업 추천으로 전환한다.\n'
)
p.write_text(r, encoding="utf-8")

print("final card routing UX patch applied")
