import json
import unittest
from unittest.mock import patch

import agent
from data_loader import (
    build_policy_bundles,
    load_eligibility_rules,
    load_summary_documents,
)
from state import get_default_state


BUNDLES = build_policy_bundles(
    load_summary_documents(),
    load_eligibility_rules(),
)


def complete_state():
    state = get_default_state()
    state["profile"].update({
        "age": 24,
        "residency": "예",
        "employment": "미취업",
        "student": "아니오",
        "startup": "미창업",
        "housing": "무주택",
        "marriage": "미혼",
    })
    state["profile_status"] = "COMPLETE"
    return state


def incomplete_recommend_intent(topic="복지"):
    """실서비스에서 재현된 것처럼 GPT가 추천 Task만 반환한 payload."""
    return {
        "action": "NORMAL",
        "turn_kind": "NEW_TASK",
        "reuse_focus": False,
        "use_previous_context": False,
        "confidence": "high",
        "tasks": ["RECOMMEND"],
        "topic": topic,
        "exclude_topics": [],
        "exclude_policy_mentions": [],
        "explore_without_profile": False,
        "follow_up_field": None,
        "policy_mention": None,
        "rewritten_query": topic,
        "interest_query": topic,
        "workflow": [{
            "action": "NORMAL",
            "task": "RECOMMEND",
            "policy_mention": None,
            "topic": topic,
        }],
        "profile_patch": {},
        "clarify_reasons": [],
    }


class ConversationWorkflowRegressionTests(unittest.TestCase):
    def test_profile_and_conditionless_buttons_resume_active_workflow(self):
        with open("static/index.html", "r", encoding="utf-8") as file:
            html = file.read()

        explore_block = html[html.index("async function exploreWithoutProfile"):html.index("function onInterestSelected")]
        recommend_block = html[html.index("async function postProfileAndRecommend"):html.index("function showInterestCardsInChat")]
        self.assertIn("ui_event: 'SUBMIT_RECOMMEND_PROFILE'", explore_block)
        self.assertIn("ui_event: 'SUBMIT_RECOMMEND_PROFILE'", recommend_block)

    def test_model_task_omission_is_recovered_for_user_example(self):
        state = complete_state()
        seen = []

        def fake_explain(current_state, query, collection):
            seen.append(("EXPLAIN", current_state.get("current_policy_id"), query))
            return "입영지원금 설명 본문"

        def fake_recommend(current_state, bundles):
            seen.append((
                "RECOMMEND",
                current_state.get("interest_query"),
                current_state.get("_recommend_query"),
            ))
            return "복지 추천 카드"

        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(incomplete_recommend_intent(), ensure_ascii=False),
        ), patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(
            agent, "run_recommend", side_effect=fake_recommend
        ):
            next_state, response = agent.handle_turn(
                state,
                "안녕 나 입영지원금 설명해주고, 복지 분야에서 정책 하나 추천해줘",
                None,
                BUNDLES,
            )

        self.assertEqual([item[0] for item in seen], ["EXPLAIN", "RECOMMEND"])
        self.assertEqual(seen[0][1], "NYJ-YOUTH-016")
        self.assertEqual(seen[1][1], "복지")
        self.assertEqual(seen[1][2], "복지")
        self.assertIn("먼저 입영지원금 지원에 대해 설명해드릴게요", response)
        self.assertIn("이어서 복지 분야에서 정책을 추천해드릴게요", response)
        self.assertIn("입영지원금 설명 본문", response)
        self.assertIn("복지 추천 카드", response)
        self.assertEqual(next_state["last_tasks"], ["EXPLAIN", "RECOMMEND"])

    def test_broad_explain_then_recommend_pauses_without_dropping_second_task(self):
        state = complete_state()

        def fake_explain(current_state, query, collection):
            current_state["active_clarify"] = "CLARIFY_POLICY"
            current_state["_policy_candidates"] = [
                {"policy_id": "NYJ-YOUTH-020", "policy_name": "청년창업 교육프로그램"},
                {"policy_id": "NYJ-YOUTH-021", "policy_name": "청년창업 컨설팅"},
            ]
            return "창업 정책이 여러 개예요. 설명할 정책을 선택해주세요."

        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(incomplete_recommend_intent("농업"), ensure_ascii=False),
        ), patch.object(agent, "run_explain", side_effect=fake_explain):
            next_state, response = agent.handle_turn(
                state,
                "창업 정책 설명해주고, 농업 분야에서 추천해줘",
                None,
                BUNDLES,
            )

        workflow = next_state["active_workflow"]
        self.assertEqual(
            [step["task"] for step in workflow["steps"]],
            ["EXPLAIN", "RECOMMEND"],
        )
        self.assertEqual(workflow["steps"][0]["policy_mention"], "창업 정책")
        self.assertEqual(workflow["steps"][1]["topic"], "농업")
        self.assertEqual(next_state["pending_tasks"], ["EXPLAIN", "RECOMMEND"])
        self.assertIn("먼저 설명할 정책을 정확히 선택해주세요", response)
        self.assertIn("그다음 농업 분야 추천까지 이어갈게요", response)

    def test_broad_explain_selection_then_profile_card_returns_one_combined_answer(self):
        state = get_default_state()

        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(incomplete_recommend_intent("복지"), ensure_ascii=False),
        ):
            state, first = agent.handle_turn(
                state,
                "농업 정책에 대해서 설명해주고, 복지 분야에서 정책 하나 추천해줘",
                None,
                BUNDLES,
            )

        self.assertEqual(state["active_clarify"], "CLARIFY_POLICY")
        self.assertEqual(state["pending_tasks"], ["EXPLAIN", "RECOMMEND"])
        self.assertTrue(state.get("_policy_candidates"))
        self.assertIn("어떤 정책을 알고 싶으신가요", first)
        self.assertIn("그다음 복지 분야 추천까지 이어갈게요", first)
        selected_name = state["_policy_candidates"][0]["policy_name"]

        with patch.object(agent, "run_explain", return_value="선택한 농업 정책 설명"):
            state, second = agent.handle_turn(state, selected_name, None, BUNDLES)

        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["pending_tasks"], ["RECOMMEND"])
        self.assertEqual(state["active_workflow"]["results"]["EXPLAIN"], "선택한 농업 정책 설명")
        self.assertIn("정책 설명은 준비해두었어요", second)
        self.assertIn("맞춤 추천을 위해 몇 가지 여쭤볼게요", second)

        state["profile"].update(complete_state()["profile"])
        state["profile_status"] = "COMPLETE"
        with patch.object(agent, "run_recommend", return_value="복지 추천 카드"):
            state, final = agent.handle_turn(
                state,
                "프로필 입력 완료",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
            )

        self.assertIsNone(state["active_workflow"])
        self.assertIn("[정책 설명]", final)
        self.assertIn("선택한 농업 정책 설명", final)
        self.assertIn("[맞춤 추천 결과]", final)
        self.assertIn("복지 추천 카드", final)

    def test_other_field_after_recommendation_excludes_previous_topic_and_shows_cards(self):
        state = complete_state()
        state["last_recommendation_topic"] = "교육"
        state["last_recommendation_policy_ids"] = ["NYJ-YOUTH-018", "NYJ-YOUTH-019"]
        state["current_policy_id"] = "NYJ-YOUTH-018"
        state["focus_policy_id"] = "NYJ-YOUTH-018"
        seen = []

        def fake_recommend(current_state, bundles):
            seen.append({
                "topic": current_state.get("interest_query"),
                "excluded_topics": list(current_state.get("_exclude_topics") or []),
                "skip": current_state.get("_skip_profile_check"),
            })
            current_state["last_result_policy_ids"] = ["NYJ-YOUTH-016"]
            return "교육을 제외한 다른 분야 정책 카드"

        action = agent.detect_navigation_action(
            state, "이거 말고 다른 분야로 부탁할게", BUNDLES
        )
        self.assertEqual(action["action"], "SHOW_ALTERNATIVES")
        self.assertEqual(action["tasks"], ["RECOMMEND"])
        self.assertEqual(action["exclude_topics"], ["교육"])

        with patch.object(agent, "run_recommend", side_effect=fake_recommend):
            state, response = agent.handle_turn(
                state,
                "이거 말고 다른 분야로 부탁할게",
                None,
                BUNDLES,
            )

        self.assertEqual(
            seen,
            [{"topic": "전체", "excluded_topics": ["교육"], "skip": True}],
        )
        self.assertIn("다른 분야 정책 카드", response)

    def test_atomic_target_extraction_covers_mixed_task_orders(self):
        cases = [
            (
                "입영지원금 설명하고 복지 정책 추천해줘",
                [("EXPLAIN", "입영지원금", None), ("RECOMMEND", None, "복지")],
            ),
            (
                "농업 정책 추천해주고 입영지원금 자격 확인해줘",
                [("RECOMMEND", None, "농업"), ("ELIGIBILITY", "입영지원금", None)],
            ),
            (
                "청년월세 설명하고 나도 가능한지 확인해줘",
                [("EXPLAIN", "청년월세", None), ("ELIGIBILITY", None, None)],
            ),
            (
                "취업 정책 추천하고 그중 하나 설명해줘",
                [("RECOMMEND", None, "취업"), ("EXPLAIN", "그중 하나", None)],
            ),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                steps = agent.infer_atomic_workflow_from_message(
                    complete_state(), query, BUNDLES
                )
                actual = [
                    (step["task"], step.get("policy_mention"), step.get("topic"))
                    for step in steps
                ]
                self.assertEqual(actual, expected)

    def test_generic_alternative_excludes_all_visible_recommendations(self):
        state = complete_state()
        state["current_topic"] = "농업"
        state["interest_query"] = "농업"
        state["last_result_policy_ids"] = ["NYJ-YOUTH-006", "NYJ-YOUTH-017"]
        action = agent.detect_navigation_action(state, "이거 말고 다른 거", BUNDLES)
        self.assertEqual(action["action"], "SHOW_ALTERNATIVES")
        self.assertEqual(action["tasks"], ["RECOMMEND"])
        self.assertEqual(
            set(action["exclude_policy_ids"]),
            {"NYJ-YOUTH-006", "NYJ-YOUTH-017"},
        )
        _, clarifies, _ = agent.apply_action_transition(state, action, BUNDLES)
        self.assertEqual(clarifies, [])
        self.assertEqual(state["interest_query"], "농업")

    def test_natural_conditionless_recommendation_keeps_explore_mode(self):
        state = get_default_state()
        seen = []

        def fake_recommend(current_state, bundles):
            seen.append({
                "skip": current_state.get("_skip_profile_check"),
                "explore": current_state.get("_explore_mode"),
                "topic": current_state.get("interest_query"),
            })
            current_state["last_result_policy_ids"] = ["NYJ-YOUTH-017"]
            return "농업 정책 카드"

        with patch.object(agent, "run_recommend", side_effect=fake_recommend):
            next_state, response = agent.handle_turn(
                state,
                "농업 정책 조건 없이 추천해줘",
                None,
                BUNDLES,
            )

        self.assertEqual(
            seen,
            [{"skip": True, "explore": True, "topic": "농업"}],
        )
        self.assertIn("농업 정책 카드", response)
        self.assertEqual(next_state["last_result_policy_ids"], ["NYJ-YOUTH-017"])

    def test_multi_response_follows_user_task_order(self):
        steps = [
            {"task": "RECOMMEND", "topic": "농업"},
            {"task": "EXPLAIN", "policy_mention": "입영지원금"},
        ]
        response = agent.compose_multi_response(
            {"EXPLAIN": "설명", "RECOMMEND": "추천"},
            steps=steps,
            bundles=BUNDLES,
        )
        self.assertLess(response.index("[맞춤 추천 결과]"), response.index("[정책 설명]"))


if __name__ == "__main__":
    unittest.main()
