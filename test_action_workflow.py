import copy
import unittest
from unittest.mock import patch

import agent
from state import get_default_state


BUNDLES = [
    {
        "policy_id": "AGRI-1",
        "policy_name": "청년농업인 영농정착 지원",
        "recommendation_interests": ["농업"],
        "eligibility_mode": "RULE",
        "application_period": "1월",
        "source": "https://example.com/agri",
    },
    {
        "policy_id": "JOB-1",
        "policy_name": "취업성공 프로젝트",
        "recommendation_interests": ["취업"],
        "eligibility_mode": "RULE",
        "application_period": "2월",
        "source": "https://example.com/job",
    },
]


class ActionNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.state = get_default_state()
        self.state["current_topic"] = "농업"
        self.state["interest_query"] = "농업"
        self.state["current_policy_id"] = "AGRI-1"
        self.state["focus_policy_id"] = "AGRI-1"

    def test_change_topic_prioritizes_current_utterance(self):
        action = agent.detect_navigation_action(
            self.state, "농업 말고 취업 정책 알려줘", BUNDLES
        )
        self.assertEqual(action["action"], "CHANGE_TOPIC")
        self.assertEqual(action["topic"], "취업")
        self.assertIn("농업", action["exclude_topics"])

    def test_alternative_excludes_current_policy(self):
        action = agent.detect_navigation_action(
            self.state, "그거 말고 다른 거", BUNDLES
        )
        self.assertEqual(action["action"], "SHOW_ALTERNATIVES")
        self.assertIn("AGRI-1", action["exclude_policy_ids"])

    def test_follow_up_keeps_policy(self):
        action = agent.detect_navigation_action(
            self.state, "그 정책 신청 기간은?", BUNDLES
        )
        self.assertEqual(action["action"], "FOLLOW_UP")
        self.assertEqual(action["follow_up_field"], "application_period")
        tasks, clarifies, response = agent.apply_action_transition(
            self.state, action, BUNDLES
        )
        self.assertEqual(tasks, ["EXPLAIN"])
        self.assertEqual(clarifies, [])
        self.assertIsNone(response)
        self.assertEqual(self.state["current_policy_id"], "AGRI-1")

    def test_button_and_natural_alternative_have_same_transition(self):
        natural = agent.detect_navigation_action(
            self.state, "다른 정책 없어?", BUNDLES
        )
        button = agent.validate_action_payload({
            "action": "SHOW_ALTERNATIVES",
            "tasks": ["RECOMMEND"],
            "use_previous_context": True,
            "confidence": "high",
        })
        natural_state = copy.deepcopy(self.state)
        button_state = copy.deepcopy(self.state)
        natural_result = agent.apply_action_transition(
            natural_state, natural, BUNDLES
        )
        button_result = agent.apply_action_transition(
            button_state, button, BUNDLES
        )
        self.assertEqual(natural_result[:2], button_result[:2])
        for key in ("interest_query", "_exclude_policy_ids", "_exclude_topics"):
            self.assertEqual(natural_state.get(key), button_state.get(key))


class WorkflowTests(unittest.TestCase):
    def test_explanation_is_held_until_eligibility_finishes(self):
        state = get_default_state()
        state["_intent_workflow"] = [
            {"task": "EXPLAIN", "policy_mention": "농업 정책", "topic": None},
            {"task": "ELIGIBILITY", "policy_mention": "농업 정책", "topic": None},
        ]
        agent.start_active_workflow(state, ["EXPLAIN", "ELIGIBILITY"], "복합 요청")

        eligibility_calls = {"count": 0}

        def fake_explain(current_state, query, collection):
            current_state["current_policy_id"] = "AGRI-1"
            current_state["focus_policy_id"] = "AGRI-1"
            return "농업 정책 설명 본문"

        def fake_eligibility(current_state, bundles):
            eligibility_calls["count"] += 1
            if eligibility_calls["count"] == 1:
                current_state["active_clarify"] = "CLARIFY_PROFILE"
                return "자격 정보를 입력해주세요."
            return "농업 정책 자격 결과"

        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(
            agent, "run_eligibility", side_effect=fake_eligibility
        ):
            paused = agent.execute_active_workflow(state, object(), BUNDLES)
            self.assertNotIn("농업 정책 설명 본문", paused)
            self.assertIn("함께 정리", paused)
            self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")

            state["active_clarify"] = None
            final = agent.execute_active_workflow(state, object(), BUNDLES)
            self.assertIn("[정책 설명]", final)
            self.assertIn("농업 정책 설명 본문", final)
            self.assertIn("[자격 확인 결과]", final)
            self.assertIn("농업 정책 자격 결과", final)
            self.assertIsNone(state["active_workflow"])

    def test_recommend_then_eligibility_never_picks_first_candidate(self):
        state = get_default_state()
        state["_intent_workflow"] = [
            {"task": "RECOMMEND", "policy_mention": None, "topic": "농업"},
            {"task": "ELIGIBILITY", "policy_mention": None, "topic": None},
        ]
        agent.start_active_workflow(state, ["RECOMMEND", "ELIGIBILITY"], "추천하고 자격도")

        def fake_recommend(current_state, bundles):
            current_state["last_result_policy_ids"] = ["AGRI-1", "JOB-1"]
            current_state["current_policy_id"] = None
            return "추천 후보 두 개"

        with patch.object(agent, "run_recommend", side_effect=fake_recommend), patch.object(
            agent, "run_eligibility"
        ) as eligibility:
            response = agent.execute_active_workflow(state, object(), BUNDLES)

        eligibility.assert_not_called()
        self.assertEqual(state["active_clarify"], "CLARIFY_POLICY")
        self.assertEqual(len(state["_policy_candidates"]), 2)
        self.assertIn("추천 후보 두 개", response)
        self.assertIn("선택", response)

    def test_each_workflow_step_keeps_its_own_policy_target(self):
        state = get_default_state()
        state["_intent_workflow"] = [
            {"task": "EXPLAIN", "policy_mention": "취업성공 프로젝트", "topic": None},
            {"task": "ELIGIBILITY", "policy_mention": "청년농업인 영농정착 지원", "topic": None},
        ]
        agent.start_active_workflow(state, ["EXPLAIN", "ELIGIBILITY"], "서로 다른 대상")
        seen = []

        def fake_explain(current_state, query, collection):
            seen.append(("EXPLAIN", current_state.get("_policy_mention")))
            return "설명"

        def fake_eligibility(current_state, bundles):
            seen.append(("ELIGIBILITY", current_state.get("_policy_mention")))
            return "자격"

        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(
            agent, "run_eligibility", side_effect=fake_eligibility
        ):
            response = agent.execute_active_workflow(state, object(), BUNDLES)

        self.assertEqual(
            seen,
            [
                ("EXPLAIN", "취업성공 프로젝트"),
                ("ELIGIBILITY", "청년농업인 영농정착 지원"),
            ],
        )
        self.assertIn("[정책 설명]", response)
        self.assertIn("[자격 확인 결과]", response)


class FrontendContractTests(unittest.TestCase):
    def test_buttons_send_structured_actions_through_common_dispatcher(self):
        with open("static/index.html", encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("function dispatchAction(action, displayText)", html)
        self.assertIn("action: 'SHOW_ALTERNATIVES'", html)
        self.assertIn("action: 'CHECK_ELIGIBILITY'", html)
        self.assertIn("actionPayload ? { action: actionPayload }", html)


if __name__ == "__main__":
    unittest.main()
