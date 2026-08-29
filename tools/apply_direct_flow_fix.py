from pathlib import Path
import re


def read(path):
    return Path(path).read_text(encoding="utf-8")


def write(path, text):
    Path(path).write_text(text, encoding="utf-8", newline="")


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


def replace_count(text, old, new, expected, label):
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} occurrences, found {count}")
    return text.replace(old, new)


# ---------------- agent.py ----------------
path = "agent.py"
a = read(path)

# Exact named-policy eligibility requests must resolve before generic navigation/follow-up logic.
a = replace_once(
    a,
    '    # 특정 공식 정책명은 일반 분야 검색보다 우선한다.\n    exact_policy = resolve_policy_alias(msg) or _contains_exact_policy_name(bundles, msg)\n\n',
    '    # 특정 공식 정책명은 일반 분야 검색보다 우선한다.\n    exact_policy = resolve_policy_alias(msg) or _contains_exact_policy_name(bundles, msg)\n\n'
    '    # "청년꽃간 자격조회하자", "월세 자격 확인해줘"처럼 현재 발화에\n'
    '    # 정책 대상과 자격 의도가 함께 있으면 직전 focus보다 이 정책을 우선한다.\n'
    '    if exact_policy and _is_explicit_eligibility_request(msg):\n'
    '        explicit_policy_id = resolve_policy_alias(msg) or _explicit_policy_id_from_name(msg)\n'
    '        if explicit_policy_id:\n'
    '            return validate_action_payload({\n'
    '                "action": "NORMAL",\n'
    '                "tasks": ["ELIGIBILITY"],\n'
    '                "policy_id": explicit_policy_id,\n'
    '                "policy_mention": _policy_name_for_id(explicit_policy_id, bundles),\n'
    '                "use_previous_context": False,\n'
    '                "confidence": "high",\n'
    '            })\n\n',
    "insert exact eligibility navigation",
)

# SHOW_ALTERNATIVES should preserve Profile unless condition-less is explicit.
a = replace_once(
    a,
    '            state["interest_query"] = next_topic or "전체"\n'
    '            state["current_topic"] = next_topic or "전체"\n'
    '            state["_skip_profile_check"] = True\n'
    '            state["_explore_mode"] = True\n',
    '            state["interest_query"] = next_topic or "전체"\n'
    '            state["current_topic"] = next_topic or "전체"\n'
    '            if action.get("explore_without_profile"):\n'
    '                state["_skip_profile_check"] = True\n'
    '                state["_explore_mode"] = True\n',
    "SHOW_ALTERNATIVES profile preservation",
)

# CHANGE_TOPIC should preserve Profile and ask for confirmation card.
a = replace_once(
    a,
    '        if action.get("exclude_topics"):\n'
    '            state["_exclude_topics"] = action["exclude_topics"]\n'
    '        state["_skip_profile_check"] = True\n'
    '        state["_explore_mode"] = True\n'
    '        return ["RECOMMEND"], ([] if topic else ["CLARIFY_PREFERENCE"]), None\n',
    '        if action.get("exclude_topics"):\n'
    '            state["_exclude_topics"] = action["exclude_topics"]\n'
    '        if action.get("explore_without_profile"):\n'
    '            state["_skip_profile_check"] = True\n'
    '            state["_explore_mode"] = True\n'
    '        return ["RECOMMEND"], ([] if topic else ["CLARIFY_PREFERENCE"]), None\n',
    "CHANGE_TOPIC profile preservation",
)

# Structured recommendation-card submission is the only confirmation signal.
a = replace_once(
    a,
    '    # 메시지 기록\n'
    '    if user_message and user_message.strip():\n'
    '        state["messages"].append({"role": "user", "content": user_message.strip()})\n\n',
    '    # 메시지 기록\n'
    '    if user_message and user_message.strip():\n'
    '        state["messages"].append({"role": "user", "content": user_message.strip()})\n\n'
    '    # 추천 조건 카드를 사용자가 직접 제출한 턴만 최종 추천 실행을 허용한다.\n'
    '    # 이 값은 reset_task_context의 작업 상태 초기화를 지나 run_recommend에서 1회 소비된다.\n'
    '    if ui_event == "SUBMIT_RECOMMEND_PROFILE":\n'
    '        state["_recommend_profile_submitted"] = True\n\n',
    "recommend card submission marker",
)

# Deterministic natural-language Profile facts accumulate on every normal turn.
a = replace_once(
    a,
    '        answer = (user_message or "").strip()\n'
    '        pending_age = state.get("_pending_age_confirmation")\n'
    '        explicit_age = extract_explicit_age(answer)\n',
    '        answer = (user_message or "").strip()\n'
    '        # Prompt A 결과와 별개로 사용자가 명시한 Profile 사실은 항상 공통 State에 누적한다.\n'
    '        # 예: "만 25세야 남양주 살아 미취업이야"를 한 턴에 모두 보존한다.\n'
    '        explicit_profile_patch = _normalize_profile_patch(extract_profile_patch_from_text(answer))\n'
    '        if explicit_profile_patch:\n'
    '            update_profile(state, explicit_profile_patch)\n'
    '        pending_age = state.get("_pending_age_confirmation")\n'
    '        explicit_age = extract_explicit_age(answer)\n',
    "natural profile accumulation",
)

# Canonical Profile enums must match config/UI values.
a = replace_once(
    a,
    '    enums = {\n'
    '        "residency": {"예", "아니오"},\n'
    '        "employment": {"취업", "미취업"},\n'
    '        "student": {"대학생", "고등학생", "대학원생", "아니오"},\n'
    '        "startup": {"창업 중", "창업 준비 중", "창업하지 않음"},\n'
    '        "housing": {"무주택", "유주택"},\n'
    '        "marriage": {"미혼", "기혼"},\n'
    '    }\n'
    '    aliases = {\n'
    '        "네": "예", "아니요": "아니오", "거주": "예", "비거주": "아니오",\n'
    '        "재직": "취업", "구직": "미취업", "취준": "미취업",\n'
    '        "학생 아님": "아니오", "비학생": "아니오",\n'
    '        "예비창업": "창업 준비 중", "미창업": "창업하지 않음",\n'
    '        "자가": "유주택", "기혼자": "기혼", "미혼자": "미혼",\n'
    '    }\n',
    '    enums = {\n'
    '        "residency": {"예", "아니오"},\n'
    '        "employment": {"취업", "미취업"},\n'
    '        "student": {"대학생", "고등학생", "대학원생", "해당하지 않음"},\n'
    '        "startup": {"창업 중", "창업 준비 중", "창업하지 않음"},\n'
    '        "housing": {"무주택", "주택 소유"},\n'
    '        "marriage": {"미혼", "기혼"},\n'
    '    }\n'
    '    aliases = {\n'
    '        "네": "예", "아니요": "아니오", "거주": "예", "비거주": "아니오",\n'
    '        "재직": "취업", "구직": "미취업", "취준": "미취업",\n'
    '        "학생 아님": "해당하지 않음", "비학생": "해당하지 않음", "아니오": "해당하지 않음",\n'
    '        "예비창업": "창업 준비 중", "미창업": "창업하지 않음",\n'
    '        "자가": "주택 소유", "유주택": "주택 소유", "기혼자": "기혼", "미혼자": "미혼",\n'
    '    }\n',
    "canonical profile enums",
)

a = replace_once(
    a,
    '    elif re.search(r"학생\\s*(?:이\\s*)?(?:아니|아님)|비학생", value):\n'
    '        patch["student"] = "아니오"\n',
    '    elif re.search(r"학생\\s*(?:이\\s*)?(?:아니|아님)|비학생", value):\n'
    '        patch["student"] = "해당하지 않음"\n',
    "natural student canonical value",
)

a = replace_once(
    a,
    '    elif re.search(r"유주택|자가\\s*(?:있|보유)|주택\\s*보유", value):\n'
    '        patch["housing"] = "유주택"\n',
    '    elif re.search(r"유주택|자가\\s*(?:있|보유)|주택\\s*보유|집(?:이|은)?\\s*있", value):\n'
    '        patch["housing"] = "주택 소유"\n',
    "natural housing canonical value",
)

# Prompt A profile patch is normalized and merged with explicit deterministic facts.
a = replace_once(
    a,
    '    patch = result.get("profile_patch", {})\n'
    '    if isinstance(patch, dict):\n'
    '        clean_patch = {key: value for key, value in patch.items() if value is not None}\n'
    '        if clean_patch:\n'
    '            update_profile(state, clean_patch)\n',
    '    patch = _normalize_profile_patch(result.get("profile_patch", {}))\n'
    '    explicit_patch = _normalize_profile_patch(extract_profile_patch_from_text(user_message))\n'
    '    patch.update(explicit_patch)  # 사용자가 명시한 값이 모델 추정보다 우선\n'
    '    if patch:\n'
    '        update_profile(state, patch)\n',
    "normalize Prompt A profile patch",
)

# Every non-explore recommendation pauses on the prefilled Profile card until UI confirmation.
a = replace_once(
    a,
    '    # "조건 없이" 탐색 모드 여부\n'
    '    was_skip_mode = state.get("_explore_mode", False)\n'
    '    state["_explore_mode"] = False\n'
    '    \n'
    '    # "조건 없이"가 아닌 경우에만 Profile 체크\n'
    '    skip_profile = state.get("_skip_profile_check", False)\n'
    '    if not skip_profile:\n'
    '        if get_profile_status(profile) == "INCOMPLETE":\n'
    '            missing = get_missing_profile_fields(profile)\n'
    '            if len(missing) > 4:\n'
    '                return generate_clarify_question(state, "CLARIFY_PROFILE")\n'
    '    \n'
    '    # 사용 후 플래그 초기화\n'
    '    state["_skip_profile_check"] = False\n',
    '    # "조건 없이"는 Profile을 삭제하지 않고 이번 추천에서만 사용하지 않는다.\n'
    '    was_skip_mode = bool(state.get("_explore_mode", False))\n'
    '    state["_explore_mode"] = False\n'
    '    profile_submitted = bool(state.pop("_recommend_profile_submitted", False))\n'
    '    \n'
    '    # 자연어 NORMAL/FOLLOW_UP/CHANGE_TOPIC/SHOW_ALTERNATIVES 모두 결과 전에\n'
    '    # 현재 공통 Profile이 prefill된 추천 조건 카드를 확인한다.\n'
    '    if not was_skip_mode and not profile_submitted:\n'
    '        missing = get_missing_profile_fields(profile)\n'
    '        state["active_clarify"] = "CLARIFY_PROFILE"\n'
    '        state["pending_tasks"] = ["RECOMMEND"]\n'
    '        state["_missing_fields"] = list(missing)\n'
    '        return generate_clarify_question(state, "CLARIFY_PROFILE")\n'
    '    \n'
    '    # 1회성 실행 플래그 정리\n'
    '    state["_skip_profile_check"] = False\n',
    "recommend profile confirmation contract",
)

# Explicit eligibility language helper.
a = replace_once(
    a,
    '\ndef _is_explicit_recommend_request(message):\n',
    '\ndef _is_explicit_eligibility_request(message):\n'
    '    """정책 자격을 직접 조회/확인하려는 자연어인지 판별한다."""\n'
    '    if not message or "자격증" in message:\n'
    '        return False\n'
    '    return bool(re.search(\n'
    '        r"자격\\s*(?:조회|확인|봐|검토|되|돼|있는지)|"\n'
    '        r"가능한지|되는지|신청\\s*(?:할\\s*)?수|지원\\s*(?:받을\\s*)?수|대상인지",\n'
    '        message,\n'
    '    ))\n\n\n'
    'def _is_explicit_recommend_request(message):\n',
    "explicit eligibility helper",
)

# Ensure code-level eligibility detector recognizes 조회.
a = a.replace(
    'r"자격\\s*(?:(?:도|을|이)\\s*)?(?:확인|되|돼|있)|"',
    'r"자격\\s*(?:(?:도|을|이)\\s*)?(?:조회|확인|되|돼|있)|"',
)
a = a.replace(
    'r"자격\\s*(?:(?:도|을|이)\\s*)?(?:확인|봐|검토|되|돼)|가능한지|',
    'r"자격\\s*(?:(?:도|을|이)\\s*)?(?:조회|확인|봐|검토|되|돼)|가능한지|',
)
a = a.replace(
    '"자격 확인해줘", "자격확인해줘", "자격 확인해 줘", "자격이 있는지",',
    '"자격 조회하자", "자격조회하자", "자격 조회해줘", "자격조회해줘",\n    "자격 확인해줘", "자격확인해줘", "자격 확인해 줘", "자격이 있는지",',
)

# GPT receives semantic residency text while canonical State remains 예/아니오.
a = replace_once(
    a,
    '\ndef judge_recommendations_with_ai(candidates, bundles, profile, interest):\n',
    '\ndef _profile_for_ai(profile):\n'
    '    result = dict(profile or {})\n'
    '    if result.get("residency") == "예":\n'
    '        result["residency"] = "남양주시 거주"\n'
    '    elif result.get("residency") == "아니오":\n'
    '        result["residency"] = "남양주시 비거주"\n'
    '    return result\n\n\n'
    'def judge_recommendations_with_ai(candidates, bundles, profile, interest):\n',
    "AI profile serializer",
)
a = replace_once(
    a,
    '        profile=json.dumps(profile, ensure_ascii=False),\n'
    '        bundles_text=json.dumps(compact, ensure_ascii=False),\n',
    '        profile=json.dumps(_profile_for_ai(profile), ensure_ascii=False),\n'
    '        bundles_text=json.dumps(compact, ensure_ascii=False),\n',
    "Prompt C semantic profile",
)

# Rule PASS is authoritative; AI cannot invent a downgrade.
a = replace_once(
    a,
    '    if rule_status == "PASS" and ai_status == "PASS":\n'
    '        merged["eligibility_status"] = "PASS"\n'
    '        merged["matched_conditions"] = list(dict.fromkeys(rule_matched + ai_matched))\n'
    '        merged["explanation"] = (\n'
    '            f"{rule_result.get(\'explanation\', \'\')} "\n'
    '            f"AI 보조 검토도 같은 방향입니다: {reason[:400]}"\n'
    '        ).strip()\n'
    '        return merged\n\n'
    '    # 규칙이 UNKNOWN이거나 규칙과 GPT가 충돌하면 확정하지 않는다.\n',
    '    if rule_status == "PASS":\n'
    '        # 구조화 규칙과 공식 추가질문이 모두 PASS면 AI가 이를 UNKNOWN/FAIL로\n'
    '        # 뒤집지 않는다. AI는 같은 방향의 설명만 보조할 수 있다.\n'
    '        merged["eligibility_status"] = "PASS"\n'
    '        if ai_status == "PASS":\n'
    '            merged["matched_conditions"] = list(dict.fromkeys(rule_matched + ai_matched))\n'
    '        merged["failed_conditions"] = []\n'
    '        merged["missing_conditions"] = list(rule_missing)\n'
    '        merged["explanation"] = rule_result.get("explanation", "")\n'
    '        return merged\n\n'
    '    # 규칙이 UNKNOWN일 때만 AI의 추가 확인 의견을 보조적으로 병합한다.\n',
    "eligibility PASS authority",
)

# Filter a known false conflict and serialize residency semantically for Prompt D.
a = replace_once(
    a,
    '\ndef review_eligibility_with_ai(bundle, profile, existing_answers, rule_result):\n',
    '\ndef _sanitize_ai_eligibility_payload(payload, profile):\n'
    '    if not isinstance(payload, dict):\n'
    '        return payload\n'
    '    cleaned = dict(payload)\n'
    '    if (profile or {}).get("residency") == "예":\n'
    '        for key in ("failed_conditions", "missing_conditions"):\n'
    '            values = cleaned.get(key)\n'
    '            if isinstance(values, list):\n'
    '                cleaned[key] = [\n'
    '                    item for item in values\n'
    '                    if not ("남양주" in str(item) and "거주" in str(item))\n'
    '                ]\n'
    '    return cleaned\n\n\n'
    'def review_eligibility_with_ai(bundle, profile, existing_answers, rule_result):\n',
    "sanitize AI eligibility conflicts",
)
a = replace_once(
    a,
    '        profile=json.dumps(profile, ensure_ascii=False),\n'
    '        existing_answers=json.dumps(existing_answers, ensure_ascii=False),\n',
    '        profile=json.dumps(_profile_for_ai(profile), ensure_ascii=False),\n'
    '        existing_answers=json.dumps(existing_answers, ensure_ascii=False),\n',
    "Prompt D semantic profile",
)
a = replace_once(
    a,
    '    if not raw or raw.startswith("[ERROR]"):\n'
    '        return None\n'
    '    return merge_eligibility_review(rule_result, parse_json_response(raw))\n\n\ndef format_eligibility_response(',
    '    if not raw or raw.startswith("[ERROR]"):\n'
    '        return None\n'
    '    parsed = _sanitize_ai_eligibility_payload(parse_json_response(raw), profile)\n'
    '    return merge_eligibility_review(rule_result, parsed)\n\n\ndef format_eligibility_response(',
    "sanitize Prompt D output",
)

# Weird/unclear natural language always gets the standard three-action fallback.
a = replace_once(
    a,
    '        else:\n'
    '            response = "무엇을 도와드릴까요? 정책 설명, 맞춤 추천, 자격 확인 중 선택해주세요."\n\n'
    '    state["messages"].append({"role": "assistant", "content": response})\n',
    '        else:\n'
    '            response = _intent_clarify_response("NO_TASK")\n\n'
    '    state["messages"].append({"role": "assistant", "content": response})\n',
    "standard fallback actions",
)

write(path, a)


# ---------------- server.py ----------------
path = "server.py"
s = read(path)

# Recommendation transition behavior now lives in agent.py, not a runtime wrapper.
wrapper = '''# 추천의 주제/대안 전환은 공통 Profile을 그대로 재사용한다.\n# 사용자가 명시적으로 '조건 없이/프로필 없이'를 요청한 경우에만 탐색 모드로 둔다.\n_ORIGINAL_APPLY_ACTION_TRANSITION = agent_module.apply_action_transition\n\n\ndef _profile_preserving_action_transition(state, action, bundles, preserve_workflow=False):\n    result = _ORIGINAL_APPLY_ACTION_TRANSITION(\n        state, action, bundles, preserve_workflow=preserve_workflow\n    )\n    if isinstance(action, dict):\n        kind = action.get("action")\n        tasks = action.get("tasks") or []\n        if (\n            kind in {"CHANGE_TOPIC", "SHOW_ALTERNATIVES"}\n            and "RECOMMEND" in tasks\n            and action.get("explore_without_profile") is not True\n        ):\n            state.pop("_skip_profile_check", None)\n            state.pop("_explore_mode", None)\n    return result\n\n\nagent_module.apply_action_transition = _profile_preserving_action_transition\n\n\n'''
s = replace_once(s, wrapper, '', "remove server transition wrapper")

# Menu semantics in the Prompt guidance.
s = replace_once(
    s,
    '- "다른 자격 조회할게", "다른 정책 자격 확인할래", "다른 자격도 볼래"처럼 새 정책의 자격을 고르려는 말은 action=SHOW_ALTERNATIVES, tasks=["ELIGIBILITY"], policy_mention=null, reuse_focus=false, use_previous_context=false로 판단하고 CLARIFY_POLICY가 필요합니다.\n',
    '- "다른 자격 조회할게", "다른 정책 자격 확인할래", "다른 자격도 볼래"처럼 새 정책의 자격을 고르려는 말은 action=SHOW_ALTERNATIVES, tasks=["ELIGIBILITY"], policy_mention=null, reuse_focus=false, use_previous_context=false로 판단하고 CLARIFY_POLICY가 필요합니다.\n'
    '- "정책 알아보자", "맞춤 추천해보자", "자격조회하자"처럼 정책명이 없는 단일 메뉴 발화는 각각 EXPLAIN, RECOMMEND, ELIGIBILITY 메뉴 시작 의미입니다.\n'
    '- "청년꽃간 자격조회하자"처럼 구체적인 정책명이 함께 있으면 메뉴 시작이 아니라 해당 정책의 ELIGIBILITY 요청으로 판단하세요.\n',
    "navigation prompt menu rules",
)

# No external navigation_sync runtime patch; index.html owns UI routing.
s = s.replace("CHAT_SYNC_SCRIPT = '<script src=\"/static/navigation_sync.js\"></script>'\n", "")
s = replace_once(
    s,
    'def _load_chat_html():\n'
    '    with open("static/index.html", "r", encoding="utf-8") as f:\n'
    '        content = f.read()\n'
    '    if CHAT_SYNC_SCRIPT not in content:\n'
    '        content = content.replace("</body>", f"{CHAT_SYNC_SCRIPT}\\n</body>")\n'
    '    return content\n',
    'def _load_chat_html():\n'
    '    with open("static/index.html", "r", encoding="utf-8") as f:\n'
    '        return f.read()\n',
    "inline navigation UI ownership",
)

# Strict standalone menu phrases. Policy-containing sentences do not match these exact forms.
insert = '''\ndef _simple_menu_type(message):\n    compact = re.sub(r"[^0-9a-z가-힣]", "", str(message or "").lower())\n    explain = {\n        "정책알아보자", "정책알아보기", "정책찾아보자", "정책좀보자", "청년정책알아보자",\n    }\n    recommend = {\n        "맞춤추천하자", "맞춤추천해보자", "맞춤추천받자", "맞춤추천받기", "추천받자",\n    }\n    eligibility = {\n        "자격조회하자", "자격조회해보자", "자격확인하자", "자격확인해보자", "자격확인하기",\n    }\n    if compact in explain:\n        return "START_EXPLAIN"\n    if compact in recommend:\n        return "START_RECOMMEND"\n    if compact in eligibility:\n        return "START_ELIGIBILITY"\n    return None\n\n\n'''
s = replace_once(s, '\ndef _is_profile_reset_candidate(message):\n', insert + 'def _is_profile_reset_candidate(message):\n', "simple menu helper")

s = replace_once(
    s,
    'def _is_navigation_prompt_candidate(message):\n'
    '    return (\n'
    '        _is_profile_reset_candidate(message)\n'
    '        or _is_other_eligibility_candidate(message)\n'
    '        or _is_chat_reset_candidate(message)\n'
    '    )\n',
    'def _is_navigation_prompt_candidate(message):\n'
    '    return (\n'
    '        _simple_menu_type(message) is not None\n'
    '        or _is_profile_reset_candidate(message)\n'
    '        or _is_other_eligibility_candidate(message)\n'
    '        or _is_chat_reset_candidate(message)\n'
    '    )\n',
    "navigation candidate menu inclusion",
)

s = replace_once(
    s,
    '    action = inferred_action.get("action") if isinstance(inferred_action, dict) else None\n'
    '    tasks = inferred_action.get("tasks") or [] if isinstance(inferred_action, dict) else []\n\n',
    '    action = inferred_action.get("action") if isinstance(inferred_action, dict) else None\n'
    '    tasks = inferred_action.get("tasks") or [] if isinstance(inferred_action, dict) else []\n\n'
    '    menu_type = _simple_menu_type(message)\n'
    '    if menu_type:\n'
    '        return {"type": menu_type}\n\n',
    "navigation menu command",
)

# Handle all three menu ui_commands safely.
s = replace_once(
    s,
    '                    elif ui_command["type"] == "START_ELIGIBILITY":\n'
    '                        # 자연어 \'다른 자격 조회\'도 버튼처럼 현재 자격 단위를 닫고\n'
    '                        # 새 정책 선택부터 시작한다. 공통 Profile 자체는 유지된다.\n'
    '                        reset_task_context(state)\n'
    '                        response = ""\n'
    '                    else:\n'
    '                        policy_id = ui_command["policy_id"]\n',
    '                    elif ui_command["type"] in {"START_ELIGIBILITY", "START_EXPLAIN", "START_RECOMMEND"}:\n'
    '                        # 단일 메뉴 자연어는 하단 퀵 버튼과 같은 새 작업 단위를 시작한다.\n'
    '                        # 공통 Profile은 reset_task_context에서 삭제되지 않는다.\n'
    '                        reset_task_context(state)\n'
    '                        response = ""\n'
    '                    else:\n'
    '                        policy_id = ui_command["policy_id"]\n',
    "server ui command handling",
)

write(path, s)


# ---------------- static/index.html ----------------
path = "static/index.html"
h = read(path)

# Correct personalized-recommend button flag.
h = replace_once(
    h,
    "                    explore_without_profile: true,\n",
    "                    explore_without_profile: false,\n",
    "recommend profile explore flag",
)

# Explicit condition-less button should send the flag directly.
h = replace_once(
    h,
    "                    topic: pendingInterest,\n                    use_previous_context: false,\n                    confidence: 'high',\n",
    "                    topic: pendingInterest,\n                    use_previous_context: false,\n                    explore_without_profile: true,\n                    confidence: 'high',\n",
    "explicit explore button flag",
)

# Put ui_command routing directly in the main frontend renderer.
h = replace_once(
    h,
    "function renderResponseWithCard(data) {\n    const st = data.state || {};\n",
    "function renderResponseWithCard(data) {\n    const st = data.state || {};\n"
    "    const command = data.ui_command;\n"
    "    if (st.profile) serverProfile = st.profile;\n"
    "\n"
    "    if (command && command.type) {\n"
    "        // sendMessage가 응답 렌더링 중일 때도 퀵 버튼 함수를 즉시 실행할 수 있게 한다.\n"
    "        isWaiting = false;\n"
    "        document.getElementById('sendBtn').disabled = false;\n"
    "        if (command.type === 'START_EXPLAIN') { startExplain(); return; }\n"
    "        if (command.type === 'START_RECOMMEND') { startRecommend(); return; }\n"
    "        if (command.type === 'START_ELIGIBILITY') { startEligibility(); return; }\n"
    "        if (command.type === 'RESET_CHAT') {\n"
    "            document.getElementById('chatMessages').innerHTML = '';\n"
    "            pendingInterest = '';\n"
    "            lastMissingFields = null;\n"
    "            lastEligibilityPolicyId = null;\n"
    "            lastEligibilityFields = [];\n"
    "            profileResumePolicyId = null;\n"
    "            serverProfile = st.profile || {};\n"
    "            addBotMessage(data.response || '대화가 초기화되었어요. 아래 버튼을 눌러보세요!');\n"
    "            return;\n"
    "        }\n"
    "        if (command.type === 'RESET_PROFILE') {\n"
    "            deactivateAllCards();\n"
    "            if (command.policy_id) {\n"
    "                lastEligibilityPolicyId = command.policy_id;\n"
    "                profileResumePolicyId = command.policy_id;\n"
    "            }\n"
    "            if (Array.isArray(command.fields)) {\n"
    "                lastEligibilityFields = [...command.fields];\n"
    "                lastMissingFields = [...command.fields];\n"
    "            }\n"
    "            if (data.response) addBotMessage(data.response);\n"
    "            showProfileCard(Array.isArray(command.fields) && command.fields.length ? command.fields : undefined);\n"
    "            return;\n"
    "        }\n"
    "    }\n",
    "inline ui command renderer",
)

write(path, h)


# ---------------- README.md ----------------
path = "README.md"
r = read(path)
r = r.replace(
    '├─ sitecustomize.py          # 추천 Profile/UI 계약 및 GPT 전달값 호환 보정\n',
    '',
)
r = r.replace(
    '│  ├─ index.html             # 챗봇 UI\n│  └─ navigation_sync.js     # 자연어 State → 기존 버튼 UI 동기화\n',
    '│  └─ index.html             # 챗봇 UI 및 자연어 State → 버튼 UI 동기화\n',
)
r = r.replace(
    '`SHOW_ALTERNATIVES`, `CHANGE_TOPIC`, `FOLLOW_UP` 같은 자연어 후속 추천은 기존 Profile을 유지합니다. 예를 들어 `이거 말고 다른 정책 추천해줘`는 직전 결과를 제외하고, `주거로 바꿔줘`는 관심 분야만 주거로 변경합니다.',
    '`SHOW_ALTERNATIVES`, `CHANGE_TOPIC`, `FOLLOW_UP` 같은 자연어 후속 추천도 기존 Profile을 유지한 채 추천 조건 카드를 다시 보여줍니다. 예를 들어 `이거 말고 다른 정책 추천해줘`, `주거로 바꿔줘` 모두 현재 Profile을 prefill해 사용자가 확인·수정한 뒤 추천을 실행합니다.',
)
write(path, r)

print("direct flow patches applied")
