"""32개 정책의 자연어·버튼·전환 동작을 전수 검증한다.

외부 API 호출 없이 Action → Task → State → 정책 실행기의 결정론적 계약을
검증한다. GPT 의미 판정 자체의 JSON 계약은 test_hardening.py에서 별도로
검증하고, 여기서는 GPT 장애 시 규칙 복구까지 포함한 사용자 흐름을 검사한다.
"""
import unittest
from unittest.mock import patch

import agent
from data_loader import get_all_data
from state import get_default_state


INTERESTS = (
    "취업", "교육", "농업", "복지", "기본소득",
    "주거", "창업", "참여·문화", "전체",
)


class CatalogCollection:
    """정확한 policy_id 조회를 지원하는 전수 테스트용 VectorStore 대역."""

    def __init__(self, origins):
        self._items = {
            item["policy_id"]: {
                "document": item.get("content", ""),
                "metadata": {
                    "policy_name": item.get("policy_name", ""),
                    "category": item.get("category", ""),
                    "source": item.get("source", ""),
                },
            }
            for item in origins
        }

    def count(self):
        return len(self._items)

    def get(self, ids, include=None):
        found_ids, documents, metadatas = [], [], []
        for policy_id in ids:
            item = self._items.get(policy_id)
            if not item:
                continue
            found_ids.append(policy_id)
            documents.append(item["document"])
            metadatas.append(item["metadata"])
        return {"ids": found_ids, "documents": documents, "metadatas": metadatas}

    def query(self, query_texts, n_results=5, include=None):
        ids = list(self._items)[:n_results]
        return {
            "ids": [ids],
            "documents": [[self._items[item]["document"] for item in ids]],
            "metadatas": [[self._items[item]["metadata"] for item in ids]],
            "distances": [[1.0 for _ in ids]],
        }


class PolicyAgentMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = get_all_data()
        cls.bundles = data["bundles"]
        cls.origins = data["origin_docs"]
        cls.collection = CatalogCollection(cls.origins)
        cls.full = [item for item in cls.bundles if item.get("eligibility_mode") != "INFO_ONLY"]
        cls.info_only = [item for item in cls.bundles if item.get("eligibility_mode") == "INFO_ONLY"]

    def test_catalog_partition_is_32_explain_25_eligibility_7_info_only(self):
        self.assertEqual(32, len(self.bundles))
        self.assertEqual(25, len(self.full))
        self.assertEqual(7, len(self.info_only))
        self.assertEqual(
            {item["policy_id"] for item in self.full},
            {f"NYJ-YOUTH-{number:03d}" for number in range(1, 26)},
        )

    def test_natural_explain_resolves_every_policy_and_official_link(self):
        for bundle in self.bundles:
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "call_openai", side_effect=AssertionError("정확한 정책명은 GPT 분류가 불필요합니다.")
            ):
                state, response = agent.handle_turn(
                    get_default_state(), f"{bundle['policy_name']} 설명해줘",
                    self.collection, self.bundles,
                )
                self.assertEqual(bundle["policy_id"], state["focus_policy_id"])
                self.assertEqual("EXPLAIN", state["last_task"])
                self.assertIn(bundle["policy_name"], response)
                self.assertIn("https://www.nyj.go.kr/", response)

    def test_explain_button_resolves_every_policy(self):
        for bundle in self.bundles:
            action = {
                "action": "NORMAL", "tasks": ["EXPLAIN"],
                "policy_id": bundle["policy_id"], "policy_mention": bundle["policy_name"],
                "use_previous_context": False, "confidence": "high",
            }
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "call_openai", side_effect=AssertionError("버튼은 GPT Intent를 거치지 않습니다.")
            ):
                state, response = agent.handle_turn(
                    get_default_state(), f"{bundle['policy_name']} 정책에 대해 알려줘",
                    self.collection, self.bundles, input_action=action,
                )
                self.assertEqual(bundle["policy_id"], state["focus_policy_id"])
                self.assertEqual("NORMAL", state["last_action"])
                self.assertIn(bundle["policy_name"], response)

    def test_natural_eligibility_resolves_all_25_application_policies(self):
        for bundle in self.full:
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "review_eligibility_with_ai", return_value=None
            ), patch.object(
                agent, "call_openai", side_effect=AssertionError("명시 자격 요청은 GPT Intent가 불필요합니다.")
            ):
                state, response = agent.handle_turn(
                    get_default_state(),
                    f"{bundle['policy_name']} 내가 신청 가능한지 자격 확인해줘",
                    self.collection, self.bundles,
                )
                self.assertEqual(bundle["policy_id"], state["selected_policy_id"])
                self.assertEqual(bundle["policy_id"], state["focus_policy_id"])
                self.assertNotIn("[정책 설명]", response)
                self.assertNotIn("지원내용:", response)
                self.assertIn("공식 정책 페이지", response)

    def test_eligibility_button_resolves_all_25_application_policies(self):
        for bundle in self.full:
            action = {
                "action": "NORMAL", "tasks": ["ELIGIBILITY"],
                "policy_id": bundle["policy_id"], "policy_mention": bundle["policy_name"],
                "use_previous_context": False, "confidence": "high",
            }
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "review_eligibility_with_ai", return_value=None
            ), patch.object(
                agent, "call_openai", side_effect=AssertionError("버튼은 GPT Intent를 거치지 않습니다.")
            ):
                state, response = agent.handle_turn(
                    get_default_state(), f"{bundle['policy_name']} 자격 확인해줘",
                    self.collection, self.bundles, input_action=action,
                )
                self.assertEqual(bundle["policy_id"], state["selected_policy_id"])
                self.assertNotIn("[정책 설명]", response)

    def test_info_only_policies_explain_why_pass_fail_is_unavailable(self):
        for bundle in self.info_only:
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "call_openai", side_effect=AssertionError("정확한 정책명은 GPT Intent가 불필요합니다.")
            ):
                state, response = agent.handle_turn(
                    get_default_state(),
                    f"{bundle['policy_name']} 나도 이용할 수 있는지 자격 확인해줘",
                    self.collection, self.bundles,
                )
                self.assertEqual(bundle["policy_id"], state["selected_policy_id"])
                self.assertEqual("INFO_ONLY", state["_last_eligibility_mode"])
                self.assertIn("PASS/FAIL로 판정할 수 없어요", response)
                self.assertIn("공식 정책 페이지", response)

    def test_natural_follow_up_recommendation_works_from_every_policy(self):
        for bundle in self.bundles:
            state = get_default_state()
            state.update({
                "current_policy_id": bundle["policy_id"],
                "focus_policy_id": bundle["policy_id"],
                "_skip_profile_check": True,
                "_explore_mode": True,
            })
            with self.subTest(policy_id=bundle["policy_id"]), patch.object(
                agent, "rerank_recommendations_with_ai", return_value=None
            ), patch.object(
                agent, "call_openai", side_effect=AssertionError("명시적 후속 추천은 GPT Intent가 불필요합니다.")
            ):
                state, response = agent.handle_turn(
                    state, "이 정책과 비슷한 정책 추천해줘",
                    self.collection, self.bundles,
                )
                self.assertEqual("FOLLOW_UP", state["last_action"])
                self.assertEqual("RECOMMEND", state["last_task"])
                self.assertTrue(state["current_topic"])
                self.assertTrue(state["last_result_policy_ids"])
                self.assertIn("정책", response)

    def test_recommend_interest_buttons_use_normal_recommend_action(self):
        for interest in INTERESTS:
            action = {
                "action": "NORMAL", "tasks": ["RECOMMEND"], "topic": interest,
                "use_previous_context": False, "confidence": "high",
                "explore_without_profile": True,
            }
            state = get_default_state()
            with self.subTest(interest=interest), patch.object(
                agent, "rerank_recommendations_with_ai", return_value=None
            ), patch.object(
                agent, "call_openai", side_effect=AssertionError("추천 버튼은 GPT Intent를 거치지 않습니다.")
            ):
                state, response = agent.handle_turn(
                    state, f"{interest} 분야 정책 추천해줘",
                    self.collection, self.bundles, input_action=action,
                )
                self.assertEqual("NORMAL", state["last_action"])
                self.assertEqual("RECOMMEND", state["last_task"])
                self.assertEqual(interest, state["current_topic"])
                self.assertTrue(state["last_result_policy_ids"])
                self.assertIn("정책", response)

    def test_recommendation_never_renders_eligibility_button_for_info_only(self):
        results = agent.build_grounded_recommendations(
            self.bundles,
            {key: None for key in get_default_state()["profile"]},
            {}, "전체",
        )
        response = agent.format_explore_response(results, "전체")
        for bundle in self.info_only:
            with self.subTest(policy_id=bundle["policy_id"]):
                item = next(result for result in results if result["policy_id"] == bundle["policy_id"])
                single_response = agent.format_explore_response([item], "전체")
                self.assertNotIn(f"[ELIG_BTN:{bundle['policy_id']}:", single_response)
        for bundle in self.full:
            with self.subTest(policy_id=bundle["policy_id"]):
                item = next(result for result in results if result["policy_id"] == bundle["policy_id"])
                single_response = agent.format_explore_response([item], "전체")
                self.assertIn(f"[ELIG_BTN:{bundle['policy_id']}:", single_response)


class TransitionAndSafetyMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundles = get_all_data()["bundles"]

    def _state(self):
        state = get_default_state()
        state.update({
            "current_policy_id": "NYJ-YOUTH-017",
            "focus_policy_id": "NYJ-YOUTH-017",
            "current_topic": "농업",
            "interest_query": "농업",
        })
        return state

    def test_six_high_risk_navigation_sentences(self):
        cases = (
            ("그 정책 신청 기간은?", "FOLLOW_UP", "EXPLAIN"),
            ("그 정책 나도 신청할 수 있어?", "FOLLOW_UP", "ELIGIBILITY"),
            ("이 정책과 비슷한 정책 추천해줘", "FOLLOW_UP", "RECOMMEND"),
            ("농업 말고 취업 정책은?", "CHANGE_TOPIC", "RECOMMEND"),
            ("그거 말고 다른 거", "SHOW_ALTERNATIVES", "RECOMMEND"),
            ("그거 말고 취업성공 프로젝트 설명해줘", "SHOW_ALTERNATIVES", "EXPLAIN"),
        )
        for text, expected_action, expected_task in cases:
            with self.subTest(text=text):
                action = agent.detect_navigation_action(self._state(), text, self.bundles)
                self.assertEqual(expected_action, action["action"])
                self.assertEqual([expected_task], action["tasks"])

    def test_five_unrecognized_inputs_use_intent_clarify_menu(self):
        cases = ("...???", "ㅋㅋㅋㅋㅋㅋ", "asdf qwer zxcv", "🍕🚀🧩", "그냥 알아서 뭐든")
        for text in cases:
            with self.subTest(text=text):
                state = self._state()
                with patch.object(agent, "call_openai", return_value="not-json"):
                    state, response = agent.handle_turn(state, text, None, self.bundles)
                self.assertIn("정확히 이해하지 못했어요", response)
                self.assertIn("무엇을 도와드릴까요?", response)
                self.assertIn("NORMAL_EXPLAIN", response)
                self.assertIn("NORMAL_RECOMMEND", response)
                self.assertIn("NORMAL_ELIGIBILITY", response)
                self.assertEqual("CLARIFY", state["last_action"])

    def test_multi_query_atomic_targets_are_resolved_before_transition(self):
        state = self._state()
        state["_intent_workflow"] = [
            {"action": "NORMAL", "task": "EXPLAIN", "policy_mention": "청년기본소득", "topic": None},
            {
                "action": "SHOW_ALTERNATIVES", "task": "RECOMMEND",
                "policy_mention": None, "topic": "농업",
                "exclude_policy_mentions": ["청년기본소득"],
            },
            {
                "action": "NORMAL", "task": "ELIGIBILITY",
                "policy_mention": "청년농업인 영농정착 지원", "topic": None,
            },
        ]
        agent.start_active_workflow(
            state, ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"],
            "기본소득 설명하고 다른 농업 정책 추천한 뒤 영농정착 자격도 봐줘",
            bundles=self.bundles,
        )
        steps = state["active_workflow"]["steps"]
        self.assertEqual("NYJ-YOUTH-010", steps[0]["policy_id"])
        self.assertIn("NYJ-YOUTH-010", steps[1]["exclude_policy_ids"])
        self.assertEqual("NYJ-YOUTH-017", steps[2]["policy_id"])
        self.assertEqual(
            ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"],
            [step["task"] for step in steps],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
