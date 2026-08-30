from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


# The first patch has already modified these files when this script runs.
p = Path('static/index.html')
h = p.read_text(encoding='utf-8')

# Make the result-level multi-workflow resume flag strictly one-shot.
h = replace_once(
    h,
    """    try {\n        const res = await apiFetch('/api/chat', {\n""",
    """    const resumeMultiWorkflow = resumeMultiWorkflowOnRecommendProfile;\n    resumeMultiWorkflowOnRecommendProfile = false;\n    try {\n        const res = await apiFetch('/api/chat', {\n""",
    'capture one-shot resume flag in submitRecommendProfile',
)
h = replace_once(
    h,
    """                    resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile,\n""",
    """                    resume_multi_workflow: resumeMultiWorkflow,\n""",
    'use captured resume flag in submitRecommendProfile',
)
h = replace_once(
    h,
    """        renderResponseWithCard(data);\n        resumeMultiWorkflowOnRecommendProfile = false;\n""",
    """        renderResponseWithCard(data);\n""",
    'remove late resume flag reset',
)

# Condition-less from a reopened result card must also preserve the atomic workflow,
# and the flag must not leak into a later unrelated recommendation.
explore_anchor = """async function exploreWithoutProfile(btn) {\n    const card = btn.closest('.chat-card');\n"""
h = replace_once(
    h,
    explore_anchor,
    """async function exploreWithoutProfile(btn) {\n    const resumeMultiWorkflow = resumeMultiWorkflowOnRecommendProfile;\n    resumeMultiWorkflowOnRecommendProfile = false;\n    const card = btn.closest('.chat-card');\n""",
    'capture resume flag in exploreWithoutProfile',
)
h = replace_once(
    h,
    """                    explore_without_profile: true,\n                    confidence: 'high',\n""",
    """                    explore_without_profile: true,\n                    resume_multi_workflow: resumeMultiWorkflow,\n                    confidence: 'high',\n""",
    'send resume flag in conditionless path',
)

p.write_text(h, encoding='utf-8', newline='')


p = Path('test_conversation_workflows.py')
t = p.read_text(encoding='utf-8')
old_test = '''    def test_result_profile_rerun_restores_completed_multi_query_contract(self):
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
'''
new_test = '''    def test_result_profile_rerun_restores_completed_multi_query_contract(self):
        state = complete_state()
        # 첫 턴은 실제 run_recommend를 사용해 추천 Profile 확인에서 멈춘다.
        with patch.object(agent, "run_explain", return_value="원래 월세 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책도 추천해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["active_workflow"]["results"]["EXPLAIN"], "원래 월세 설명")

        # Profile 제출로 첫 멀티쿼리를 정상 완료한다.
        with patch.object(agent, "run_recommend", return_value="첫 주거 추천"):
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
        self.assertIn("첫 주거 추천", first_final)
        self.assertIsInstance(state.get("_last_completed_workflow"), dict)

        # 결과의 프로필 다시 설정하기에서 재제출하면 EXPLAIN은 보존하고
        # RECOMMEND부터 다시 실행해 최종 묶음 응답을 만든다.
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
'''
t = replace_once(t, old_test, new_test, 'fix result-level profile rerun test')

t = replace_once(
    t,
    '''        self.assertIn("resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile", submit_block)\n''',
    '''        self.assertIn("resume_multi_workflow: resumeMultiWorkflow", submit_block)\n        self.assertIn("resume_multi_workflow: resumeMultiWorkflow", explore_block)\n''',
    'assert one-shot resume flag in both frontend paths',
)
p.write_text(t, encoding='utf-8')

print('multi workflow resume follow-up applied')
