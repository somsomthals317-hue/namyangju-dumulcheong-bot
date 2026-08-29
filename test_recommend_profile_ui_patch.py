from pathlib import Path

import agent
import sitecustomize
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


def test_fresh_normal_recommend_always_pauses_for_prefilled_profile_card():
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

    assert tasks == ["RECOMMEND"]
    assert clarifies == []
    assert direct is None

    response = agent.run_recommend(state, [])
    assert "맞춤 추천" in response
    assert state["active_clarify"] == "CLARIFY_PROFILE"
    assert state["pending_tasks"] == ["RECOMMEND"]
    assert state["profile"] == FULL_PROFILE


def test_explicit_explore_skips_profile_card_without_deleting_profile():
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
    assert state.get("active_clarify") != "CLARIFY_PROFILE"
    assert state["profile"] == FULL_PROFILE
    assert "정책" in response


def test_residency_is_semantic_only_for_ai_payload():
    original = dict(FULL_PROFILE)
    converted = sitecustomize._profile_for_ai(original)

    assert original["residency"] == "예"
    assert converted["residency"] == "남양주시 거주"

    converted_no = sitecustomize._profile_for_ai({"residency": "아니오"})
    assert converted_no["residency"] == "남양주시 비거주"


def test_navigation_sync_uses_personalized_submit_path_and_button_sync():
    script = Path("static/navigation_sync.js").read_text(encoding="utf-8")

    assert "postProfileAndRecommend(profile, false)" in script
    assert "st.active_clarify === 'CLARIFY_POLICY'" in script
    assert "pending[0] === 'EXPLAIN'" in script
    assert "pending[0] === 'ELIGIBILITY'" in script
    assert "st.active_clarify === 'CLARIFY_PREFERENCE'" in script
    assert "pending[0] === 'RECOMMEND'" in script
    assert "!hasSpecificPolicyCandidates(st)" in script
