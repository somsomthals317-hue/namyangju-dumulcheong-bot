"""Runtime compatibility fixes for recommendation/profile UI contracts.

This module is imported automatically by Python's site initialization.  Keep
patches deliberately narrow so the existing Agent/RAG implementation remains
unchanged while UI state contracts are corrected.
"""

try:
    import agent as _agent
    from state import get_missing_profile_fields
except Exception:  # pragma: no cover - do not block interpreter startup
    _agent = None


if _agent is not None:
    _original_apply_action_transition = _agent.apply_action_transition
    _original_run_recommend = _agent.run_recommend
    _original_judge_recommendations_with_ai = _agent.judge_recommendations_with_ai
    _original_review_eligibility_with_ai = _agent.review_eligibility_with_ai

    def _profile_for_ai(profile):
        """Give GPT semantic residency text without mutating canonical State."""
        result = dict(profile or {})
        if result.get("residency") == "예":
            result["residency"] = "남양주시 거주"
        elif result.get("residency") == "아니오":
            result["residency"] = "남양주시 비거주"
        return result

    def _patched_apply_action_transition(state, action, bundles, preserve_workflow=False):
        result = _original_apply_action_transition(
            state, action, bundles, preserve_workflow=preserve_workflow
        )
        if isinstance(action, dict) and "RECOMMEND" in (action.get("tasks") or []):
            kind = action.get("action")
            explicit_explore = action.get("explore_without_profile") is True

            # A fresh NORMAL recommendation should always let the user confirm
            # the stored Profile in the pre-filled recommendation card first.
            # Follow-up/alternative/topic-change requests keep using the saved
            # Profile directly unless information is actually missing.
            if kind == "NORMAL" and not explicit_explore:
                state["_confirm_profile_before_recommend"] = True
            elif explicit_explore:
                state.pop("_confirm_profile_before_recommend", None)
        return result

    def _patched_run_recommend(state, bundles):
        profile = state.get("profile", {})
        explicit_explore = bool(state.get("_explore_mode"))
        resume_after_card = bool(state.get("_skip_profile_check"))
        confirm_first = bool(state.get("_confirm_profile_before_recommend"))
        missing = get_missing_profile_fields(profile)

        # The frontend renders the recommendation Profile card only when both
        # active_clarify and pending_tasks are present.  The previous code could
        # return the clarify sentence without those state values, so only a
        # speech bubble appeared.  Also show the card for a fresh NORMAL
        # recommendation even when Profile is already complete, so users can
        # review/edit the pre-filled values.
        if not explicit_explore and not resume_after_card and (confirm_first or missing):
            state["active_clarify"] = "CLARIFY_PROFILE"
            state["pending_tasks"] = ["RECOMMEND"]
            # Recommendation card always contains all seven fields and pre-fills
            # known values; missing_fields is therefore informational only.
            state["_missing_fields"] = list(missing)
            return _agent.generate_clarify_question(state, "CLARIFY_PROFILE")

        # Once the card has been submitted, consume the one-turn confirmation
        # marker and let the existing recommendation pipeline run normally.
        if resume_after_card:
            state.pop("_confirm_profile_before_recommend", None)
        return _original_run_recommend(state, bundles)

    def _patched_judge_recommendations_with_ai(candidates, bundles, profile, interest):
        return _original_judge_recommendations_with_ai(
            candidates, bundles, _profile_for_ai(profile), interest
        )

    def _patched_review_eligibility_with_ai(bundle, profile, existing_answers, rule_result):
        return _original_review_eligibility_with_ai(
            bundle, _profile_for_ai(profile), existing_answers, rule_result
        )

    _agent.apply_action_transition = _patched_apply_action_transition
    _agent.run_recommend = _patched_run_recommend
    _agent.judge_recommendations_with_ai = _patched_judge_recommendations_with_ai
    _agent.rerank_recommendations_with_ai = _patched_judge_recommendations_with_ai
    _agent.review_eligibility_with_ai = _patched_review_eligibility_with_ai
