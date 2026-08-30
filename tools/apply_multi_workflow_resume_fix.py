from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


# ---------- agent.py ----------
p = Path('agent.py')
a = p.read_text(encoding='utf-8')

# When user explicitly reopens a recommendation Profile from a completed multi-query,
# rebuild that workflow from the RECOMMEND step while preserving earlier results.
anchor = '''def execute_active_workflow(state, collection, bundles):\n    """복합 task를 순서대로 실행하며 Clarify에서 멈추고 다음 턴에 재개한다."""\n'''
helper = '''def _restore_completed_multi_workflow_for_recommend(state):\n    """완료된 멀티쿼리의 추천 Profile 재설정 시 원래 Atomic 계약을 복원한다."""\n    snapshot = state.get("_last_completed_workflow")\n    if not isinstance(snapshot, dict):\n        return False\n    raw_steps = snapshot.get("steps")\n    if not isinstance(raw_steps, list) or len(raw_steps) < 2:\n        return False\n    steps = [dict(step) for step in raw_steps if isinstance(step, dict)]\n    recommend_index = next(\n        (index for index, step in enumerate(steps) if step.get("task") == "RECOMMEND"),\n        None,\n    )\n    if recommend_index is None:\n        return False\n\n    old_results = snapshot.get("results") if isinstance(snapshot.get("results"), dict) else {}\n    preserved_results = {}\n    for step in steps[:recommend_index]:\n        task = step.get("task")\n        if task in old_results:\n            preserved_results[task] = old_results[task]\n\n    # 추천부터 뒤의 단계는 새 Profile 기준으로 다시 실행한다.\n    for step in steps[recommend_index:]:\n        step["transition_applied"] = False\n        step["pre_clarifies"] = []\n\n    state["active_workflow"] = {\n        "steps": steps,\n        "index": recommend_index,\n        "results": preserved_results,\n        "original_query": snapshot.get("original_query") or "",\n        "build_error": None,\n    }\n    state["pending_tasks"] = [step.get("task") for step in steps[recommend_index:]]\n    state["active_clarify"] = None\n    state["_original_query"] = snapshot.get("original_query") or ""\n    return True\n\n\n'''
a = replace_once(a, anchor, helper + anchor, 'insert completed workflow restore helper')

# Explicit profile-rerun flag from the recommendation result restores the last multi workflow.
a = replace_once(
    a,
    '''    if ui_event == "SUBMIT_RECOMMEND_PROFILE":\n        state["_recommend_profile_submitted"] = True\n\n''',
    '''    if ui_event == "SUBMIT_RECOMMEND_PROFILE":\n        state["_recommend_profile_submitted"] = True\n        if (\n            not state.get("active_workflow")\n            and isinstance(input_action, dict)\n            and input_action.get("resume_multi_workflow") is True\n        ):\n            _restore_completed_multi_workflow_for_recommend(state)\n\n''',
    'restore multi workflow on recommendation profile rerun',
)

# Preserve a snapshot at successful workflow completion so profile re-review can keep
# the original EXPLAIN/RECOMMEND/ELIGIBILITY contract.
a = replace_once(
    a,
    '''    final_results = dict(results)\n    state["active_workflow"] = None\n''',
    '''    final_results = dict(results)\n    if len(steps) > 1 and any(step.get("task") == "RECOMMEND" for step in steps):\n        state["_last_completed_workflow"] = {\n            "steps": [dict(step) for step in steps],\n            "results": dict(final_results),\n            "original_query": workflow.get("original_query", ""),\n        }\n    state["active_workflow"] = None\n''',
    'snapshot completed multi workflow',
)

p.write_text(a, encoding='utf-8')


# ---------- state.py ----------
p = Path('state.py')
s = p.read_text(encoding='utf-8')
s = replace_once(
    s,
    '''    "_eligibility_profile_fields",\n)\n''',
    '''    "_eligibility_profile_fields",\n    # 새 작업을 명시적으로 시작하면 과거 멀티쿼리 재실행 계약도 폐기한다.\n    "_last_completed_workflow",\n)\n''',
    'clear old completed workflow on new task reset',
)
p.write_text(s, encoding='utf-8')


# ---------- static/index.html ----------
p = Path('static/index.html')
h = p.read_text(encoding='utf-8')

# Global flag is set only when the user explicitly clicks the result-level Profile reset.
h = replace_once(
    h,
    '''let pcSelections = {};\n\nfunction makeChipRow''',
    '''let pcSelections = {};\nlet resumeMultiWorkflowOnRecommendProfile = false;\n\nfunction makeChipRow''',
    'recommend multi rerun frontend flag',
)

# Send the explicit rerun flag in the structured action and render every returned State/card.
h = replace_once(
    h,
    '''                    explore_without_profile: false,\n                    confidence: 'high',\n''',
    '''                    explore_without_profile: false,\n                    resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile,\n                    confidence: 'high',\n''',
    'submit recommendation rerun flag',
)

old_submit_render = '''        if (st.active_clarify === 'CLARIFY_PREFERENCE') {\n            addBotMessage(data.response);\n            showInterestCardsInChat();\n        } else {\n            addBotMessage(data.response);\n        }\n'''
new_submit_render = '''        // 멀티쿼리는 추천 제출 뒤 ELIGIBILITY 등 다음 Clarify로 이어질 수 있으므로\n        // 말풍선만 그리지 말고 공통 State→카드 렌더러를 반드시 사용한다.\n        renderResponseWithCard(data);\n        resumeMultiWorkflowOnRecommendProfile = false;\n'''
h = replace_once(h, old_submit_render, new_submit_render, 'render cards after recommendation profile submit')

h = replace_once(
    h,
    '''function reopenRecommendProfile(btn) {\n    if (btn) btn.disabled = true;\n    deactivateAllCards();\n''',
    '''function reopenRecommendProfile(btn) {\n    if (btn) btn.disabled = true;\n    // 결과 카드에서 다시 설정한 경우에만 완료된 멀티쿼리 Atomic 계약을 재사용한다.\n    resumeMultiWorkflowOnRecommendProfile = true;\n    deactivateAllCards();\n''',
    'mark result-level recommendation profile rerun',
)

# Condition-less path can also advance to another task in a multi-query.
old_explore_render = '''        if (data.state && data.state.profile) serverProfile = data.state.profile;\n        addBotMessage(data.response);\n'''
new_explore_render = '''        if (data.state && data.state.profile) serverProfile = data.state.profile;\n        renderResponseWithCard(data);\n'''
h = replace_once(h, old_explore_render, new_explore_render, 'render cards after conditionless recommendation')

p.write_text(h, encoding='utf-8', newline='')


# ---------- tests ----------
p = Path('test_conversation_workflows.py')
t = p.read_text(encoding='utf-8')
insert_before = '''    def test_generic_alternative_excludes_all_visible_recommendations(self):\n'''
new_tests = r'''    def test_two_task_profile_pause_resume_keeps_explain_in_final_answer(self):
        state = complete_state()
        with patch.object(agent, "run_explain", return_value="월세 정책 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책도 추천해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["active_workflow"]["results"]["EXPLAIN"], "월세 정책 설명")

        with patch.object(agent, "run_recommend", return_value="주거 추천 결과"):
            state, final = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertIsNone(state["active_workflow"])
        self.assertIn("월세 정책 설명", final)
        self.assertIn("주거 추천 결과", final)

    def test_three_task_profile_pause_resume_reaches_eligibility_clarify(self):
        state = complete_state()
        with patch.object(agent, "run_explain", return_value="월세 정책 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책 추천해주고 청년월세 지원사업 자격도 확인해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["pending_tasks"], ["RECOMMEND", "ELIGIBILITY"])

        def fake_eligibility(current_state, bundles):
            current_state["active_clarify"] = "CLARIFY_ADDITIONAL"
            current_state["_active_additional_q"] = {
                "policy_id": "NYJ-YOUTH-011",
                "policy_name": "청년월세 지원사업",
                "questions": [{"question_id": "q1", "question": "추가 조건?", "options": ["예", "아니오"], "q_num": 1}],
                "total": 1,
                "batch_start": 1,
                "batch_end": 1,
                "remaining": 1,
            }
            return "추가 자격 확인이 필요해요."

        with patch.object(agent, "run_recommend", return_value="주거 추천 결과"), patch.object(
            agent, "run_eligibility", side_effect=fake_eligibility
        ):
            state, second = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_ADDITIONAL")
        self.assertEqual(state["pending_tasks"], ["ELIGIBILITY"])
        self.assertEqual(state["active_workflow"]["index"], 2)
        self.assertIn("정책 설명과 맞춤 추천", second)

    def test_result_profile_rerun_restores_completed_multi_query_contract(self):
        state = complete_state()
        with patch.object(agent, "run_explain", return_value="원래 월세 설명"), patch.object(
            agent, "run_recommend", return_value="첫 주거 추천"
        ):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책도 추천해줘",
                None,
                BUNDLES,
            )
            # 첫 턴은 추천 Profile 확인에서 멈추므로 제출하여 완료한다.
            state, first_final = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertIsNone(state["active_workflow"])
        self.assertIn("원래 월세 설명", first_final)
        self.assertIsInstance(state.get("_last_completed_workflow"), dict)

        with patch.object(agent, "run_recommend", return_value="수정 Profile 주거 추천"):
            state, rerun = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={
                    "action": "NORMAL",
                    "tasks": ["RECOMMEND"],
                    "topic": "주거",
                    "resume_multi_workflow": True,
                },
            )
        self.assertIn("원래 월세 설명", rerun)
        self.assertIn("수정 Profile 주거 추천", rerun)
        self.assertEqual(state["last_tasks"], ["EXPLAIN", "RECOMMEND"])

    def test_recommend_profile_frontend_always_renders_following_workflow_cards(self):
        with open("static/index.html", "r", encoding="utf-8") as file:
            html = file.read()
        submit_block = html[html.index("async function submitRecommendProfile"):html.index("function reopenRecommendProfile")]
        explore_block = html[html.index("async function exploreWithoutProfile"):html.index("function onInterestSelected")]
        self.assertIn("renderResponseWithCard(data)", submit_block)
        self.assertIn("renderResponseWithCard(data)", explore_block)
        self.assertIn("resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile", submit_block)

'''
t = replace_once(t, insert_before, new_tests + insert_before, 'insert workflow resume regressions')
p.write_text(t, encoding='utf-8')


# ---------- README.md ----------
p = Path('README.md')
r = p.read_text(encoding='utf-8')
needle = '- 한 발화에 `설명 + 추천`, `설명 + 추천 + 자격`처럼 **2개 이상의 Task가 명시되면 단일 라우팅보다 Atomic Workflow를 우선**하여 원문 순서와 대상 정책/분야를 보존한다.\n'
addition = needle + '- Atomic Workflow가 추천 Profile/자격 Additional 카드에서 멈춰도 완료된 앞 단계 결과와 남은 Task를 State에 유지하며, 추천 결과에서 `[프로필 다시 설정하기]`로 재추천해도 원래 멀티쿼리의 설명 결과와 후속 자격 Task를 다시 묶는다.\n'
r = replace_once(r, needle, addition, 'README multi workflow resume contract')
p.write_text(r, encoding='utf-8')

print('multi workflow resume patch applied')
