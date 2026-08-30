from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


p = Path('agent.py')
a = p.read_text(encoding='utf-8')
a = replace_once(a,
'''    # 1. UI와 자연어를 공통 Action으로 먼저 정규화한다.\n    normalized_action = validate_action_payload(input_action)\n    if not normalized_action and not ui_event:\n        normalized_action = detect_navigation_action(state, user_message, bundles)\n''',
'''    # 1. UI와 자연어를 공통 Action으로 먼저 정규화한다.\n    # 한 발화에 설명/추천/자격 중 2개 이상이 명시되면 단일 Action shortcut보다\n    # atomic workflow를 먼저 확정한다. Prompt A가 Task를 누락하거나 navigation\n    # 규칙이 한 Task만 선점해도 사용자 원문의 작업 수/순서가 사라지지 않는다.\n    normalized_action = validate_action_payload(input_action)\n    if not normalized_action and not ui_event:\n        atomic_steps = infer_atomic_workflow_from_message(state, user_message, bundles)\n        if len(atomic_steps) >= 2:\n            atomic_tasks = [step['task'] for step in atomic_steps]\n            normalized_action = validate_action_payload({\n                'action': 'RUN_WORKFLOW',\n                'tasks': atomic_tasks,\n                'workflow': atomic_steps,\n                'confidence': 'high',\n            })\n        else:\n            normalized_action = detect_navigation_action(state, user_message, bundles)\n''',
'multi-task precedence before navigation')
a = replace_once(a,
'''    if has_additional and policy_id:\n        lines.append(\n            f"\\n[ACTION_BTN:EDIT_ADDITIONAL:{policy_id}:추가 답변 다시 입력하기]"\n        )\n    \n    # FAIL 또는 UNKNOWN이면 액션 버튼 추가\n    if status in ("FAIL", "UNKNOWN"):\n        if policy_id:\n            lines.append(\n                f"\\n[ACTION_BTN:RESET_PROFILE:{policy_id}:프로필 다시 설정하기]"\n            )\n        else:\n            lines.append("\\n[ACTION_BTN:RESET_PROFILE:프로필 다시 설정하기]")\n        lines.append("[ACTION_BTN:NORMAL_ELIGIBILITY:다른 자격 조회하기]")\n        lines.append("[ACTION_BTN:RESET_CHAT:대화 초기화하기]")\n''',
'''    if has_additional and policy_id:\n        lines.append(\n            f"\\n[ACTION_BTN:EDIT_ADDITIONAL:{policy_id}:추가 답변 다시 입력하기]"\n        )\n\n    # 최종 판정 상태와 Additional Question 유무에 관계없이 사용자는\n    # Profile을 다시 검토하거나 다른 정책 자격조회/대화 초기화로 이동할 수 있다.\n    if policy_id:\n        lines.append(\n            f"\\n[ACTION_BTN:RESET_PROFILE:{policy_id}:프로필 다시 설정하기]"\n        )\n    else:\n        lines.append("\\n[ACTION_BTN:RESET_PROFILE:프로필 다시 설정하기]")\n    lines.append("[ACTION_BTN:NORMAL_ELIGIBILITY:다른 자격 조회하기]")\n    lines.append("[ACTION_BTN:RESET_CHAT:대화 초기화하기]")\n''',
'common eligibility result actions')
p.write_text(a, encoding='utf-8')

p = Path('static/index.html')
h = p.read_text(encoding='utf-8')
h = replace_once(h,
'''        .chat-card-btn:disabled { opacity:0.5; cursor:not-allowed; }\n\n        /* 관심분야 버튼 그리드 */\n''',
'''        .chat-card-btn:disabled { opacity:0.5; cursor:not-allowed; }\n        .aq-action-row {\n            display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr);\n            gap:8px; margin-bottom:12px;\n        }\n        .aq-action-btn {\n            width:100%; min-width:0; margin:0; padding:10px 4px;\n            background:#f8f8f8; color:#555; border:1px solid #ddd;\n            border-radius:10px; font-size:11px; font-weight:600; line-height:1.2;\n            cursor:pointer; white-space:nowrap; word-break:keep-all;\n        }\n        .aq-action-btn:hover { border-color:#1e8c6e; background:#f0faf7; }\n\n        /* 관심분야 버튼 그리드 */\n''',
'additional action row css')
h = replace_once(h,
'''        <div style="display:flex;gap:8px;margin-bottom:12px;">\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="resetAndShowFullCard('${aq.policy_id || ''}', this)">프로필 다시 설정하기</button>\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="startEligibility()">다른 정책 선택</button>\n        </div>\n''',
'''        <div class="aq-action-row">\n            <button type="button" class="aq-action-btn" onclick="resetAndShowFullCard('${aq.policy_id || ''}', this)">프로필 다시 설정하기</button>\n            <button type="button" class="aq-action-btn" onclick="startEligibility()">다른 정책 선택</button>\n        </div>\n''',
'additional action row markup')
p.write_text(h, encoding='utf-8', newline='')

p = Path('test_conversation_workflows.py')
t = p.read_text(encoding='utf-8')
insert_before = '''    def test_generic_alternative_excludes_all_visible_recommendations(self):\n'''
new_tests = '''    def test_exact_user_explain_plus_recommend_is_atomic_before_prompt_a(self):\n        state = complete_state()\n        seen = []\n        def fake_explain(current_state, query, collection):\n            seen.append(("EXPLAIN", current_state.get("_policy_mention"), query))\n            return "청년월세 설명"\n        def fake_recommend(current_state, bundles):\n            seen.append(("RECOMMEND", current_state.get("interest_query"), None))\n            return "주거 추천"\n        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(agent, "run_recommend", side_effect=fake_recommend), patch.object(agent, "call_openai", side_effect=AssertionError("Prompt A must not decide explicit multi-task routing")):\n            next_state, response = agent.handle_turn(state, "청년월세 지원사업 설명해주고 주거 정책도 추천해줘", None, BUNDLES)\n        self.assertEqual([item[0] for item in seen], ["EXPLAIN", "RECOMMEND"])\n        self.assertEqual(seen[1][1], "주거")\n        self.assertIn("청년월세 설명", response)\n        self.assertIn("주거 추천", response)\n        self.assertEqual(next_state["last_tasks"], ["EXPLAIN", "RECOMMEND"])\n\n    def test_three_task_query_preserves_explain_recommend_eligibility_order(self):\n        state = complete_state()\n        seen = []\n        def fake_explain(current_state, query, collection):\n            seen.append("EXPLAIN")\n            current_state["current_policy_id"] = "NYJ-YOUTH-001"\n            current_state["focus_policy_id"] = "NYJ-YOUTH-001"\n            return "월세 설명"\n        def fake_recommend(current_state, bundles):\n            seen.append("RECOMMEND")\n            return "주거 추천"\n        def fake_eligibility(current_state, bundles):\n            seen.append("ELIGIBILITY")\n            return "월세 자격 결과"\n        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(agent, "run_recommend", side_effect=fake_recommend), patch.object(agent, "run_eligibility", side_effect=fake_eligibility), patch.object(agent, "call_openai", side_effect=AssertionError("Prompt A must not collapse three explicit tasks")):\n            next_state, response = agent.handle_turn(state, "청년월세 지원사업 설명해주고 주거 정책 추천해주고 청년월세 지원사업 자격도 확인해줘", None, BUNDLES)\n        self.assertEqual(seen, ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"])\n        self.assertEqual(next_state["last_tasks"], ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"])\n        self.assertIn("월세 설명", response)\n        self.assertIn("주거 추천", response)\n        self.assertIn("월세 자격 결과", response)\n\n    def test_final_eligibility_actions_exist_for_pass_without_additional(self):\n        result = {"eligibility_status":"PASS","explanation":"조건을 충족했습니다.","matched_conditions":["만 19세 이상"],"failed_conditions":[],"missing_conditions":[]}\n        response = agent.format_eligibility_response("청년성장 프로젝트", result, policy_id="NYJ-YOUTH-001", has_additional=False)\n        self.assertIn("ACTION_BTN:RESET_PROFILE", response)\n        self.assertIn("ACTION_BTN:NORMAL_ELIGIBILITY", response)\n        self.assertIn("ACTION_BTN:RESET_CHAT", response)\n        self.assertNotIn("ACTION_BTN:EDIT_ADDITIONAL", response)\n\n    def test_final_eligibility_additional_edit_is_conditional(self):\n        result = {"eligibility_status":"PASS","explanation":"조건을 충족했습니다.","matched_conditions":[],"failed_conditions":[],"missing_conditions":[]}\n        response = agent.format_eligibility_response("테스트 정책", result, policy_id="NYJ-YOUTH-001", has_additional=True)\n        self.assertIn("ACTION_BTN:EDIT_ADDITIONAL", response)\n        self.assertIn("ACTION_BTN:RESET_PROFILE", response)\n        self.assertIn("ACTION_BTN:NORMAL_ELIGIBILITY", response)\n        self.assertIn("ACTION_BTN:RESET_CHAT", response)\n\n    def test_additional_action_buttons_use_nonbreaking_two_column_layout(self):\n        with open("static/index.html", "r", encoding="utf-8") as file:\n            html = file.read()\n        self.assertIn(".aq-action-row", html)\n        self.assertIn("grid-template-columns:minmax(0,1fr) minmax(0,1fr)", html)\n        self.assertIn("white-space:nowrap", html)\n        self.assertIn('class="aq-action-btn"', html)\n\n'''
t = replace_once(t, insert_before, new_tests + insert_before, 'insert tests')
p.write_text(t, encoding='utf-8')

p = Path('README.md')
r = p.read_text(encoding='utf-8')
needle = '''- 이전 대화 Context는 필요할 때만 재사용하고, **현재 사용자가 명시한 새 Task·정책·분야를 진행 중 Clarify보다 우선**한다. 예를 들어 자격 추가질문 중 `만 25세야 ... 취업 정책 추천해줘`라고 하면 Profile 사실을 저장한 뒤 기존 자격 질문을 닫고 취업 추천으로 전환한다.\n'''
replacement = needle + '''- 한 발화에 `설명 + 추천`, `설명 + 추천 + 자격`처럼 **2개 이상의 Task가 명시되면 단일 라우팅보다 Atomic Workflow를 우선**하여 원문 순서와 대상 정책/분야를 보존한다.\n- 최종 자격 판정에는 PASS/FAIL/UNKNOWN 및 추가질문 유무와 관계없이 `[프로필 다시 설정하기]`, `[다른 자격 조회하기]`, `[대화 초기화하기]`를 제공하고, `[추가 답변 다시 입력하기]`는 실제 Additional Question이 있었던 정책에만 표시한다.\n'''
r = replace_once(r, needle, replacement, 'README')
p.write_text(r, encoding='utf-8')
print('applied')
