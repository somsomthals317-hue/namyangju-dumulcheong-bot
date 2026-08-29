"""
Agent State 관리
- 세션 단위로 유지
- Profile, focus, pending_tasks, policy_answers 등
"""


def get_default_state():
    """기본 세션 State 생성"""
    return {
        # 사용자 Profile (7개)
        "profile": {
            "age": None,
            "residency": None,
            "employment": None,
            "student": None,
            "startup": None,
            "housing": None,
            "marriage": None,
        },
        # 관심 분야
        "interest_query": None,
        # 현재 주제와 정책. 기존 focus 필드는 하위 호환용으로 함께 유지
        "current_topic": None,
        "current_policy_id": None,
        "focus_policy_id": None,
        # 직전 검색·추천 결과와 현재 실행 중인 멀티쿼리
        "last_result_policy_ids": [],
        # 추천 뒤 특정 정책 설명으로 이동해도 "다른 분야"가 직전 추천
        # 문맥을 잃지 않도록 마지막 추천 묶음을 별도로 보존한다.
        "last_recommendation_topic": None,
        "last_recommendation_policy_ids": [],
        "active_workflow": None,
        "last_action": None,
        "last_task": None,
        "last_tasks": [],
        "last_intent_failure": None,
        # 자격확인 중인 정책
        "selected_policy_id": None,
        # 대화 메시지 기록
        "messages": [],
        # Clarify 관련
        "pending_tasks": [],
        "active_clarify": None,
        "resume_step": None,
        # 정책별 추가 질문 답변 저장
        "policy_answers": {},
        # 상태
        "profile_status": "INCOMPLETE",  # INCOMPLETE | COMPLETE
    }


def get_profile_status(profile):
    """Profile 완성 여부 체크"""
    required = ["age", "residency", "employment", "student", "startup", "housing", "marriage"]
    for field in required:
        if profile.get(field) is None:
            return "INCOMPLETE"
    return "COMPLETE"


def get_missing_profile_fields(profile):
    """부족한 Profile 필드 목록 반환"""
    required = ["age", "residency", "employment", "student", "startup", "housing", "marriage"]
    missing = []
    for field in required:
        if profile.get(field) is None:
            missing.append(field)
    return missing


PROFILE_KEYS = ["age", "residency", "employment", "student", "startup", "housing", "marriage"]


def get_policy_needed_fields(bundle):
    """
    특정 정책이 실제로 필요한 공통 Profile 필드만 반환.
    - basic_condition이 "해당없음"인 항목은 제외
    - 공통 Profile 7개에 없는 키(예: income)는 제외 (additional_questions로 처리)
    """
    basic = bundle.get("basic_condition", {})
    needed = []
    for key, value in basic.items():
        if key not in PROFILE_KEYS:
            continue
        if value and value != "해당없음":
            # 공통 Profile 선택지로 직접 비교할 수 있는 조건만 묻는다.
            # 예: "면접 예정", "영농경력 3년 이하"는 취업/미취업 카드로
            # 답할 수 없으므로 정책별 additional_questions가 담당한다.
            if key == "employment" and not (
                "미취업" in str(value) or str(value).startswith("재직자")
            ):
                continue
            needed.append(key)
    return needed



TASK_TRANSIENT_KEYS = (
    "_active_additional_q",
    "_partial_results",
    "_policy_candidates",
    "_explore_mode",
    "_skip_profile_check",
    "_missing_fields",
    "_original_query",
    "_rewritten_query",
    "_policy_mention",
    "_recommend_query",
    "_pending_age_confirmation",
    "_intent_turn_kind",
    "_intent_reuse_focus",
    "_intent_confidence",
    "_intent_workflow",
    "_normalized_action",
    "_exclude_topics",
    "_exclude_policy_ids",
    "_follow_up_field",
    "_eligibility_policy_id",
    "_eligibility_profile_fields",
)


def reset_task_context(state, keep_focus=False, keep_interest=False):
    """새 작업을 시작할 때 이전 카드·검색·선택 상태가 섞이지 않게 정리한다."""
    state["active_clarify"] = None
    state["pending_tasks"] = []
    state["selected_policy_id"] = None
    state["resume_step"] = None
    state["active_workflow"] = None
    state["last_result_policy_ids"] = []
    for key in TASK_TRANSIENT_KEYS:
        state.pop(key, None)
    if not keep_focus:
        state["focus_policy_id"] = None
        state["current_policy_id"] = None
    if not keep_interest:
        state["interest_query"] = None
        state["current_topic"] = None
    return state

def update_profile(state, patch):
    """Profile을 patch로 업데이트"""
    for key, value in patch.items():
        if key in state["profile"] and value is not None:
            state["profile"][key] = value
    state["profile_status"] = get_profile_status(state["profile"])
    return state


def save_policy_answer(state, policy_id, question_id, answer):
    """정책별 추가 질문 답변 저장"""
    if policy_id not in state["policy_answers"]:
        state["policy_answers"][policy_id] = {}
    state["policy_answers"][policy_id][question_id] = answer
    return state

