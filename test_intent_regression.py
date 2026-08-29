import unittest
from unittest.mock import patch

import agent
from data_loader import (
    build_policy_bundles,
    load_eligibility_rules,
    load_summary_documents,
)
from state import get_default_state


class NaturalIntentRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundles = build_policy_bundles(
            load_summary_documents(),
            load_eligibility_rules(),
        )

    def test_other_policy_eligibility_clears_stale_focus(self):
        state = get_default_state()
        state.update(
            {
                "focus_policy_id": "NYJ-YOUTH-004",
                "selected_policy_id": "NYJ-YOUTH-004",
                "_policy_mention": "경기청년 역량강화 기회지원사업(응시료지원)",
                "active_clarify": "CLARIFY_PROFILE",
                "pending_tasks": ["ELIGIBILITY"],
            }
        )

        next_state, response = agent.handle_turn(
            state,
            "나 자격 다른거 확인하고 싶어",
            None,
            self.bundles,
        )

        self.assertIsNone(next_state["focus_policy_id"])
        self.assertIsNone(next_state["selected_policy_id"])
        self.assertEqual(["ELIGIBILITY"], next_state["pending_tasks"])
        self.assertEqual("CLARIFY_POLICY", next_state["active_clarify"])
        self.assertIn("어떤 정책", response)

    def test_horse_industry_natural_eligibility_is_not_explain(self):
        state = get_default_state()

        # Prompt A가 EXPLAIN으로 흔들려도 코드의 자격 표현 및 공식 별칭
        # 보정이 말산업 정책의 ELIGIBILITY 흐름을 지켜야 한다.
        with patch.object(agent, "analyze_user_turn", return_value=(["EXPLAIN"], [])):
            next_state, response = agent.handle_turn(
                state,
                "말산업 그걸 되는지 안되는지 자격되는지 봐줘",
                None,
                self.bundles,
            )

        self.assertEqual("NYJ-YOUTH-006", next_state["selected_policy_id"])
        self.assertIn("ELIGIBILITY", next_state["pending_tasks"])
        self.assertNotIn("지원내용:", response)
        self.assertNotIn("[정책 설명]", response)

    def test_named_horse_policy_overrides_stale_exam_fee_focus(self):
        state = get_default_state()
        state.update({
            "focus_policy_id": "NYJ-YOUTH-004",
            "selected_policy_id": "NYJ-YOUTH-004",
            "_policy_mention": "경기청년 역량강화 기회지원사업(응시료지원)",
        })

        # Prompt A가 이전 정책을 반환해도 현재 발화의 명시 정책이 우선한다.
        with patch.object(agent, "analyze_user_turn", return_value=(["ELIGIBILITY"], [])):
            next_state, response = agent.handle_turn(
                state,
                "나 말산업 자격되는지 확인해줘",
                None,
                self.bundles,
            )

        self.assertEqual("NYJ-YOUTH-006", next_state["selected_policy_id"])
        self.assertEqual("NYJ-YOUTH-006", next_state["focus_policy_id"])
        self.assertIn("말산업 청년인턴취업지원", response)
        self.assertNotIn("경기청년 역량강화", response)

    def test_failed_horse_policy_keeps_full_policy_profile_contract(self):
        state = get_default_state()
        state["profile"].update({
            "age": 24,
            "residency": "예",
            "employment": "취업",
        })
        state["selected_policy_id"] = "NYJ-YOUTH-006"
        state["focus_policy_id"] = "NYJ-YOUTH-006"

        response = agent.run_eligibility(state, self.bundles)

        self.assertEqual(
            ["age", "residency", "employment"],
            state["_eligibility_profile_fields"],
        )
        self.assertIn(
            "[ACTION_BTN:RESET_PROFILE:NYJ-YOUTH-006:프로필 다시 설정하기]",
            response,
        )
        self.assertIn(
            "[ACTION_BTN:NORMAL_ELIGIBILITY:다른 자격 조회하기]",
            response,
        )


if __name__ == "__main__":
    unittest.main()

