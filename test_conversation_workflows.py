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
            [{"topic": "전체", "excluded_topics": ["교육"], "skip": None}],
        )
        self.assertIn("다른 분야 정책 카드", response)

        # GPT가 일반어인 "전체"를 의미상 관련 후보 없음으로 반환해도
        # 검증된 비-FAIL 후보에서 교육 분야를 제외한 카드가 복구되어야 한다.
        fallback_state = complete_state()
        fallback_state["last_recommendation_topic"] = "교육"
        fallback_state["last_recommendation_policy_ids"] = [
            "NYJ-YOUTH-018", "NYJ-YOUTH-019"
        ]
        fallback_state["current_policy_id"] = "NYJ-YOUTH-018"
        fallback_state["focus_policy_id"] = "NYJ-YOUTH-018"
        with patch.object(agent, "rerank_recommendations_with_ai", return_value=[]):
            fallback_state, fallback_response = agent.handle_turn(
                fallback_state,
                "이거 말고 다른 분야로 부탁할게",
                None,
                BUNDLES,
            )
        # 일반 대안/분야 전환은 즉시 추천을 실행하지 않고 저장된 Profile을
        # prefill한 확인 카드에서 멈춘다. 카드 제출 후에만 새 결과가 생성된다.
        self.assertNotIn("찾지 못했어요", fallback_response)
        self.assertEqual(fallback_state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(fallback_state["pending_tasks"], ["RECOMMEND"])
        self.assertEqual(fallback_state["last_result_policy_ids"], [])
        self.assertEqual(fallback_state["interest_query"], "전체")
        self.assertEqual(fallback_state.get("_exclude_topics"), ["교육"])

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

    def test_exact_user_explain_plus_recommend_is_atomic_before_prompt_a(self):
        state = complete_state()
        seen = []
        def fake_explain(current_state, query, collection):
            seen.append(("EXPLAIN", current_state.get("_policy_mention"), query))
            return "청년월세 설명"
        def fake_recommend(current_state, bundles):
            seen.append(("RECOMMEND", current_state.get("interest_query"), None))
            return "주거 추천"
        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(agent, "run_recommend", side_effect=fake_recommend), patch.object(agent, "call_openai", side_effect=AssertionError("Prompt A must not decide explicit multi-task routing")):
            next_state, response = agent.handle_turn(state, "청년월세 지원사업 설명해주고 주거 정책도 추천해줘", None, BUNDLES)
        self.assertEqual([item[0] for item in seen], ["EXPLAIN", "RECOMMEND"])
        self.assertEqual(seen[1][1], "주거")
        self.assertIn("청년월세 설명", response)
        self.assertIn("주거 추천", response)
        self.assertEqual(next_state["last_tasks"], ["EXPLAIN", "RECOMMEND"])

    def test_three_task_query_preserves_explain_recommend_eligibility_order(self):
        state = complete_state()
        seen = []
        def fake_explain(current_state, query, collection):
            seen.append("EXPLAIN")
            current_state["current_policy_id"] = "NYJ-YOUTH-001"
            current_state["focus_policy_id"] = "NYJ-YOUTH-001"
            return "월세 설명"
        def fake_recommend(current_state, bundles):
            seen.append("RECOMMEND")
            return "주거 추천"
        def fake_eligibility(current_state, bundles):
            seen.append("ELIGIBILITY")
            return "월세 자격 결과"
        with patch.object(agent, "run_explain", side_effect=fake_explain), patch.object(agent, "run_recommend", side_effect=fake_recommend), patch.object(agent, "run_eligibility", side_effect=fake_eligibility), patch.object(agent, "call_openai", side_effect=AssertionError("Prompt A must not collapse three explicit tasks")):
            next_state, response = agent.handle_turn(state, "청년월세 지원사업 설명해주고 주거 정책 추천해주고 청년월세 지원사업 자격도 확인해줘", None, BUNDLES)
        self.assertEqual(seen, ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"])
        self.assertEqual(next_state["last_tasks"], ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"])
        self.assertIn("월세 설명", response)
        self.assertIn("주거 추천", response)
        self.assertIn("월세 자격 결과", response)

    def test_final_eligibility_actions_exist_for_pass_without_additional(self):
        result = {"eligibility_status":"PASS","explanation":"조건을 충족했습니다.","matched_conditions":["만 19세 이상"],"failed_conditions":[],"missing_conditions":[]}
        response = agent.format_eligibility_response("청년성장 프로젝트", result, policy_id="NYJ-YOUTH-001", has_additional=False)
        self.assertIn("ACTION_BTN:RESET_PROFILE", response)
        self.assertIn("ACTION_BTN:NORMAL_ELIGIBILITY", response)
        self.assertIn("ACTION_BTN:RESET_CHAT", response)
        self.assertNotIn("ACTION_BTN:EDIT_ADDITIONAL", response)

    def test_final_eligibility_additional_edit_is_conditional(self):
        result = {"eligibility_status":"PASS","explanation":"조건을 충족했습니다.","matched_conditions":[],"failed_conditions":[],"missing_conditions":[]}
        response = agent.format_eligibility_response("테스트 정책", result, policy_id="NYJ-YOUTH-001", has_additional=True)
        self.assertIn("ACTION_BTN:EDIT_ADDITIONAL", response)
        self.assertIn("ACTION_BTN:RESET_PROFILE", response)
        self.assertIn("ACTION_BTN:NORMAL_ELIGIBILITY", response)
        self.assertIn("ACTION_BTN:RESET_CHAT", response)

    def test_additional_action_buttons_use_nonbreaking_two_column_layout(self):
        with open("static/index.html", "r", encoding="utf-8") as file:
            html = file.read()
        self.assertIn(".aq-action-row", html)
        self.assertIn("grid-template-columns:minmax(0,1fr) minmax(0,1fr)", html)
        self.assertIn("white-space:nowrap", html)
        self.assertIn('class="aq-action-btn"', html)

    def test_two_task_profile_pause_resume_keeps_explain_in_final_answer(self):
        state = complete_state()
        with patch.object(agent, "run_explain", return_value="월세 정책 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책도 추천해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["active_workflow"]["results"]["EXPLAIN"], "월세 정책 설명")

        with patch.object(agent, "run_recommend", return_value="주거 추천 결과"):
            state, final = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertIsNone(state["active_workflow"])
        self.assertIn("월세 정책 설명", final)
        self.assertIn("주거 추천 결과", final)

    def test_three_task_profile_pause_resume_reaches_eligibility_clarify(self):
        state = complete_state()
        with patch.object(agent, "run_explain", return_value="월세 정책 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책 추천해주고 청년월세 지원사업 자격도 확인해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["pending_tasks"], ["RECOMMEND", "ELIGIBILITY"])

        def fake_eligibility(current_state, bundles):
            current_state["active_clarify"] = "CLARIFY_ADDITIONAL"
            current_state["_active_additional_q"] = {
                "policy_id": "NYJ-YOUTH-011",
                "policy_name": "청년월세 지원사업",
                "questions": [{"question_id": "q1", "question": "추가 조건?", "options": ["예", "아니오"], "q_num": 1}],
                "total": 1,
                "batch_start": 1,
                "batch_end": 1,
                "remaining": 1,
            }
            return "추가 자격 확인이 필요해요."

        with patch.object(agent, "run_recommend", return_value="주거 추천 결과"), patch.object(
            agent, "run_eligibility", side_effect=fake_eligibility
        ):
            state, second = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_ADDITIONAL")
        self.assertEqual(state["pending_tasks"], ["ELIGIBILITY"])
        self.assertEqual(state["active_workflow"]["index"], 2)
        self.assertIn("정책 설명과 맞춤 추천", second)

    def test_result_profile_rerun_restores_completed_multi_query_contract(self):
        state = complete_state()
        # 첫 턴은 실제 run_recommend를 사용해 추천 Profile 확인에서 멈춘다.
        with patch.object(agent, "run_explain", return_value="원래 월세 설명"):
            state, first = agent.handle_turn(
                state,
                "청년월세 지원사업 설명해주고 주거 정책도 추천해줘",
                None,
                BUNDLES,
            )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertEqual(state["active_workflow"]["results"]["EXPLAIN"], "원래 월세 설명")

        # Profile 제출로 첫 멀티쿼리를 정상 완료한다.
        with patch.object(agent, "run_recommend", return_value="첫 주거 추천"):
            state, first_final = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={"action": "NORMAL", "tasks": ["RECOMMEND"], "topic": "주거"},
            )
        self.assertIsNone(state["active_workflow"])
        self.assertIn("원래 월세 설명", first_final)
        self.assertIn("첫 주거 추천", first_final)
        self.assertIsInstance(state.get("_last_completed_workflow"), dict)

        # 결과의 프로필 다시 설정하기에서 재제출하면 EXPLAIN은 보존하고
        # RECOMMEND부터 다시 실행해 최종 묶음 응답을 만든다.
        with patch.object(agent, "run_recommend", return_value="수정 Profile 주거 추천"):
            state, rerun = agent.handle_turn(
                state,
                "",
                None,
                BUNDLES,
                ui_event="SUBMIT_RECOMMEND_PROFILE",
                input_action={
                    "action": "NORMAL",
                    "tasks": ["RECOMMEND"],
                    "topic": "주거",
                    "resume_multi_workflow": True,
                },
            )
        self.assertIn("원래 월세 설명", rerun)
        self.assertIn("수정 Profile 주거 추천", rerun)
        self.assertEqual(state["last_tasks"], ["EXPLAIN", "RECOMMEND"])

    def test_recommend_profile_frontend_always_renders_following_workflow_cards(self):
        with open("static/index.html", "r", encoding="utf-8") as file:
            html = file.read()
        submit_block = html[html.index("async function submitRecommendProfile"):html.index("function reopenRecommendProfile")]
        explore_block = html[html.index("async function exploreWithoutProfile"):html.index("function onInterestSelected")]
        self.assertIn("renderResponseWithCard(data)", submit_block)
        self.assertIn("renderResponseWithCard(data)", explore_block)
        self.assertIn("resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile", submit_block)

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

    def test_natural_profile_sentence_captures_age_and_residency_together(self):
        state = get_default_state()
        state, first = agent.handle_turn(
            state, "청년기본소득 자격 확인해줘", None, BUNDLES
        )
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertIn("자격 확인을 위해", first)

        state, second = agent.handle_turn(
            state, "만 24세고 남양주시에 거주하고 있어", None, BUNDLES
        )

        self.assertEqual(state["profile"]["age"], 24)
        self.assertEqual(state["profile"]["residency"], "예")
        self.assertEqual(state["active_clarify"], "CLARIFY_ADDITIONAL")
        self.assertNotIn("남양주시 거주", second)

    def test_natural_multi_answer_consumes_all_basic_income_questions(self):
        state = get_default_state()
        state, _ = agent.handle_turn(
            state, "청년기본소득 자격 확인해줘", None, BUNDLES
        )
        state, _ = agent.handle_turn(
            state, "만 24세고 남양주시에 거주하고 있어", None, BUNDLES
        )

        with patch.object(agent, "review_eligibility_with_ai", return_value=None):
            state, response = agent.handle_turn(
                state,
                "경기도에 계속 3년 넘게 살았고, 합산은 10년이 안 돼. "
                "생년월일은 2002-05-01이야",
                None,
                BUNDLES,
            )

        answers = state["policy_answers"]["NYJ-YOUTH-010"]
        self.assertEqual(answers["gyeonggi_three_years"], "예")
        self.assertEqual(answers["gyeonggi_ten_years"], "아니오")
        self.assertEqual(answers["birth_date"], "2002-05-01")
        self.assertIsNone(state["active_clarify"])
        self.assertIn("거주기간 요건(3년 계속 또는 합산 10년)을 충족", response)

    def test_eligibility_particle_suffix_overrides_explain_misclassification(self):
        state = complete_state()
        wrong_model_payload = {
            "action": "SEARCH_POLICY",
            "turn_kind": "NEW_TASK",
            "reuse_focus": False,
            "use_previous_context": False,
            "confidence": "high",
            "tasks": ["EXPLAIN"],
            "topic": None,
            "policy_mention": "청년기본소득",
            "workflow": [{
                "action": "SEARCH_POLICY",
                "task": "EXPLAIN",
                "policy_mention": "청년기본소득",
                "topic": None,
            }],
            "profile_patch": {},
            "clarify_reasons": [],
        }
        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(wrong_model_payload, ensure_ascii=False),
        ):
            state, response = agent.handle_turn(
                state,
                "추천한 청년기본소득 자격도 확인해줘",
                None,
                BUNDLES,
            )

        self.assertEqual(state["selected_policy_id"], "NYJ-YOUTH-010")
        self.assertEqual(state["active_clarify"], "CLARIFY_ADDITIONAL")
        self.assertIn("추가 자격 조건 확인", response)
        self.assertNotIn("[정책 설명]", response)

    def test_intra_query_pronoun_reuses_explain_policy_for_eligibility(self):
        state = get_default_state()
        model_payload = {
            "action": "RUN_WORKFLOW",
            "turn_kind": "NEW_TASK",
            "reuse_focus": False,
            "use_previous_context": False,
            "confidence": "high",
            "tasks": ["EXPLAIN", "ELIGIBILITY"],
            "topic": None,
            "policy_mention": "입영지원금",
            # 실서비스에서 재현된 잘못된 모델 출력: 두 번째 대명사 step을
            # FOLLOW_UP이 아닌 NORMAL+대상 없음으로 반환했다.
            "workflow": [
                {
                    "action": "NORMAL",
                    "task": "EXPLAIN",
                    "policy_mention": "입영지원금",
                    "topic": None,
                },
                {
                    "action": "NORMAL",
                    "task": "ELIGIBILITY",
                    "policy_mention": None,
                    "topic": None,
                    "use_previous_context": False,
                },
            ],
            "profile_patch": {},
            "clarify_reasons": [],
        }
        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(model_payload, ensure_ascii=False),
        ), patch.object(agent, "run_explain", return_value="입영지원금 설명"):
            state, response = agent.handle_turn(
                state,
                "입영지원금 정책 설명해주고 내가 자격되는지도 확인해줘",
                None,
                BUNDLES,
            )

        workflow = state["active_workflow"]
        self.assertEqual(
            [step["policy_id"] for step in workflow["steps"]],
            ["NYJ-YOUTH-016", "NYJ-YOUTH-016"],
        )
        self.assertEqual(workflow["index"], 1)
        self.assertEqual(state["active_clarify"], "CLARIFY_PROFILE")
        self.assertNotIn("어떤 정책의 자격", response)
        self.assertIn("입영지원금 지원 자격 확인", response)

    def test_three_task_pause_narrates_explain_recommend_and_eligibility(self):
        state = get_default_state()
        with patch.object(
            agent,
            "call_openai",
            return_value=json.dumps(incomplete_recommend_intent("복지"), ensure_ascii=False),
        ), patch.object(agent, "run_explain", return_value="입영지원금 설명"), patch.object(
            agent, "run_recommend", return_value="복지 추천 카드"
        ):
            state, response = agent.handle_turn(
                state,
                "입영지원금 지원 설명해주고, 복지 정책을 조건 없이 추천해주고, "
                "청년월세 지원사업 자격도 확인해줘",
                None,
                BUNDLES,
            )

        self.assertEqual(
            [step["task"] for step in state["active_workflow"]["steps"]],
            ["EXPLAIN", "RECOMMEND", "ELIGIBILITY"],
        )
        self.assertIn("정책 설명과 맞춤 추천은 준비해두었어요", response)
        self.assertIn("자격 확인에 필요한 정보", response)

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
