import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from data_loader import build_policy_bundles, load_eligibility_rules, load_summary_documents
from state import get_default_state


BUNDLES = build_policy_bundles(load_summary_documents(), load_eligibility_rules())


def full_profile():
    return {
        "age": 25,
        "residency": "예",
        "employment": "미취업",
        "student": "대학생",
        "startup": "창업하지 않음",
        "housing": "무주택",
        "marriage": "미혼",
    }


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

    def test_full_turn_age_profile_and_new_recommendation_interrupts_additional_card(self):
        state = get_default_state()
        state["active_clarify"] = "CLARIFY_ADDITIONAL"
        state["pending_tasks"] = ["ELIGIBILITY"]
        state["current_policy_id"] = "NYJ-YOUTH-016"
        state["focus_policy_id"] = "NYJ-YOUTH-016"
        state["selected_policy_id"] = "NYJ-YOUTH-016"
        state["active_workflow"] = {
            "index": 0,
            "steps": [{"action": "NORMAL", "task": "ELIGIBILITY", "policy_id": "NYJ-YOUTH-016"}],
            "responses": [],
        }
        state["_active_additional_q"] = {
            "policy_id": "NYJ-YOUTH-016",
            "questions": [{"question_id": "dummy", "question": "dummy?", "options": ["예", "아니오"]}],
        }

        next_state, response = agent.handle_turn(
            state,
            "나 만 25세야 남양주 살아 미취업이야 취업 정책 추천해줘",
            None,
            BUNDLES,
        )

        self.assertEqual(next_state["profile"]["age"], 25)
        self.assertEqual(next_state["profile"]["residency"], "예")
        self.assertEqual(next_state["profile"]["employment"], "미취업")
        self.assertEqual(next_state.get("interest_query"), "취업")
        self.assertEqual(next_state.get("active_clarify"), "CLARIFY_PROFILE")
        self.assertEqual(next_state.get("pending_tasks"), ["RECOMMEND"])
        self.assertNotEqual(next_state.get("active_clarify"), "CLARIFY_ADDITIONAL")
        self.assertIn("맞춤 추천", response)

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

    def test_saved_profile_skips_redundant_eligibility_profile_card(self):
        bundle = next(b for b in BUNDLES if b.get("policy_name") == "청년 주거급여")
        state = get_default_state()
        state["profile"].update(full_profile())
        state["profile_status"] = "COMPLETE"
        state["selected_policy_id"] = bundle["policy_id"]
        state["focus_policy_id"] = bundle["policy_id"]

        response = agent.run_eligibility(state, BUNDLES)

        self.assertNotEqual(state.get("active_clarify"), "CLARIFY_PROFILE")
        if bundle.get("additional_questions"):
            self.assertEqual(state.get("active_clarify"), "CLARIFY_ADDITIONAL")
            aq = state.get("_active_additional_q") or {}
            self.assertEqual(aq.get("policy_id"), bundle["policy_id"])
            self.assertEqual(aq.get("policy_name"), "청년 주거급여")
            self.assertTrue(aq.get("questions"))
            self.assertIn("추가 자격 조건", response)

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

    def test_explain_and_eligibility_sentence_never_turns_into_recommendation(self):
        state = get_default_state()
        wrong_model = {
            "action": "NORMAL", "turn_kind": "NEW_TASK", "reuse_focus": False,
            "use_previous_context": False, "confidence": "high",
            "tasks": ["RECOMMEND"], "topic": "복지", "policy_mention": None,
            "workflow": [{"action": "NORMAL", "task": "RECOMMEND", "topic": "복지"}],
            "profile_patch": {}, "clarify_reasons": [],
        }
        import json
        seen = []
        with patch.object(agent, "call_openai", return_value=json.dumps(wrong_model, ensure_ascii=False)), \
             patch.object(agent, "run_explain", side_effect=lambda s, q, c: seen.append("EXPLAIN") or "설명"), \
             patch.object(agent, "run_eligibility", side_effect=lambda s, b: seen.append("ELIGIBILITY") or "자격"):
            next_state, response = agent.handle_turn(
                state, "입영지원금 설명해주고 내가 자격되는지도 확인해줘", None, BUNDLES
            )
        self.assertEqual(seen, ["EXPLAIN", "ELIGIBILITY"])
        self.assertNotIn("RECOMMEND", next_state.get("last_tasks", []))

    def test_bare_menu_variants_are_defined_as_same_front_door(self):
        text = Path("server.py").read_text(encoding="utf-8")
        for token in ["자격조회", "자격조회하자", "자격확인", "자격확인하자"]:
            self.assertIn(f'"{token}"', text)
        for token in ["정책보기", "정책알아보자", "맞춤추천", "맞춤추천하자"]:
            self.assertIn(f'"{token}"', text)
        self.assertLess(text.index("simple_menu_type = ("), text.index("should_probe_intent = ("))

    def test_named_policy_request_is_not_bare_menu(self):
        text = Path("server.py").read_text(encoding="utf-8")
        self.assertIn("if compact in eligibility", text)
        self.assertNotIn('"청년월세지원사업자격조회"', text)

    def test_server_eligibility_dropdown_is_exact_application_set(self):
        text = Path("server.py").read_text(encoding="utf-8")
        self.assertIn('bundle.get("eligibility_mode") != "INFO_ONLY"', text)
        policies_block = text[text.index('def get_policies()'):]
        self.assertNotIn('item["policy_id"].startswith("NYJ-YOUTH-")', policies_block)
        self.assertIn('if item["policy_id"] in eligibility_ids', policies_block)

    def test_frontend_cards_show_context_and_profile_reset_actions(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn("📌 관심 분야:", html)
        self.assertIn("RESET_RECOMMEND_PROFILE", html)
        self.assertIn("function reopenRecommendProfile", html)
        self.assertIn("✅ 선택 정책:", html)
        self.assertIn("lastEligibilityPolicyName", html)
        additional_block = html[html.index("function showAdditionalQuestionCard"):html.index("let aqSelections")]
        self.assertIn("프로필 다시 설정하기", additional_block)
        self.assertIn("다른 정책 선택", additional_block)

    def test_recommend_response_contains_profile_reset_action(self):
        response = agent.format_recommend_response([], [], 0, 0, interest="취업")
        self.assertIn("RESET_RECOMMEND_PROFILE", response)


if __name__ == "__main__":
    unittest.main()
