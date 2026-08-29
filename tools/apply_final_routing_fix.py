from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, got {count}")
    return text.replace(old, new, 1)


# server.py: deterministic bare-menu layer before Prompt A
p = Path('server.py')
s = p.read_text(encoding='utf-8')
s = replace_once(
    s,
    '''    explain = {\n        "정책알아보자", "정책알아보기", "정책찾아보자", "정책좀보자", "청년정책알아보자",\n    }\n    recommend = {\n        "맞춤추천하자", "맞춤추천해보자", "맞춤추천받자", "맞춤추천받기", "추천받자",\n    }\n    eligibility = {\n        "자격조회하자", "자격조회해보자", "자격확인하자", "자격확인해보자", "자격확인하기",\n    }\n''',
    '''    # 문장 전체가 메뉴 명령으로만 구성될 때만 여기서 확정한다.\n    # 정책명/분야/조건이 붙은 구체 요청은 아래 Prompt A/Agent가 의미를 판단한다.\n    explain = {\n        "정책", "정책보기", "정책알아보자", "정책알아보기", "정책찾아보자", "정책좀보자",\n        "청년정책", "청년정책보기", "청년정책알아보자",\n    }\n    recommend = {\n        "맞춤추천", "맞춤추천하자", "맞춤추천해보자", "맞춤추천받자", "맞춤추천받기",\n        "추천", "추천하자", "추천받자", "추천받기",\n    }\n    eligibility = {\n        "자격조회", "자격조회하자", "자격조회해보자", "자격조회해줘",\n        "자격확인", "자격확인하자", "자격확인해보자", "자격확인해줘", "자격확인하기",\n    }\n''',
    'bare menu variants',
)
s = replace_once(
    s,
    '''        state = get_session(session_id)\n\n        # 정책 별칭이 포함된 자연어와 세 가지 UI 전환 자연어는 Prompt A가\n''',
    '''        state = get_session(session_id)\n\n        # 0. Bare Menu Command Layer\n        # "자격조회"와 "자격조회하자"처럼 의미가 완전히 같은 메뉴 명령은\n        # GPT 변동성을 허용하지 않고 하단 버튼과 동일한 ui_command로 즉시 수렴한다.\n        # exact compact match이므로 "청년월세 자격조회" 같은 구체 요청은 절대 잡지 않는다.\n        simple_menu_type = (\n            _simple_menu_type(message)\n            if not ui_event and input_action is None and message.strip()\n            else None\n        )\n        if simple_menu_type:\n            reset_task_context(state)\n            state["messages"].append({"role": "user", "content": message.strip()})\n            sessions[session_id] = state\n            session_last_seen[session_id] = time.monotonic()\n            return _chat_response(state, "", {"type": simple_menu_type})\n\n        # 정책 별칭이 포함된 자연어와 나머지 UI 전환 자연어는 Prompt A가\n''',
    'bare menu early bypass',
)
p.write_text(s, encoding='utf-8')


# agent.py
p = Path('agent.py')
a = p.read_text(encoding='utf-8')

# A clear topic + recommendation/change request is always a new recommendation unit,
# even while eligibility/profile cards are active.
a = replace_once(
    a,
    '''    # "취업 정책은?"처럼 현재 발화의 새 분야를 과거 주제보다 우선한다.\n    if mentioned_topics and not exact_policy and re.search(\n''',
    '''    # "남양주 살아 취업 추천해줘"처럼 Profile 정보와 새 추천 요청이 한 문장에\n    # 함께 있어도 진행 중 자격/추가질문의 답변으로 흡수하지 않는다.\n    if mentioned_topics and not exact_policy and (\n        _is_explicit_recommend_request(msg)\n        or re.search(r"(?:바꿔|바꾸|변경|전환)(?:줘|해줘|할래)?", msg)\n    ):\n        topic = ", ".join(mentioned_topics)\n        current_topics = _clean_action_topics(current_topic) if current_topic else []\n        action = "CHANGE_TOPIC" if current_topics and any(t not in current_topics for t in mentioned_topics) else "NORMAL"\n        return validate_action_payload({\n            "action": action,\n            "tasks": ["RECOMMEND"],\n            "topic": topic,\n            "use_previous_context": False,\n            "confidence": "high",\n        })\n\n    # "취업 정책은?"처럼 현재 발화의 새 분야를 과거 주제보다 우선한다.\n    if mentioned_topics and not exact_policy and re.search(\n''',
    'explicit recommendation before stale clarify',
)

# Generic student answer cannot be coerced into a specific enum. Keep the card active.
a = replace_once(
    a,
    '''    elif clarify_type == "CLARIFY_PROFILE":\n        if (\n            "프로필 입력 완료" in user_message\n''',
    '''    elif clarify_type == "CLARIFY_PROFILE":\n        if re.fullmatch(r"\\s*(?:나(?:는|도)?\\s*)?학생(?:이야|이에요|입니다)?[.!?]?\\s*", user_message or ""):\n            state["active_clarify"] = "CLARIFY_PROFILE"\n            return (\n                "학생 유형을 조금 더 구체적으로 알려주세요. "\n                "고등학생, 대학생, 대학원생, 해당하지 않음 중에서 선택해주세요."\n            )\n        if (\n            "프로필 입력 완료" in user_message\n''',
    'generic student clarify',
)

# Recommendation interest is a hard candidate gate; GPT only ranks already-matched policies.
a = replace_once(
    a,
    '''    matched_results = results if interest == "전체" else [r for r in results if r["relevance"] >= 0.35]\n\n    # 명확한 자격 FAIL은 추천 후보에서 제외한다. 그 외에는 코드 키워드가\n    # 놓친 정책까지 GPT가 의미적으로 판단할 수 있도록 전체 정책을 전달한다.\n    # GPT 장애/형식 오류 때만 결정론적 키워드 후보로 안전하게 복구한다.\n    hard_fail_results = [r for r in matched_results if r.get("eligibility_status") == "FAIL"]\n    ai_candidates = [r for r in results if r.get("eligibility_status") != "FAIL"]\n    fallback_candidates = [r for r in matched_results if r.get("eligibility_status") != "FAIL"]\n''',
    '''    # 관심 분야는 추천의 hard gate다. GPT는 이 집합 안에서만 의미 적합성과\n    # 순위를 판단하며 다른 분야 정책을 새로 끼워 넣을 수 없다. 복수 분야는\n    # 선택한 분야 중 하나 이상과 공식 recommendation_interests가 맞으면 포함한다.\n    matched_results = results if interest == "전체" else [\n        r for r in results if r.get("matched_interests")\n    ]\n\n    # 명확한 자격 FAIL은 추천 후보에서 제외한다. GPT 장애/형식 오류 때도\n    # 동일한 분야 hard gate 안의 결정론적 후보로만 복구한다.\n    hard_fail_results = [r for r in matched_results if r.get("eligibility_status") == "FAIL"]\n    ai_candidates = [r for r in matched_results if r.get("eligibility_status") != "FAIL"]\n    fallback_candidates = [r for r in matched_results if r.get("eligibility_status") != "FAIL"]\n''',
    'recommendation hard gate',
)

p.write_text(a, encoding='utf-8')


# Add exact multi-task regression for the user's observed sentence.
p = Path('test_final_routing_regression.py')
t = p.read_text(encoding='utf-8')
marker = '''    def test_bare_menu_variants_are_defined_as_same_front_door(self):\n'''
method = '''    def test_explain_and_eligibility_sentence_never_turns_into_recommendation(self):\n        state = get_default_state()\n        wrong_model = {\n            "action": "NORMAL", "turn_kind": "NEW_TASK", "reuse_focus": False,\n            "use_previous_context": False, "confidence": "high",\n            "tasks": ["RECOMMEND"], "topic": "복지", "policy_mention": None,\n            "workflow": [{"action": "NORMAL", "task": "RECOMMEND", "topic": "복지"}],\n            "profile_patch": {}, "clarify_reasons": [],\n        }\n        import json\n        seen = []\n        with patch.object(agent, "call_openai", return_value=json.dumps(wrong_model, ensure_ascii=False)), \\\n             patch.object(agent, "run_explain", side_effect=lambda s, q, c: seen.append("EXPLAIN") or "설명"), \\\n             patch.object(agent, "run_eligibility", side_effect=lambda s, b: seen.append("ELIGIBILITY") or "자격"):\n            next_state, response = agent.handle_turn(\n                state, "입영지원금 설명해주고 내가 자격되는지도 확인해줘", None, BUNDLES\n            )\n        self.assertEqual(seen, ["EXPLAIN", "ELIGIBILITY"])\n        self.assertNotIn("RECOMMEND", next_state.get("last_tasks", []))\n\n'''
if marker not in t:
    raise RuntimeError('test insertion marker missing')
t = t.replace(marker, method + marker, 1)
p.write_text(t, encoding='utf-8')

print('final routing patch applied')
