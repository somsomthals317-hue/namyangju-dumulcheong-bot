from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)

# agent.py: restore completed multi-query automatically on a recommendation Profile resubmit.
p = Path('agent.py')
a = p.read_text(encoding='utf-8')
a = replace_once(
    a,
    '''    if ui_event == "SUBMIT_RECOMMEND_PROFILE":\n        state["_recommend_profile_submitted"] = True\n        if (\n            not state.get("active_workflow")\n            and isinstance(input_action, dict)\n            and input_action.get("resume_multi_workflow") is True\n        ):\n            _restore_completed_multi_workflow_for_recommend(state)\n''',
    '''    if ui_event == "SUBMIT_RECOMMEND_PROFILE":\n        state["_recommend_profile_submitted"] = True\n        # 완료된 멀티쿼리 결과에서 추천 Profile을 다시 확인하는 경우에는\n        # 프론트의 임시 boolean에 의존하지 않고 서버 State의 마지막 Atomic\n        # Workflow를 자동 복원한다. 새 추천/버튼 시작 시 reset_task_context가\n        # 이 snapshot을 지우므로 unrelated 새 작업에는 과거 설명이 붙지 않는다.\n        if not state.get("active_workflow") and state.get("_last_completed_workflow"):\n            _restore_completed_multi_workflow_for_recommend(state)\n''',
    'automatic completed multi-workflow restore',
)
p.write_text(a, encoding='utf-8')

# server.py: explicit multi-task utterances must bypass the Prompt A navigation probe.
p = Path('server.py')
s = p.read_text(encoding='utf-8')
s = replace_once(
    s,
    '''        should_probe_intent = (\n            not ui_event\n            and input_action is None\n            and message.strip()\n            and get_guardrail_response(message) is None\n            and (\n''',
    '''        should_probe_intent = (\n            not ui_event\n            and input_action is None\n            and message.strip()\n            and get_guardrail_response(message) is None\n            # 설명+추천(+자격)처럼 작업 동사가 2개 이상 명시된 문장은\n            # Prompt A 사전 probe에서 축약하지 않는다. Agent의 deterministic\n            # Atomic parser가 원문 Task 수/순서/대상을 처음부터 소유한다.\n            and not agent_module._looks_like_multi_task_request(message)\n            and (\n''',
    'multi-task bypasses server intent probe',
)
p.write_text(s, encoding='utf-8')

# Strengthen the existing regressions to cover the real deployed contract.
p = Path('test_conversation_workflows.py')
t = p.read_text(encoding='utf-8')
t = replace_once(
    t,
    '''                input_action={\n                    "action": "NORMAL",\n                    "tasks": ["RECOMMEND"],\n                    "topic": "주거",\n                    "resume_multi_workflow": True,\n                },\n''',
    '''                input_action={\n                    "action": "NORMAL",\n                    "tasks": ["RECOMMEND"],\n                    "topic": "주거",\n                },\n''',
    'result profile rerun no longer needs frontend resume flag',
)
# Add a server-front-door source contract test near the existing frontend test.
needle = '''    def test_recommend_profile_frontend_always_renders_following_workflow_cards(self):\n'''
new_test = '''    def test_server_front_door_never_prompt_probes_explicit_multi_task_query(self):\n        with open("server.py", "r", encoding="utf-8") as file:\n            server = file.read()\n        self.assertIn(\n            "and not agent_module._looks_like_multi_task_request(message)",\n            server,\n        )\n\n'''
t = replace_once(t, needle, new_test + needle, 'server front-door multi-task regression')
p.write_text(t, encoding='utf-8')

# README contract note.
p = Path('README.md')
r = p.read_text(encoding='utf-8')
needle = '- Atomic Workflow가 추천 Profile/자격 Additional 카드에서 멈춰도 완료된 앞 단계 결과와 남은 Task를 State에 유지하며, 추천 결과에서 `[프로필 다시 설정하기]`로 재추천해도 원래 멀티쿼리의 설명 결과와 후속 자격 Task를 다시 묶는다.\n'
replacement = needle + '- 서버의 자연어 사전 Intent probe는 명시적 멀티쿼리를 건드리지 않으며, 완료된 멀티쿼리의 추천 Profile 재제출은 프론트 임시 상태가 아니라 서버의 `_last_completed_workflow`를 기준으로 자동 복원한다.\n'
r = replace_once(r, needle, replacement, 'README server atomic contract')
p.write_text(r, encoding='utf-8')

print('server multi-workflow contract patch applied')
