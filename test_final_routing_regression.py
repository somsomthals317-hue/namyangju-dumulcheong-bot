import unittest
from unittest.mock import patch

import agent
from data_loader import build_policy_bundles, load_eligibility_rules, load_summary_documents
from state import get_default_state


BUNDLES = build_policy_bundles(load_summary_documents(), load_eligibility_rules())


class FinalRoutingRegressionTests(unittest.TestCase):
    def test_active_eligibility_does_not_capture_new_employment_recommendation(self):
        state = get_default_state()
        state["active_clarify"] = "CLARIFY_ADDITIONAL"
        state["pending_tasks"] = ["ELIGIBILITY"]
        state["current_policy_id"] = "NYJ-YOUTH-016"
        state["focus_policy_id"] = "NYJ-YOUTH-016"
        action = agent.detect_navigation_action(state, "남양주 살아 취업 추천해줘", BUNDLES)
        self.assertIsNotNone(action)
        self.assertEqual(action["tasks"], ["RECOMMEND"])
        self.assertEqual(action["topic"], "취업")
        self.assertFalse(action["use_previous_context"])

    def test_all_structured_eligibility_policy_actions_never_become_explain(self):
        full = [b for b in BUNDLES if b.get("eligibility_mode") != "INFO_ONLY"]
        self.assertEqual(len(full), 25)
        for bundle in full:
            with self.subTest(policy=bundle["policy_name"]):
                state = get_default_state()
                action = {
                    "action": "NORMAL",
                    "tasks": ["ELIGIBILITY"],
                    "policy_id": bundle["policy_id"],
                    "policy_mention": bundle["policy_name"],
                    "use_previous_context": False,
                    "confidence": "high",
                }
                next_state, response = agent.handle_turn(
                    state,
                    f"{bundle['policy_name']} 자격 확인해줘",
                    None,
                    BUNDLES,
                    input_action=action,
                )
                workflow = next_state.get("active_workflow") or {}
                steps = workflow.get("steps") or []
                remaining_tasks = next_state.get("pending_tasks") or []
                self.assertNotIn("정책 설명은", response)
                self.assertNotEqual(next_state.get("last_task"), "EXPLAIN")
                self.assertTrue(
                    next_state.get("selected_policy_id") == bundle["policy_id"]
                    or next_state.get("focus_policy_id") == bundle["policy_id"]
                )
                if steps:
                    self.assertTrue(all(step.get("task") != "EXPLAIN" for step in steps))
                self.assertTrue(all(task != "EXPLAIN" for task in remaining_tasks))

    def test_housing_recommendation_ai_candidates_are_hard_gated_to_housing(self):
        state = get_default_state()
        state["interest_query"] = "주거"
        state["_explore_mode"] = True
        captured = []

        def fake_rerank(candidates, bundles, profile, interest):
            captured.extend(candidates)
            return candidates

        with patch.object(agent, "rerank_recommendations_with_ai", side_effect=fake_rerank):
            response = agent.run_recommend(state, BUNDLES)

        self.assertTrue(captured)
        self.assertTrue(all("주거" in item.get("matched_interests", []) for item in captured))
        names = {item["policy_name"] for item in captured}
        self.assertNotIn("청년창업 교육프로그램", names)
        self.assertNotIn("청년창업 컨설팅", names)
        self.assertNotIn("청년창업 교육프로그램", response)
        self.assertNotIn("청년창업 컨설팅", response)

    def test_multi_interest_candidates_match_at_least_one_requested_interest(self):
        state = get_default_state()
        state["interest_query"] = "주거, 취업"
        state["_explore_mode"] = True
        captured = []

        def fake_rerank(candidates, bundles, profile, interest):
            captured.extend(candidates)
            return candidates

        with patch.object(agent, "rerank_recommendations_with_ai", side_effect=fake_rerank):
            agent.run_recommend(state, BUNDLES)
        self.assertTrue(captured)
        self.assertTrue(all(
            set(item.get("matched_interests", [])) & {"주거", "취업"}
            for item in captured
        ))

    def test_generic_student_answer_keeps_profile_clarify(self):
        state = get_default_state()
        state["active_clarify"] = "CLARIFY_PROFILE"
        state["pending_tasks"] = ["RECOMMEND"]
        response = agent.handle_clarify_answer(state, "학생이야", None, BUNDLES)
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertIsNone(state["profile"].get("student"))
        self.assertIn("고등학생", response)
        self.assertIn("대학생", response)

    def test_bare_menu_variants_are_defined_as_same_front_door(self):
        text = open("server.py", encoding="utf-8").read()
        for token in ["자격조회", "자격조회하자", "자격확인", "자격확인하자"]:
            self.assertIn(f'"{token}"', text)
        for token in ["정책보기", "정책알아보자", "맞춤추천", "맞춤추천하자"]:
            self.assertIn(f'"{token}"', text)
        # Bare menu는 Prompt A보다 먼저 deterministic ui_command로 빠져야 한다.
        self.assertLess(text.index("simple_menu_type = _simple_menu_type(message)"), text.index("should_probe_intent = ("))

    def test_named_policy_request_is_not_bare_menu(self):
        # 단순 명령 집합은 exact compact match만 사용해야 정책명이 붙은 문장을 가로채지 않는다.
        text = open("server.py", encoding="utf-8").read()
        self.assertIn("if compact in eligibility", text)
        self.assertNotIn('"청년월세지원사업자격조회"', text)


if __name__ == "__main__":
    unittest.main()
