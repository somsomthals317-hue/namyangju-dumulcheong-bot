import json
import unittest
from unittest.mock import patch

import agent
from data_loader import (
    build_policy_bundles,
    load_eligibility_rules,
    load_summary_documents,
)
from state import get_default_state, reset_task_context


ALLOWED_INTERESTS = {
    "취업", "교육", "농업", "복지", "기본소득",
    "주거", "창업", "참여·문화", "전체",
}
CONDITION_FIELD_MAP = {
    "age_condition": "age",
    "residency_condition": "residency",
    "income_condition": "income",
    "employment_condition": "employment",
    "student_condition": "student",
    "startup_condition": "startup",
    "housing_condition": "housing",
    "marriage_condition": "marriage",
}


def intent_payload(**overrides):
    payload = {
        "tasks": [],
        "clarify_reasons": [],
        "profile_patch": {},
        "interest_query": None,
        "policy_mention": None,
        "rewritten_query": None,
        "turn_kind": "SMALL_TALK",
        "reuse_focus": False,
        "confidence": "high",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class GptStateHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summaries = load_summary_documents()
        cls.rules = load_eligibility_rules()
        cls.bundles = build_policy_bundles(cls.summaries, cls.rules)

    def test_prompt_a_uses_json_mode_and_switch_clears_stale_task(self):
        state = get_default_state()
        state.update({
            "focus_policy_id": "NYJ-YOUTH-004",
            "selected_policy_id": "NYJ-YOUTH-004",
            "interest_query": "취업",
            "active_clarify": "CLARIFY_PROFILE",
            "pending_tasks": ["ELIGIBILITY"],
            "_active_additional_q": {"policy_id": "NYJ-YOUTH-004"},
        })
        reply = intent_payload(
            tasks=["ELIGIBILITY"],
            clarify_reasons=["CLARIFY_POLICY"],
            turn_kind="SWITCH_POLICY",
            confidence="high",
        )

        with patch.object(agent, "call_openai", return_value=reply) as mocked:
            tasks, clarifies = agent.analyze_user_turn(
                state, "아까 건 말고 다른 지원 자격을 새로 보고 싶어"
            )

        self.assertEqual(["ELIGIBILITY"], tasks)
        self.assertEqual(["CLARIFY_POLICY"], clarifies)
        self.assertTrue(mocked.call_args.kwargs["json_mode"])
        self.assertIsNone(state["focus_policy_id"])
        self.assertIsNone(state["selected_policy_id"])
        self.assertIsNone(state["interest_query"])
        self.assertIsNone(state["active_clarify"])
        self.assertNotIn("_active_additional_q", state)

    def test_explicit_focus_reference_is_the_only_gpt_focus_reuse(self):
        state = get_default_state()
        state["focus_policy_id"] = "NYJ-YOUTH-010"
        reply = intent_payload(
            tasks=["ELIGIBILITY"],
            policy_mention="청년기본소득",
            turn_kind="NEW_TASK",
            reuse_focus=True,
        )
        with patch.object(agent, "call_openai", return_value=reply):
            tasks, _ = agent.analyze_user_turn(state, "그 정책 자격도 확인해줘")
        self.assertEqual(["ELIGIBILITY"], tasks)
        self.assertEqual("NYJ-YOUTH-010", state["focus_policy_id"])

    def test_active_profile_card_can_be_semantically_switched_by_gpt(self):
        state = get_default_state()
        state.update({
            "focus_policy_id": "NYJ-YOUTH-004",
            "selected_policy_id": "NYJ-YOUTH-004",
            "active_clarify": "CLARIFY_PROFILE",
            "pending_tasks": ["ELIGIBILITY"],
        })
        reply = intent_payload(
            tasks=["ELIGIBILITY"],
            clarify_reasons=["CLARIFY_POLICY"],
            turn_kind="SWITCH_POLICY",
        )
        with patch.object(agent, "call_openai", return_value=reply) as mocked:
            next_state, response = agent.handle_turn(
                state,
                "지금 입력은 멈추고 완전히 다른 지원의 신청 가능성을 보고 싶어요",
                None,
                self.bundles,
            )
        self.assertTrue(mocked.called)
        self.assertEqual("CLARIFY_POLICY", next_state["active_clarify"])
        self.assertIsNone(next_state["focus_policy_id"])
        self.assertIn("어떤 정책", response)

    def test_generic_recommendation_does_not_reuse_old_interest(self):
        state = get_default_state()
        state["interest_query"] = "취업"
        next_state, response = agent.handle_turn(
            state, "맞춤 추천해줘", None, self.bundles
        )
        self.assertIsNone(next_state["interest_query"])
        self.assertEqual("CLARIFY_PREFERENCE", next_state["active_clarify"])
        self.assertIn("관심", response)

    def test_other_eligibility_request_does_not_reuse_old_policy(self):
        state = get_default_state()
        state["focus_policy_id"] = "NYJ-YOUTH-004"
        state["selected_policy_id"] = "NYJ-YOUTH-004"
        next_state, response = agent.handle_turn(
            state, "다른 정책 자격을 확인하고 싶어", None, self.bundles
        )
        self.assertIsNone(next_state["focus_policy_id"])
        self.assertIsNone(next_state["selected_policy_id"])
        self.assertEqual("CLARIFY_POLICY", next_state["active_clarify"])
        self.assertIn("어떤 정책", response)

    def test_reset_task_context_keeps_profile_and_saved_answers(self):
        state = get_default_state()
        state["profile"]["age"] = 24
        state["policy_answers"]["NYJ-YOUTH-010"] = {"q": "예"}
        state["focus_policy_id"] = "NYJ-YOUTH-010"
        state["interest_query"] = "기본소득"
        state["_policy_candidates"] = [{"policy_id": "NYJ-YOUTH-010"}]
        reset_task_context(state)
        self.assertEqual(24, state["profile"]["age"])
        self.assertEqual({"q": "예"}, state["policy_answers"]["NYJ-YOUTH-010"])
        self.assertIsNone(state["focus_policy_id"])
        self.assertIsNone(state["interest_query"])
        self.assertNotIn("_policy_candidates", state)

    def test_additional_question_card_has_batch_progress(self):
        questions = [
            {
                "question_id": f"q{i}",
                "question": f"질문 {i}?",
                "options": ["예", "아니오"],
            }
            for i in range(1, 6)
        ]
        bundle = {
            "policy_id": "TEST-POLICY",
            "policy_name": "테스트 정책",
            "eligibility_mode": "FULL",
            "basic_condition": {
                key: "해당없음"
                for key in (
                    "age", "residency", "income", "employment",
                    "student", "startup", "housing", "marriage",
                )
            },
            "additional_questions": questions,
            "caution_condition": [],
            "unverified_conditions": [],
            "source": "",
        }
        state = get_default_state()
        state["selected_policy_id"] = "TEST-POLICY"
        response = agent.run_eligibility(state, [bundle])
        card = state["_active_additional_q"]
        self.assertIn("추가 자격 조건", response)
        self.assertEqual(5, card["total"])
        self.assertEqual(0, card["answered"])
        self.assertEqual(5, card["remaining"])
        self.assertEqual(1, card["batch_start"])
        self.assertEqual(3, card["batch_end"])
        self.assertEqual([1, 2, 3], [q["q_num"] for q in card["questions"]])

    def test_result_exposes_edit_additional_answers_action(self):
        text = agent.format_eligibility_response(
            "테스트 정책",
            {
                "eligibility_status": "PASS",
                "explanation": "검토 완료",
                "matched_conditions": [],
                "failed_conditions": [],
                "missing_conditions": [],
                "next_questions": [],
                "caution_condition": [],
            },
            policy_id="TEST-POLICY",
            has_additional=True,
        )
        self.assertIn(
            "[ACTION_BTN:EDIT_ADDITIONAL:TEST-POLICY:추가 답변 다시 입력하기]",
            text,
        )

    def test_eligibility_ai_review_uses_json_mode(self):
        bundle = {
            "policy_id": "TEST-POLICY",
            "policy_name": "테스트 정책",
            "summary": "요약",
            "main_target": "청년",
            "benefit": "지원",
            "basic_condition": {},
            "additional_questions": [],
            "caution_condition": [],
        }
        rule_result = {
            "eligibility_status": "PASS",
            "matched_conditions": [],
            "failed_conditions": [],
            "missing_conditions": [],
            "explanation": "규칙 통과",
        }
        ai_reply = json.dumps({
            "ai_status": "PASS",
            "reason": "공식 조건과 입력이 일치합니다.",
            "matched_conditions": [],
            "failed_conditions": [],
            "missing_conditions": [],
        }, ensure_ascii=False)
        with patch.object(agent, "call_openai", return_value=ai_reply) as mocked:
            result = agent.review_eligibility_with_ai(
                bundle, {}, {}, rule_result
            )
        self.assertEqual("PASS", result["eligibility_status"])
        self.assertTrue(mocked.call_args.kwargs["json_mode"])


class PolicyDataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summaries = load_summary_documents()
        cls.rules = load_eligibility_rules()

    def test_all_32_policies_have_valid_recommendation_interests(self):
        self.assertEqual(32, len(self.summaries))
        for policy in self.summaries:
            interests = policy.get("recommendation_interests")
            self.assertTrue(interests, policy["policy_id"])
            self.assertTrue(set(interests) <= ALLOWED_INTERESTS, policy["policy_id"])

    def test_summary_and_rules_conditions_are_complete_and_equal(self):
        rules_by_id = {item["policy_id"]: item for item in self.rules}
        self.assertEqual(
            {item["policy_id"] for item in self.summaries},
            set(rules_by_id),
        )
        for summary in self.summaries:
            policy_id = summary["policy_id"]
            basic = rules_by_id[policy_id].get("basic_condition", {})
            self.assertEqual(set(CONDITION_FIELD_MAP.values()), set(basic), policy_id)
            for summary_key, rule_key in CONDITION_FIELD_MAP.items():
                self.assertTrue(str(summary.get(summary_key, "")).strip(), policy_id)
                self.assertEqual(summary[summary_key], basic[rule_key], policy_id)

    def test_additional_questions_have_unique_ids_and_labels(self):
        for rule in self.rules:
            questions = rule.get("additional_questions", [])
            question_ids = [item.get("question_id") for item in questions]
            self.assertEqual(len(question_ids), len(set(question_ids)), rule["policy_id"])
            for item in questions:
                self.assertTrue(str(item.get("question_id") or "").strip(), rule["policy_id"])
                self.assertTrue(str(item.get("question") or "").strip(), rule["policy_id"])
                self.assertIsInstance(item.get("options", []), list)


class FrontendContractTests(unittest.TestCase):
    def test_all_api_calls_use_timeout_wrapper(self):
        with open("static/index.html", "r", encoding="utf-8") as file:
            html = file.read()
        self.assertIn("async function apiFetch", html)
        self.assertNotIn("await fetch('/api/", html)
        self.assertIn("EDIT_ADDITIONAL_ANSWERS", html)
        self.assertIn("batchStart", html)
        self.assertIn("crypto.randomUUID", html)


if __name__ == "__main__":
    unittest.main()
