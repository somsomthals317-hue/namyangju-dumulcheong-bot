import json
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


def focused_state():
    state = get_default_state()
    state["current_policy_id"] = "AGRI-1"
    state["focus_policy_id"] = "AGRI-1"
    state["current_topic"] = "농업"
    state["interest_query"] = "농업"
    return state


class ActionTaskBackboneTests(unittest.TestCase):
    def test_deployed_legacy_actions_are_only_compatibility_aliases(self):
        explain = agent.validate_action_payload({
            "action": "SEARCH_POLICY", "tasks": ["EXPLAIN"]
        })
        eligibility = agent.validate_action_payload({
            "action": "CHECK_ELIGIBILITY", "tasks": ["ELIGIBILITY"]
        })
        self.assertEqual(explain["action"], "NORMAL")
        self.assertEqual(eligibility["action"], "NORMAL")

    def test_change_topic_is_recommend_only(self):
        invalid = agent.validate_action_payload({
            "action": "CHANGE_TOPIC",
            "tasks": ["EXPLAIN"],
            "topic": "취업",
        })
        self.assertEqual(invalid["action"], "CLARIFY")
        self.assertEqual(invalid["tasks"], [])
        self.assertEqual(invalid["clarify_reason"], "INVALID_ACTION_TASK")

    def test_follow_up_can_select_eligibility_task(self):
        action = agent.detect_navigation_action(
            focused_state(), "나도 신청할 수 있어?", BUNDLES
        )
        self.assertEqual(action["action"], "FOLLOW_UP")
        self.assertEqual(action["tasks"], ["ELIGIBILITY"])

    def test_follow_up_recommend_uses_policy_interest_as_seed(self):
        state = focused_state()
        action = agent.detect_navigation_action(
            state, "이거랑 비슷한 정책 추천해줘", BUNDLES
        )
        tasks, clarifies, _ = agent.apply_action_transition(state, action, BUNDLES)
        self.assertEqual(action["action"], "FOLLOW_UP")
        self.assertEqual(tasks, ["RECOMMEND"])
        self.assertEqual(state["interest_query"], "농업")
        self.assertNotIn("CLARIFY_PREFERENCE", clarifies)

    def test_show_alternative_can_explain_new_policy(self):
        state = focused_state()
        action = agent.detect_navigation_action(
            state, "그거 말고 취업성공 프로젝트 알려줘", BUNDLES
        )
        self.assertEqual(action["action"], "SHOW_ALTERNATIVES")
        self.assertEqual(action["tasks"], ["EXPLAIN"])
        tasks, clarifies, _ = agent.apply_action_transition(state, action, BUNDLES)
        self.assertEqual(tasks, ["EXPLAIN"])
        self.assertEqual(clarifies, [])
        self.assertEqual(state["current_policy_id"], "JOB-1")
        self.assertIn("AGRI-1", state["_exclude_policy_ids"])

    def test_show_alternative_can_check_new_policy_eligibility(self):
        state = focused_state()
        action = agent.detect_navigation_action(
            state, "그거 말고 취업성공 프로젝트 나도 돼?", BUNDLES
        )
        self.assertEqual(action["action"], "SHOW_ALTERNATIVES")
        self.assertEqual(action["tasks"], ["ELIGIBILITY"])
        tasks, clarifies, _ = agent.apply_action_transition(state, action, BUNDLES)
        self.assertEqual(tasks, ["ELIGIBILITY"])
        self.assertEqual(clarifies, [])
        self.assertEqual(state["selected_policy_id"], "JOB-1")

    def test_result_ordinal_resolves_to_exact_policy(self):
        state = get_default_state()
        state["last_result_policy_ids"] = ["AGRI-1", "JOB-1"]
        action = agent.detect_navigation_action(
            state, "그중 두 번째 자격 봐줘", BUNDLES
        )
        self.assertEqual(action["action"], "FOLLOW_UP")
        self.assertEqual(action["tasks"], ["ELIGIBILITY"])
        self.assertEqual(action["policy_id"], "JOB-1")

    def test_unresolved_pronoun_never_hallucinates_policy(self):
        state = get_default_state()
        intent = {
            "action": "FOLLOW_UP",
            "turn_kind": "NEW_TASK",
            "reuse_focus": True,
            "use_previous_context": True,
            "confidence": "medium",
            "tasks": ["EXPLAIN"],
            "topic": None,
            "exclude_topics": [],
            "exclude_policy_mentions": [],
            "follow_up_field": "general",
            "policy_mention": None,
            "rewritten_query": "",
            "interest_query": None,
            "workflow": [{
                "action": "FOLLOW_UP",
                "task": "EXPLAIN",
                "policy_mention": None,
                "topic": None,
            }],
            "profile_patch": {},
            "clarify_reasons": ["CLARIFY_POLICY"],
        }
        with patch.object(agent, "call_openai", return_value=json.dumps(intent)):
            next_state, response = agent.handle_turn(
                state, "나 이거 설명해줘", None, BUNDLES
            )
        self.assertEqual(next_state["active_clarify"], "CLARIFY_POLICY")
        self.assertIn("어떤 정책", response)
        self.assertIsNone(next_state["current_policy_id"])

    def test_invalid_model_output_uses_intent_clarify_menu(self):
        state = focused_state()
        state["active_clarify"] = "CLARIFY_PREFERENCE"
        state["pending_tasks"] = ["RECOMMEND"]
        with patch.object(agent, "call_openai", return_value="not-json"):
            next_state, response = agent.handle_turn(
                state, "아니 새 질문인데 설명 요청도 아닌 이상한 말", None, BUNDLES
            )
        self.assertEqual(next_state["last_action"], "CLARIFY")
        self.assertEqual(next_state["last_intent_failure"], "INVALID_MODEL_OUTPUT")
        self.assertIsNone(next_state["active_clarify"])
        self.assertEqual(next_state["current_policy_id"], "AGRI-1")
        self.assertIn("정확히 이해하지 못했어요", response)
        self.assertIn("ACTION_BTN:NORMAL_RECOMMEND", response)

    def test_workflow_resolves_targets_before_show_alternative_transition(self):
        state = focused_state()
        state["_intent_workflow"] = [
            {
                "action": "FOLLOW_UP",
                "task": "EXPLAIN",
                "policy_mention": None,
                "topic": None,
                "use_previous_context": True,
            },
            {
                "action": "SHOW_ALTERNATIVES",
                "task": "RECOMMEND",
                "policy_mention": None,
                "topic": None,
                "use_previous_context": True,
            },
        ]
        workflow = agent.start_active_workflow(
            state,
            ["EXPLAIN", "RECOMMEND"],
            "이 정책 설명해주고 다른 건 없어?",
            bundles=BUNDLES,
        )
        self.assertEqual(workflow["steps"][0]["policy_id"], "AGRI-1")
        self.assertIn("AGRI-1", workflow["steps"][1]["exclude_policy_ids"])

        seen = []

        def fake_follow_up(current_state, bundles):
            seen.append(("EXPLAIN", current_state.get("current_policy_id")))
            return "기존 정책 설명"

        def fake_recommend(current_state, bundles):
            seen.append(("RECOMMEND", list(current_state.get("_exclude_policy_ids", []))))
            return "다른 정책 카드"

        with patch.object(agent, "run_policy_follow_up", side_effect=fake_follow_up), patch.object(
            agent, "run_recommend", side_effect=fake_recommend
        ):
            response = agent.execute_active_workflow(state, object(), BUNDLES)

        self.assertEqual(seen[0], ("EXPLAIN", "AGRI-1"))
        self.assertIn("AGRI-1", seen[1][1])
        self.assertIn("기존 정책 설명", response)
        self.assertIn("다른 정책 카드", response)

    def test_prompt_workflow_keeps_action_per_atomic_unit(self):
        state = focused_state()
        payload = {
            "action": "FOLLOW_UP",
            "turn_kind": "NEW_TASK",
            "reuse_focus": True,
            "use_previous_context": True,
            "confidence": "high",
            "tasks": ["EXPLAIN", "RECOMMEND"],
            "topic": None,
            "exclude_topics": [],
            "exclude_policy_mentions": [],
            "follow_up_field": None,
            "policy_mention": None,
            "rewritten_query": "",
            "interest_query": None,
            "workflow": [
                {
                    "action": "FOLLOW_UP",
                    "task": "EXPLAIN",
                    "policy_mention": None,
                    "topic": None,
                },
                {
                    "action": "SHOW_ALTERNATIVES",
                    "task": "RECOMMEND",
                    "policy_mention": None,
                    "topic": None,
                },
            ],
            "profile_patch": {},
            "clarify_reasons": [],
        }
        with patch.object(agent, "call_openai", return_value=json.dumps(payload)):
            tasks, _ = agent.analyze_user_turn(state, "이 정책 설명하고 다른 건 없어?")
        self.assertEqual(tasks, ["EXPLAIN", "RECOMMEND"])
        self.assertEqual(
            [step["action"] for step in state["_intent_workflow"]],
            ["FOLLOW_UP", "SHOW_ALTERNATIVES"],
        )

    def test_frontend_fallback_menu_uses_normal_action_backbone(self):
        with open("static/index.html", encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("ACTION_BTN:NORMAL_EXPLAIN", html)
        self.assertIn("ACTION_BTN:NORMAL_RECOMMEND", html)
        self.assertIn("ACTION_BTN:NORMAL_ELIGIBILITY", html)
        self.assertNotIn("action: 'CHECK_ELIGIBILITY'", html)


if __name__ == "__main__":
    unittest.main()
