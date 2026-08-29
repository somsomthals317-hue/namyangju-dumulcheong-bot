import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from state import get_default_state, update_profile


FULL_PROFILE = {
    "age": 25,
    "residency": "예",
    "employment": "미취업",
    "student": "대학생",
    "startup": "창업하지 않음",
    "housing": "무주택",
    "marriage": "미혼",
}


class DirectRecommendationFlowTests(unittest.TestCase):
    def test_natural_profile_text_accumulates_card_canonical_values(self):
        patch_values = agent.extract_profile_patch_from_text(
            "나 만 25세야 남양주 살아 미취업이야"
        )
        self.assertEqual(patch_values["age"], 25)
        self.assertEqual(patch_values["residency"], "예")
        self.assertEqual(patch_values["employment"], "미취업")

    def test_profile_normalizer_preserves_residency_no_and_ui_enums(self):
        normalized = agent._normalize_profile_patch({
            "residency": "아니오",
            "student": "아니오",
            "housing": "유주택",
        })
        self.assertEqual(normalized["residency"], "아니오")
        self.assertEqual(normalized["student"], "해당하지 않음")
        self.assertEqual(normalized["housing"], "주택 소유")

    def test_fresh_recommend_pauses_for_prefilled_profile_card(self):
        state = get_default_state()
        update_profile(state, FULL_PROFILE)
        tasks, clarifies, direct = agent.apply_action_transition(
            state,
            {
                "action": "NORMAL",
                "tasks": ["RECOMMEND"],
                "topic": "취업",
                "explore_without_profile": False,
                "confidence": "high",
            },
            [],
        )
        self.assertEqual(tasks, ["RECOMMEND"])
        self.assertEqual(clarifies, [])
        self.assertIsNone(direct)
        response = agent.run_recommend(state, [])
        self.assertTrue(response)
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["pending_tasks"], ["RECOMMEND"])
        self.assertEqual(state["profile"], FULL_PROFILE)

    def test_change_topic_keeps_profile_and_still_confirms_card(self):
        state = get_default_state()
        update_profile(state, FULL_PROFILE)
        state["interest_query"] = "취업"
        state["current_topic"] = "취업"
        tasks, clarifies, _ = agent.apply_action_transition(
            state,
            {
                "action": "CHANGE_TOPIC",
                "tasks": ["RECOMMEND"],
                "topic": "주거",
                "confidence": "high",
            },
            [],
        )
        self.assertEqual(tasks, ["RECOMMEND"])
        self.assertEqual(clarifies, [])
        self.assertFalse(state.get("_explore_mode", False))
        self.assertEqual(state["profile"], FULL_PROFILE)
        agent.run_recommend(state, [])
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["interest_query"], "주거")

    def test_explicit_explore_skips_card_without_deleting_profile(self):
        state = get_default_state()
        update_profile(state, FULL_PROFILE)
        agent.apply_action_transition(
            state,
            {
                "action": "NORMAL",
                "tasks": ["RECOMMEND"],
                "topic": "취업",
                "explore_without_profile": True,
                "confidence": "high",
            },
            [],
        )
        response = agent.run_recommend(state, [])
        self.assertNotEqual(state.get("active_clarify"), "CLARIFY_PROFILE")
        self.assertEqual(state["profile"], FULL_PROFILE)
        self.assertIn("정책", response)

    def test_named_policy_eligibility_query_routes_directly(self):
        state = get_default_state()
        bundles = [{"policy_id": "NYJ-YOUTH-023", "policy_name": "청년꽃간"}]
        with patch.object(agent, "_contains_exact_policy_name", return_value=True), \
             patch.object(agent, "_explicit_policy_id_from_name", return_value="NYJ-YOUTH-023"), \
             patch.object(agent, "resolve_policy_alias", return_value=None):
            action = agent.detect_navigation_action(
                state, "청년꽃간 자격조회하자", bundles
            )
        self.assertEqual(action["action"], "NORMAL")
        self.assertEqual(action["tasks"], ["ELIGIBILITY"])
        self.assertEqual(action["policy_id"], "NYJ-YOUTH-023")

    def test_rule_pass_cannot_be_downgraded_by_ai_residency_conflict(self):
        rule = {
            "eligibility_status": "PASS",
            "matched_conditions": ["공식 질문 충족"],
            "failed_conditions": [],
            "missing_conditions": [],
            "explanation": "규칙상 충족",
        }
        ai = {
            "ai_status": "FAIL",
            "reason": "거주지 확인 필요",
            "matched_conditions": [],
            "failed_conditions": ["거주지: 남양주시"],
            "missing_conditions": [],
        }
        merged = agent.merge_eligibility_review(rule, ai)
        self.assertEqual(merged["eligibility_status"], "PASS")
        self.assertEqual(merged["failed_conditions"], [])
        self.assertNotIn("AI 추가 확인", " ".join(merged["missing_conditions"]))

    def test_residency_semantics_are_ai_only(self):
        original = {"residency": "예"}
        converted = agent._profile_for_ai(original)
        self.assertEqual(original["residency"], "예")
        self.assertEqual(converted["residency"], "남양주시 거주")
        self.assertEqual(
            agent._profile_for_ai({"residency": "아니오"})["residency"],
            "남양주시 비거주",
        )

    def test_frontend_owns_ui_commands_and_explicit_explore_flags(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn("const command = data.ui_command;", html)
        self.assertIn("command.type === 'START_EXPLAIN'", html)
        self.assertIn("command.type === 'START_RECOMMEND'", html)
        self.assertIn("command.type === 'START_ELIGIBILITY'", html)
        self.assertIn("explore_without_profile: false", html)
        self.assertIn("explore_without_profile: true", html)
        self.assertNotIn("navigation_sync.js", html)

    def test_server_contains_standalone_menu_commands_and_no_runtime_patch(self):
        server = Path("server.py").read_text(encoding="utf-8")
        self.assertIn('"정책알아보자"', server)
        self.assertIn('"맞춤추천해보자"', server)
        self.assertIn('"자격조회하자"', server)
        self.assertNotIn("CHAT_SYNC_SCRIPT", server)
        self.assertNotIn("_profile_preserving_action_transition", server)


if __name__ == "__main__":
    unittest.main()
