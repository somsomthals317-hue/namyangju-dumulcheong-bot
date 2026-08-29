"""Runtime compatibility fixes for recommendation/profile UI contracts."""

import re

try:
    import agent as _agent
    from state import get_missing_profile_fields, update_profile
except Exception:  # pragma: no cover
    _agent = None


if _agent is not None:
    _original_apply_action_transition = _agent.apply_action_transition
    _original_run_recommend = _agent.run_recommend
    _original_judge_recommendations_with_ai = _agent.judge_recommendations_with_ai
    _original_review_eligibility_with_ai = _agent.review_eligibility_with_ai
    _original_handle_turn = _agent.handle_turn

    def _profile_for_ai(profile):
        result = dict(profile or {})
        if result.get("residency") == "예":
            result["residency"] = "남양주시 거주"
        elif result.get("residency") == "아니오":
            result["residency"] = "남양주시 비거주"
        return result

    def _canonical_profile_patch(raw_patch):
        patch = dict(raw_patch or {})
        if patch.get("student") == "아니오":
            patch["student"] = "해당하지 않음"
        if patch.get("housing") == "유주택":
            patch["housing"] = "주택 소유"
        return patch

    def _specific_policy_action(message, bundles):
        text = str(message or "")
        compact = re.sub(r"[^0-9a-z가-힣]", "", text.lower())
        if not compact:
            return None

        matched = []
        for bundle in bundles or []:
            name = str(bundle.get("policy_name") or "").strip()
            if not name:
                continue
            name_compact = re.sub(r"[^0-9a-z가-힣]", "", name.lower())
            if name_compact and name_compact in compact:
                matched.append((bundle.get("policy_id"), name))

        if len(matched) != 1:
            alias_id = _agent.resolve_policy_alias(text)
            if alias_id:
                bundle = next((b for b in bundles or [] if b.get("policy_id") == alias_id), None)
                if bundle:
                    matched = [(alias_id, bundle.get("policy_name"))]

        if len(matched) != 1:
            return None

        policy_id, policy_name = matched[0]
        if re.search(r"자격|자격\s*조회|자격\s*확인|가능한지|신청\s*(?:할\s*)?수", text):
            return {
                "action": "NORMAL",
                "tasks": ["ELIGIBILITY"],
                "policy_id": policy_id,
                "policy_mention": policy_name,
                "use_previous_context": False,
                "confidence": "high",
            }
        if re.search(r"알아보|설명|알려|내용|뭐야", text):
            return {
                "action": "NORMAL",
                "tasks": ["EXPLAIN"],
                "policy_id": policy_id,
                "policy_mention": policy_name,
                "use_previous_context": False,
                "confidence": "high",
            }
        return None

    def _patched_apply_action_transition(state, action, bundles, preserve_workflow=False):
        result = _original_apply_action_transition(
            state, action, bundles, preserve_workflow=preserve_workflow
        )
        if isinstance(action, dict) and "RECOMMEND" in (action.get("tasks") or []):
            explicit_explore = action.get("explore_without_profile") is True
            if explicit_explore:
                state.pop("_confirm_profile_before_recommend", None)
            else:
                # 자연어 NORMAL뿐 아니라 CHANGE_TOPIC/SHOW_ALTERNATIVES/FOLLOW_UP도
                # 저장된 Profile을 카드에서 확인한 뒤 추천을 실행한다.
                state["_confirm_profile_before_recommend"] = True
        return result

    def _patched_run_recommend(state, bundles):
        profile = state.get("profile", {})
        explicit_explore = bool(state.get("_explore_mode"))
        resume_after_card = bool(state.get("_skip_profile_check"))
        confirm_first = bool(state.get("_confirm_profile_before_recommend"))
        missing = get_missing_profile_fields(profile)

        if not explicit_explore and not resume_after_card and (confirm_first or missing):
            state["active_clarify"] = "CLARIFY_PROFILE"
            state["pending_tasks"] = ["RECOMMEND"]
            state["_missing_fields"] = list(missing)
            return _agent.generate_clarify_question(state, "CLARIFY_PROFILE")

        if resume_after_card:
            state.pop("_confirm_profile_before_recommend", None)
        return _original_run_recommend(state, bundles)

    def _patched_judge_recommendations_with_ai(candidates, bundles, profile, interest):
        return _original_judge_recommendations_with_ai(
            candidates, bundles, _profile_for_ai(profile), interest
        )

    def _patched_review_eligibility_with_ai(bundle, profile, existing_answers, rule_result):
        reviewed = _original_review_eligibility_with_ai(
            bundle, _profile_for_ai(profile), existing_answers, rule_result
        )
        if not isinstance(reviewed, dict):
            return reviewed

        # State에서 남양주시 거주가 이미 확정된 경우 AI가 같은 거주 조건을
        # 다시 미확인으로 만들어 규칙 PASS를 UNKNOWN으로 내리지 못하게 한다.
        if (profile or {}).get("residency") == "예":
            missing = reviewed.get("missing_conditions") or []
            filtered = [
                item for item in missing
                if not (
                    ("거주" in str(item) or "남양주" in str(item))
                    and "AI 추가 확인" in str(item)
                )
            ]
            reviewed["missing_conditions"] = filtered
            if (
                rule_result.get("eligibility_status") == "PASS"
                and reviewed.get("eligibility_status") == "UNKNOWN"
                and not filtered
            ):
                reviewed["eligibility_status"] = "PASS"
                reviewed["failed_conditions"] = []
                reviewed["explanation"] = rule_result.get("explanation", reviewed.get("explanation", ""))
        return reviewed

    def _patched_handle_turn(state, user_message, collection, bundles, ui_event=None, input_action=None):
        # 모든 자연어 턴에서 명시된 Profile 값을 결정론적 추출기로 먼저 누적한다.
        # Prompt A가 일부 필드를 놓쳐도 카드 prefill 값은 유지된다.
        if user_message and str(user_message).strip() and not ui_event:
            explicit_patch = _canonical_profile_patch(
                _agent.extract_profile_patch_from_text(user_message)
            )
            if explicit_patch:
                update_profile(state, explicit_patch)

        # "청년꽃간 자격조회하자"처럼 정책명이 명시되면 일반 메뉴/fallback보다
        # 해당 정책의 구조화 Action을 우선한다.
        if not ui_event and input_action is None:
            specific = _specific_policy_action(user_message, bundles)
            if specific:
                input_action = specific

        return _original_handle_turn(
            state, user_message, collection, bundles,
            ui_event=ui_event, input_action=input_action,
        )

    _agent.apply_action_transition = _patched_apply_action_transition
    _agent.run_recommend = _patched_run_recommend
    _agent.judge_recommendations_with_ai = _patched_judge_recommendations_with_ai
    _agent.rerank_recommendations_with_ai = _patched_judge_recommendations_with_ai
    _agent.review_eligibility_with_ai = _patched_review_eligibility_with_ai
    _agent.handle_turn = _patched_handle_turn
