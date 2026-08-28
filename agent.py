"""
두물청 - Agent 핵심 로직
handle_turn()을 중심으로 Intent 분석 → Clarify → Task 실행 → 응답 결합
"""
import json
import os
import re
from functools import lru_cache
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
from state import (
    get_default_state, get_profile_status, get_missing_profile_fields,
    get_policy_needed_fields, update_profile, save_policy_answer, reset_task_context,
)
from prompts import (
    SYSTEM_PROMPT, PROMPT_A_INTENT, PROMPT_B_SELECT, PROMPT_B_EXPLAIN,
    PROMPT_C_RECOMMEND, PROMPT_D_ELIGIBILITY, PROMPT_E_COMPOSE,
    CLARIFY_MESSAGES,
)
from vector_store import retrieve_policy_candidates
from data_loader import build_policy_bundles, load_origin_documents, load_summary_documents

# OpenAI 클라이언트 (모듈 레벨)
_client = None


def configure_openai(api_key):
    """OpenAI API 설정"""
    global _client
    _client = OpenAI(api_key=api_key, timeout=45.0, max_retries=2)


# 하위 호환을 위한 별칭
configure_gemini = configure_openai


def call_openai(prompt, system_instruction=None, json_mode=False):
    """OpenAI API 호출"""
    from config import OPENAI_MODEL
    global _client
    
    # 클라이언트가 없으면 자동 초기화
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            _client = OpenAI(api_key=api_key, timeout=45.0, max_retries=2)
        else:
            return "[ERROR] OPENAI_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요."
    
    messages = [
        {"role": "system", "content": system_instruction or SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    
    try:
        request_args = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            request_args["response_format"] = {"type": "json_object"}
        response = _client.chat.completions.create(**request_args)
        content = response.choices[0].message.content
        return content.strip() if content else "[ERROR] 모델이 빈 응답을 반환했습니다."
    except Exception as exc:
        # API 예외가 FastAPI 전체 요청을 500으로 만들지 않게 한다.
        return f"[ERROR] 모델 호출 실패: {type(exc).__name__}"


# 하위 호환을 위한 별칭
call_gemini = call_openai


def parse_json_response(text):
    """모델 응답에서 JSON 추출"""
    # 코드블록 제거
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 부분 파싱 시도
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1:
            start = text.find("[")
            end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


CANONICAL_ACTIONS = {
    "NORMAL", "FOLLOW_UP", "SHOW_ALTERNATIVES",
    "CHANGE_TOPIC", "RESET", "CLARIFY",
}
# 이미 배포된 화면/API payload는 깨뜨리지 않고 새 Backbone으로 즉시 변환한다.
# 새 Prompt와 새 버튼은 아래 구형 명칭을 생성하지 않는다.
LEGACY_ACTION_ALIASES = {
    "SEARCH_POLICY": ("NORMAL", "EXPLAIN"),
    "REQUEST_RECOMMENDATION": ("NORMAL", "RECOMMEND"),
    "CHECK_ELIGIBILITY": ("NORMAL", "ELIGIBILITY"),
    "RUN_WORKFLOW": ("NORMAL", None),
}
VALID_ACTIONS = CANONICAL_ACTIONS | set(LEGACY_ACTION_ALIASES)
VALID_TASKS = {"EXPLAIN", "RECOMMEND", "ELIGIBILITY"}
VALID_FOLLOW_UP_FIELDS = {
    "application_period", "application_method", "documents",
    "benefit", "target", "conditions", "general", None,
}


def _clean_action_topics(value):
    if not value:
        return []
    raw = value if isinstance(value, list) else [value]
    normalized = normalize_interests(", ".join(str(item) for item in raw))
    return [item.strip() for item in normalized.split(",") if item.strip()]


def validate_action_payload(payload):
    """GPT와 UI Action을 같은 보수적 Schema로 검증한다."""
    if not isinstance(payload, dict):
        return None
    raw_action = payload.get("action")
    if raw_action not in VALID_ACTIONS:
        return None
    raw_tasks = payload.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    tasks = [task for task in ("EXPLAIN", "RECOMMEND", "ELIGIBILITY") if task in raw_tasks]
    action, legacy_task = LEGACY_ACTION_ALIASES.get(raw_action, (raw_action, None))
    if legacy_task and not tasks:
        tasks = [legacy_task]

    # Action과 Task는 독립이지만 CHANGE_TOPIC만 추천 관심 분야 전환 전용이다.
    # 충돌 payload를 임의로 실행하지 않고 해석 실패 Action으로 격리한다.
    clarify_reason = str(payload.get("clarify_reason") or "").strip() or None
    if action == "CHANGE_TOPIC" and tasks != ["RECOMMEND"]:
        action = "CLARIFY"
        tasks = []
        clarify_reason = "INVALID_ACTION_TASK"
    elif action == "CLARIFY":
        tasks = []
        clarify_reason = clarify_reason or "LOW_CONFIDENCE"
    elif action == "RESET":
        tasks = []
    elif not tasks:
        action = "CLARIFY"
        clarify_reason = clarify_reason or "NO_TASK"
    topic_values = _clean_action_topics(payload.get("topic"))
    topic = topic_values[0] if topic_values else None
    exclude_topics = _clean_action_topics(payload.get("exclude_topics"))
    exclude_policy_mentions = payload.get("exclude_policy_mentions", [])
    if not isinstance(exclude_policy_mentions, list):
        exclude_policy_mentions = []
    follow_up_field = payload.get("follow_up_field")
    if follow_up_field not in VALID_FOLLOW_UP_FIELDS:
        follow_up_field = None
    confidence = payload.get("confidence")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "action": action,
        "tasks": tasks,
        "topic": topic,
        "policy_id": str(payload.get("policy_id") or "").strip() or None,
        "policy_mention": str(payload.get("policy_mention") or "").strip() or None,
        "exclude_topics": exclude_topics,
        "exclude_policy_ids": [
            str(item).strip()
            for item in payload.get("exclude_policy_ids", [])
            if str(item).strip()
        ] if isinstance(payload.get("exclude_policy_ids", []), list) else [],
        "exclude_policy_mentions": [
            str(item).strip() for item in exclude_policy_mentions if str(item).strip()
        ],
        "follow_up_field": follow_up_field,
        "use_previous_context": payload.get("use_previous_context") is True,
        "confidence": confidence,
        "workflow": payload.get("workflow") if isinstance(payload.get("workflow"), list) else [],
        "clarify_reason": clarify_reason,
        "explore_without_profile": payload.get("explore_without_profile") is True,
    }


def detect_navigation_action(state, message, bundles):
    """전환·대안·후속 질문의 고위험 표현을 RAG 전에 Action으로 정규화한다."""
    msg = (message or "").strip()
    if not msg:
        return None
    compact = re.sub(r"\s+", "", msg.lower())
    if re.search(r"처음부터|대화\s*초기화|전부\s*초기화|리셋", msg):
        return validate_action_payload({
            "action": "RESET", "tasks": [], "confidence": "high",
        })

    current_policy_id = state.get("current_policy_id") or state.get("focus_policy_id")
    current_topic = state.get("current_topic") or state.get("interest_query")
    mentioned_topics = _clean_action_topics(normalize_interests(msg))

    # 특정 공식 정책명은 일반 분야 검색보다 우선한다.
    exact_policy = resolve_policy_alias(msg) or _contains_exact_policy_name(bundles, msg)

    if current_policy_id and re.search(
        r"(?:이거|그거|이\s*정책|그\s*정책).*(?:비슷|유사).*(?:추천|다른\s*정책)",
        msg,
    ):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["RECOMMEND"],
            "policy_id": current_policy_id,
            "use_previous_context": True,
            "confidence": "high",
        })

    if current_policy_id and re.search(
        r"(?:이거|그거|이\s*정책|그\s*정책).*(?:설명|알려|내용|뭐야)",
        msg,
    ) and not re.search(r"말고|다른\s*(?:정책|분야|것|거|지원)?", msg):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["EXPLAIN"],
            "policy_id": current_policy_id,
            "follow_up_field": "general",
            "use_previous_context": True,
            "confidence": "high",
        })

    result_reference_id = _policy_id_from_result_reference(state, msg)
    if result_reference_id:
        referenced_task = "ELIGIBILITY" if re.search(
            r"자격|가능한지|되는지|신청\s*(?:할\s*)?수", msg
        ) else "EXPLAIN"
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": [referenced_task],
            "policy_id": result_reference_id,
            "use_previous_context": True,
            "confidence": "high",
        })

    if current_policy_id and re.search(r"신청\s*(?:기간|시기)|접수\s*기간|언제\s*신청", msg):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["EXPLAIN"],
            "follow_up_field": "application_period",
            "use_previous_context": True,
            "confidence": "high",
        })
    if current_policy_id and re.search(r"신청\s*(?:방법|어떻게)|접수\s*방법", msg):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["EXPLAIN"],
            "follow_up_field": "application_method",
            "use_previous_context": True,
            "confidence": "high",
        })
    if current_policy_id and re.search(r"제출\s*서류|필요\s*서류|준비\s*서류", msg):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["EXPLAIN"],
            "follow_up_field": "documents",
            "use_previous_context": True,
            "confidence": "high",
        })
    if (
        current_policy_id
        and re.search(
            r"신청\s*조건|자격|가능한지|신청\s*(?:할\s*)?수|지원\s*(?:받을\s*)?수",
            msg,
        )
        and "자격증" not in msg
        and not re.search(r"말고|다른\s*(?:정책|분야|것|거|지원)?", msg)
    ):
        return validate_action_payload({
            "action": "FOLLOW_UP",
            "tasks": ["ELIGIBILITY"],
            "policy_id": current_policy_id,
            "policy_mention": "그 정책",
            "use_previous_context": True,
            "confidence": "high",
        })

    before, separator, after = msg.partition("말고")
    before_topics = _clean_action_topics(normalize_interests(before)) if separator else []
    after_topics = _clean_action_topics(normalize_interests(after)) if separator else []
    alternative_cue = bool(re.search(
        r"말고|다른\s*(?:정책|분야|것|거|것도|것은)?|"
        r"(?:이거|그거|이것|그것)\s*말고|대안",
        msg,
    ))

    if separator and after_topics and not exact_policy:
        new_topic = after_topics[0]
        return validate_action_payload({
            "action": "CHANGE_TOPIC",
            "tasks": ["RECOMMEND"],
            "topic": new_topic,
            "exclude_topics": before_topics,
            "use_previous_context": False,
            "confidence": "high",
        })

    alternative_eligibility = (
        alternative_cue
        and "자격증" not in msg
        and bool(re.search(
            r"자격|가능한지|되는지|나도\s*(?:돼|되)|"
            r"신청\s*(?:할\s*)?수|지원\s*(?:받을\s*)?수",
            msg,
        ))
    )
    if alternative_eligibility:
        return validate_action_payload({
            "action": "SHOW_ALTERNATIVES",
            "tasks": ["ELIGIBILITY"],
            "policy_mention": _extract_policy_mention(msg) if exact_policy else None,
            "exclude_topics": before_topics,
            "exclude_policy_ids": [current_policy_id] if current_policy_id else [],
            "use_previous_context": False,
            "confidence": "high",
        })

    # 직전 대상을 거부하면서 새 정책명을 직접 지정하면 추천으로 우회하지 않는다.
    if alternative_cue and exact_policy:
        task = "RECOMMEND" if _is_explicit_recommend_request(msg) else "EXPLAIN"
        mention = _extract_policy_mention(msg)
        return validate_action_payload({
            "action": "SHOW_ALTERNATIVES",
            "tasks": [task],
            "policy_mention": mention,
            "exclude_topics": before_topics,
            "exclude_policy_ids": [current_policy_id] if current_policy_id else [],
            "use_previous_context": True,
            "confidence": "high",
        })

    # 진행 중 카드에서 전환 동사와 새 작업 의미가 섞인 긴 문장은 Prompt A가
    # Action을 의미적으로 판정하도록 넘긴다.
    if state.get("active_clarify") and alternative_cue:
        return None

    if alternative_cue:
        exclude_topics = before_topics[:]
        exclude_policy_ids = []
        if re.search(r"(?:이거|그거|이것|그것)\s*말고", msg):
            # 직전 답변이 추천 목록이면 "이거/그거"는 화면에 보인 결과 묶음을
            # 가리킬 수 있다. 한 정책만 제외해 같은 목록이 반복되지 않게 한다.
            exclude_policy_ids.extend(state.get("last_result_policy_ids") or [])
            if not exclude_policy_ids and current_policy_id:
                exclude_policy_ids.append(current_policy_id)
        elif not exclude_topics and current_policy_id:
            exclude_policy_ids.append(current_policy_id)
        elif not exclude_topics and current_topic:
            exclude_topics = _clean_action_topics(current_topic)
        return validate_action_payload({
            "action": "SHOW_ALTERNATIVES",
            "tasks": ["RECOMMEND"],
            "exclude_topics": exclude_topics,
            "exclude_policy_ids": exclude_policy_ids,
            "use_previous_context": True,
            "confidence": "high",
        })

    # "취업 정책은?"처럼 현재 발화의 새 분야를 과거 주제보다 우선한다.
    if mentioned_topics and not exact_policy and re.search(
        r"정책\s*(?:은|는|있|알려|보여|찾아)|(?:쪽|분야)\s*(?:은|는|정책)",
        msg,
    ):
        topic = mentioned_topics[0]
        action = "CHANGE_TOPIC" if current_topic and topic not in _clean_action_topics(current_topic) else "NORMAL"
        return validate_action_payload({
            "action": action,
            "tasks": ["RECOMMEND"],
            "topic": topic,
            "use_previous_context": False,
            "confidence": "high",
        })
    return None


def _policy_id_for_action(action, bundles):
    policy_id = action.get("policy_id")
    if policy_id and any(bundle["policy_id"] == policy_id for bundle in bundles):
        return policy_id
    mention = action.get("policy_mention")
    alias_id = resolve_policy_alias(mention) if mention else None
    if alias_id and any(bundle["policy_id"] == alias_id for bundle in bundles):
        return alias_id
    if mention:
        status, candidates = match_policy_candidates(bundles, mention)
        if status == "UNIQUE":
            return candidates[0]["policy_id"]
    return None


def _intent_clarify_response(reason=None):
    """자연어 해석 실패를 정책/프로필 Clarify와 분리해 안전하게 종료한다."""
    return (
        "말씀하신 내용을 정책 요청으로 정확히 이해하지 못했어요.\n"
        "정책명과 궁금한 내용(설명·추천·자격)을 함께 적거나, 아래에서 원하는 기능을 선택해주세요.\n\n"
        "무엇을 도와드릴까요?\n\n"
        "[ACTION_BTN:NORMAL_EXPLAIN:정책 알아보기]\n"
        "[ACTION_BTN:NORMAL_RECOMMEND:맞춤 추천받기]\n"
        "[ACTION_BTN:NORMAL_ELIGIBILITY:자격 확인하기]"
    )


def _topic_for_policy_id(policy_id, bundles):
    """FOLLOW_UP+RECOMMEND가 직전 정책을 seed로 쓸 때 공식 관심 분야를 찾는다."""
    bundle = next((item for item in bundles if item.get("policy_id") == policy_id), None)
    if not bundle:
        return None
    raw = bundle.get("recommendation_interests") or []
    if isinstance(raw, str):
        raw = [raw]
    normalized = _clean_action_topics(raw)
    if normalized:
        return ", ".join(normalized)
    return normalize_interests(bundle.get("category") or bundle.get("sub_category") or "") or None


def _policy_name_for_id(policy_id, bundles):
    bundle = next((item for item in bundles if item.get("policy_id") == policy_id), None)
    return bundle.get("policy_name") if bundle else None


def apply_action_transition(state, action, bundles, preserve_workflow=False):
    """Action에 따라 이전 State를 유지·제외·교체하고 실행 task를 반환한다."""
    action = validate_action_payload(action)
    if not action:
        return [], [], None
    kind = action["action"]
    state["last_action"] = kind

    def reset_for_transition(keep_focus=False, keep_interest=False):
        workflow = state.get("active_workflow") if preserve_workflow else None
        reset_task_context(state, keep_focus=keep_focus, keep_interest=keep_interest)
        if preserve_workflow:
            state["active_workflow"] = workflow

    if kind == "CLARIFY":
        # 해석 실패는 기존 정책·주제를 바꾸지 않지만, 사용자가 메뉴를 선택할 수
        # 있도록 진행 중 카드/Workflow만 닫아 stale 입력 재개를 막는다.
        state["last_intent_failure"] = action.get("clarify_reason") or "LOW_CONFIDENCE"
        state["active_clarify"] = None
        state["pending_tasks"] = []
        state["active_workflow"] = None
        state.pop("_policy_candidates", None)
        state.pop("_active_additional_q", None)
        return [], [], _intent_clarify_response(state["last_intent_failure"])

    if kind == "RESET":
        clean = get_default_state()
        state.clear()
        state.update(clean)
        return [], [], "대화를 초기화했어요. 정책 검색, 맞춤 추천, 자격 확인 중 무엇을 도와드릴까요?"

    tasks = action.get("tasks") or []

    if kind == "NORMAL":
        policy_id = _policy_id_for_action(action, bundles)
        topic = action.get("topic")
        reset_for_transition()
        if topic:
            state["interest_query"] = topic
            state["current_topic"] = topic
        if action.get("policy_mention"):
            state["_policy_mention"] = action["policy_mention"]
        if policy_id:
            state["current_policy_id"] = policy_id
            state["focus_policy_id"] = policy_id
            state["_policy_mention"] = _policy_name_for_id(policy_id, bundles) or state.get("_policy_mention")
            if "ELIGIBILITY" in tasks:
                state["selected_policy_id"] = policy_id
        if "RECOMMEND" in tasks and action.get("explore_without_profile"):
            state["_skip_profile_check"] = True
            state["_explore_mode"] = True
        clarifies = []
        if "RECOMMEND" in tasks and not state.get("interest_query"):
            clarifies.append("CLARIFY_PREFERENCE")
        if any(task in tasks for task in ("EXPLAIN", "ELIGIBILITY")) and not (
            policy_id or action.get("policy_mention")
        ):
            clarifies.append("CLARIFY_POLICY")
        return tasks, clarifies, None

    if kind == "FOLLOW_UP":
        # 새 정책명이 있으면 항상 과거 focus보다 먼저 resolve한다.
        policy_id = _policy_id_for_action(action, bundles)
        if not policy_id and action.get("use_previous_context"):
            policy_id = state.get("current_policy_id") or state.get("focus_policy_id")
        if policy_id:
            state["current_policy_id"] = policy_id
            state["focus_policy_id"] = policy_id
            state["_policy_mention"] = _policy_name_for_id(policy_id, bundles) or action.get("policy_mention")
        if "RECOMMEND" in tasks and not action.get("topic"):
            seed_topic = _topic_for_policy_id(policy_id, bundles)
            if seed_topic:
                state["interest_query"] = seed_topic
                state["current_topic"] = seed_topic
        elif action.get("topic"):
            state["interest_query"] = action["topic"]
            state["current_topic"] = action["topic"]
        if "ELIGIBILITY" in tasks and policy_id:
            state["selected_policy_id"] = policy_id
        if "RECOMMEND" in tasks and action.get("explore_without_profile"):
            state["_skip_profile_check"] = True
            state["_explore_mode"] = True
        if "EXPLAIN" in tasks:
            state["_follow_up_field"] = action.get("follow_up_field") or "general"
        clarifies = []
        if any(task in tasks for task in ("EXPLAIN", "ELIGIBILITY")) and not policy_id:
            clarifies.append("CLARIFY_POLICY")
        if "RECOMMEND" in tasks and not state.get("interest_query"):
            clarifies.append("CLARIFY_PREFERENCE")
        return tasks, clarifies, None

    if kind == "SHOW_ALTERNATIVES":
        previous_topic = state.get("current_topic") or state.get("interest_query")
        previous_policy = state.get("current_policy_id") or state.get("focus_policy_id")
        new_policy_id = _policy_id_for_action(action, bundles)
        exclude_topics = list(action.get("exclude_topics") or [])
        exclude_ids = list(action.get("exclude_policy_ids") or [])
        if not exclude_topics and not exclude_ids:
            if state.get("last_result_policy_ids"):
                exclude_ids.extend(state["last_result_policy_ids"])
            elif previous_policy:
                exclude_ids.append(previous_policy)
            elif previous_topic:
                exclude_topics.extend(_clean_action_topics(previous_topic))
        reset_for_transition()
        state["_exclude_topics"] = list(dict.fromkeys(exclude_topics))
        state["_exclude_policy_ids"] = list(dict.fromkeys(exclude_ids))
        # 새 정책명이 함께 있으면 그 대상을 과거 policy보다 우선한다.
        if action.get("policy_mention"):
            state["_policy_mention"] = action["policy_mention"]
        if new_policy_id:
            state["current_policy_id"] = new_policy_id
            state["focus_policy_id"] = new_policy_id
            state["_policy_mention"] = _policy_name_for_id(new_policy_id, bundles) or state.get("_policy_mention")
            if "ELIGIBILITY" in tasks:
                state["selected_policy_id"] = new_policy_id
        if "RECOMMEND" in tasks:
            # 특정 정책만 제외하면 같은 분야를 seed로, 분야를 제외하면 전체를 탐색한다.
            next_topic = action.get("topic") or (
                previous_topic if exclude_ids and previous_topic else "전체"
            )
            state["interest_query"] = next_topic or "전체"
            state["current_topic"] = next_topic or "전체"
            state["_skip_profile_check"] = True
            state["_explore_mode"] = True
        clarifies = []
        if any(task in tasks for task in ("EXPLAIN", "ELIGIBILITY")) and not (
            new_policy_id or action.get("policy_mention")
        ):
            clarifies.append("CLARIFY_POLICY")
        return tasks, clarifies, None

    if kind == "CHANGE_TOPIC":
        topic = action.get("topic")
        reset_for_transition()
        if topic:
            state["interest_query"] = topic
            state["current_topic"] = topic
        if action.get("exclude_topics"):
            state["_exclude_topics"] = action["exclude_topics"]
        state["_skip_profile_check"] = True
        state["_explore_mode"] = True
        return ["RECOMMEND"], ([] if topic else ["CLARIFY_PREFERENCE"]), None

    return tasks, [], None


def run_policy_follow_up(state, bundles):
    """현재 정책을 다시 검색하지 않고 저장된 Bundle의 요청 필드만 답한다."""
    policy_id = state.get("current_policy_id") or state.get("focus_policy_id")
    bundle = next((item for item in bundles if item["policy_id"] == policy_id), None)
    if not bundle:
        state["active_clarify"] = "CLARIFY_POLICY"
        state["pending_tasks"] = ["EXPLAIN"]
        return "어떤 정책을 말씀하시는지 정책명을 선택해주세요."
    field = state.pop("_follow_up_field", "general")
    labels = {
        "application_period": ("신청기간", bundle.get("application_period") or "공지 확인 필요"),
        "application_method": ("신청방법", bundle.get("application_method") or "공식 정책 페이지에서 확인해주세요."),
        "documents": ("제출서류", bundle.get("documents") or "공식 모집공고에서 확인해주세요."),
        "benefit": ("지원내용", bundle.get("benefit") or bundle.get("summary") or "공식 페이지에서 확인해주세요."),
        "target": ("지원대상", bundle.get("main_target") or "공식 페이지에서 확인해주세요."),
        "conditions": ("신청조건", bundle.get("main_target") or "자격 확인 기능에서 세부 조건을 확인해주세요."),
    }
    label, value = labels.get(field, ("정책 안내", bundle.get("summary") or bundle.get("benefit") or "공식 페이지에서 확인해주세요."))
    lines = [f"📋 {bundle['policy_name']} {label}", "", f"- {value}"]
    link = _official_policy_link(bundle.get("source"))
    if link:
        lines.extend(["", link])
    return "\n".join(lines)


def extract_explicit_age(text):
    """'만 나이가 24살', '만으로 24세', '만 24살'처럼 명시된 만 나이를 추출한다."""
    value = (text or "").strip()
    patterns = (
        r"만\s*나이(?:가|는)?\s*(\d{1,2})\s*(?:살|세)",
        r"만으로\s*(\d{1,2})\s*(?:살|세)",
        r"만\s*(\d{1,2})\s*(?:살|세)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            age = int(match.group(1))
            if 0 <= age <= 100:
                return age
    return None


def resume_after_age_update(state, age, collection, bundles):
    """만 나이를 저장하고, 나이 확인 전에 진행 중이던 Clarify를 안전하게 이어간다."""
    update_profile(state, {"age": age})
    clarify_type = state.get("active_clarify")

    if clarify_type == "CLARIFY_PROFILE":
        return handle_clarify_answer(state, f"만 {age}세", collection, bundles)
    if clarify_type == "CLARIFY_POLICY":
        return f"만 나이 {age}세로 저장했어요.\n\n" + generate_clarify_question(state, clarify_type)
    if clarify_type == "CLARIFY_PREFERENCE":
        return f"만 나이 {age}세로 저장했어요.\n\n" + generate_clarify_question(state, clarify_type)
    if clarify_type == "CLARIFY_ADDITIONAL":
        return f"만 나이 {age}세로 저장했어요. 이어서 화면의 추가 자격 질문에 답해주세요."
    return f"만 나이 {age}세로 저장했어요. 정책 설명, 맞춤 추천, 자격 확인 중 무엇을 도와드릴까요?"


def handle_turn(state, user_message, collection, bundles, ui_event=None, input_action=None):
    """
    메인 턴 처리 함수
    Returns: (state, response_text)
    """
    # 메시지 기록
    if user_message and user_message.strip():
        state["messages"].append({"role": "user", "content": user_message.strip()})

    if not ui_event:
        guarded = get_guardrail_response(user_message)
        if guarded:
            if "[ACTION_BTN:NORMAL_" in guarded:
                _, _, guarded = apply_action_transition(
                    state,
                    {
                        "action": "CLARIFY",
                        "tasks": [],
                        "confidence": "low",
                        "clarify_reason": "INPUT_GUARDRAIL",
                    },
                    bundles,
                )
            state["messages"].append({"role": "assistant", "content": guarded})
            return state, guarded

        answer = (user_message or "").strip()
        pending_age = state.get("_pending_age_confirmation")
        explicit_age = extract_explicit_age(answer)

        # 확인 질문 뒤에 "만 나이가 24살"처럼 명확하게 다시 말하면 즉시 저장한다.
        if pending_age is not None and explicit_age is not None:
            state.pop("_pending_age_confirmation", None)
            response = resume_after_age_update(state, explicit_age, collection, bundles)
            state["messages"].append({"role": "assistant", "content": response})
            return state, response

        if isinstance(pending_age, int) and answer in ("예", "네", "맞아", "맞아요"):
            state.pop("_pending_age_confirmation", None)
            response = resume_after_age_update(state, pending_age, collection, bundles)
            state["messages"].append({"role": "assistant", "content": response})
            return state, response
        if pending_age is not None and answer in ("아니오", "아니요", "아니야"):
            # 기존 정책/추천 Clarify 카드를 다시 그리지 않고 정확한 만 나이를 기다린다.
            state["_pending_age_confirmation"] = "EXPLICIT"
            response = "알겠습니다. 정확한 만 나이를 '만 24세'처럼 알려주세요."
            state["messages"].append({"role": "assistant", "content": response})
            return state, response

        if pending_age == "EXPLICIT":
            response = "정확한 만 나이를 '만 24세' 또는 '만 나이가 24살'처럼 알려주세요."
            state["messages"].append({"role": "assistant", "content": response})
            return state, response

        # 처음부터 만 나이라고 명시한 경우에는 재확인하지 않는다.
        # 다만 "만 24세인데 기본소득 설명하고 자격도 봐줘"처럼 실제 작업 요청이
        # 함께 있으면 나이만 저장하고 끝내지 말고 아래 Intent 처리를 계속한다.
        if explicit_age is not None:
            has_task_request = bool(re.search(
                r"정책|추천|자격|설명|알려|가능|신청|지원|봐\s*줘|찾아",
                answer,
            ))
            if has_task_request and not state.get("active_clarify"):
                update_profile(state, {"age": explicit_age})
            else:
                response = resume_after_age_update(state, explicit_age, collection, bundles)
                state["messages"].append({"role": "assistant", "content": response})
                return state, response

        ambiguous_age = re.search(r"(?<!만\s)(\d{1,2})\s*살", answer)
        if ambiguous_age:
            age_candidate = int(ambiguous_age.group(1))
            state["_pending_age_confirmation"] = age_candidate
            response = f"말씀하신 {age_candidate}살이 만 나이 {age_candidate}세라는 뜻인가요? 예 또는 아니오로 답해주세요."
            state["messages"].append({"role": "assistant", "content": response})
            return state, response
    
    # 구조화 카드가 진행 중 Workflow의 질문에 답한 경우 새 Workflow를 만들지 않고 재개한다.
    if (
        ui_event in {"SUBMIT_PROFILE", "SUBMIT_RECOMMEND_PROFILE", "SUBMIT_ADDITIONAL_ANSWERS"}
        and state.get("active_workflow")
    ):
        state["active_clarify"] = None
        response = execute_active_workflow(state, collection, bundles)
        state["messages"].append({"role": "assistant", "content": response})
        return state, response

    # 1. UI와 자연어를 공통 Action으로 먼저 정규화한다.
    normalized_action = validate_action_payload(input_action)
    if not normalized_action and not ui_event:
        normalized_action = detect_navigation_action(state, user_message, bundles)

    # 같은 요청이라면 버튼과 자연어가 동일한 탐색 모드를 사용한다. 구조화
    # Action이 들어와도 현재 발화의 '조건 없이' 의미를 버리지 않는다.
    if (
        normalized_action
        and "RECOMMEND" in normalized_action.get("tasks", [])
        and "조건 없이" in (user_message or "")
    ):
        normalized_action["explore_without_profile"] = True

    if normalized_action:
        if normalized_action["action"] in {"RESET", "CLARIFY"}:
            _, _, direct_response = apply_action_transition(state, normalized_action, bundles)
            state["messages"].append({"role": "assistant", "content": direct_response})
            return state, direct_response
        tasks = list(normalized_action.get("tasks") or [])
        clarify_reasons = []
        raw_workflow = normalized_action.get("workflow") or []
        state["_intent_workflow"] = raw_workflow or [{
            "action": normalized_action["action"],
            "task": tasks[0] if len(tasks) == 1 else None,
            "policy_id": normalized_action.get("policy_id"),
            "policy_mention": normalized_action.get("policy_mention"),
            "topic": normalized_action.get("topic"),
            "exclude_topics": normalized_action.get("exclude_topics", []),
            "exclude_policy_ids": normalized_action.get("exclude_policy_ids", []),
            "exclude_policy_mentions": normalized_action.get("exclude_policy_mentions", []),
            "follow_up_field": normalized_action.get("follow_up_field"),
            "use_previous_context": normalized_action.get("use_previous_context", False),
            "explore_without_profile": normalized_action.get("explore_without_profile", False),
        }]
    # 구형 ui_event도 같은 task 실행기로 수렴시켜 하위 호환한다.
    elif ui_event:
        tasks, clarify_reasons = route_ui_event(ui_event, state)
        # 구조화 이벤트는 Clarify 재발 방지 + pending 정리
        NO_CLARIFY_EVENTS = ("SUBMIT_PROFILE", "SUBMIT_RECOMMEND_PROFILE",
                             "SUBMIT_ADDITIONAL_ANSWERS", "START_RECOMMEND")
        if ui_event in NO_CLARIFY_EVENTS:
            state["active_clarify"] = None
            state["pending_tasks"] = []
            clarify_reasons = []
        elif user_message and user_message.strip():
            state["_policy_mention"] = user_message.strip()
    else:
        # 2. Active Clarify에 대한 답변인지 확인
        #    구조화된 짧은 답변은 현재 카드가 처리하고, 작업 전환 가능성이 있는
        #    자유 문장은 GPT Intent가 새 작업/정책 변경/취소 여부를 재분류한다.
        preclassified_tasks, preclassified_clarifies = None, []
        if state["active_clarify"]:
            active_clarify = state["active_clarify"]
            if _is_other_policy_switch_request(user_message):
                reset_task_context(state)
            elif _should_reclassify_clarify_with_ai(user_message, active_clarify, bundles):
                probe_tasks, probe_clarifies = analyze_user_turn(state, user_message)
                turn_kind = state.pop("_intent_turn_kind", "")
                probe_action = state.get("_normalized_action")
                if probe_action and probe_action.get("action") == "CLARIFY":
                    state.pop("_normalized_action", None)
                    _, _, response = apply_action_transition(state, probe_action, bundles)
                    state["messages"].append({"role": "assistant", "content": response})
                    return state, response
                if turn_kind == "CANCEL":
                    reset_task_context(state)
                    response = "진행 중이던 질문을 취소했어요. 정책 설명, 맞춤 추천, 자격 확인 중 무엇을 도와드릴까요?"
                    state["messages"].append({"role": "assistant", "content": response})
                    return state, response
                if probe_tasks and turn_kind in {"NEW_TASK", "SWITCH_POLICY"}:
                    preclassified_tasks = probe_tasks
                    preclassified_clarifies = probe_clarifies
                elif is_explicit_new_request(user_message, active_clarify):
                    reset_task_context(
                        state,
                        keep_focus=_has_focus_reference(user_message),
                    )
                else:
                    response = handle_clarify_answer(state, user_message, collection, bundles)
                    state["messages"].append({"role": "assistant", "content": response})
                    return state, response
            elif is_explicit_new_request(user_message, active_clarify):
                reset_task_context(
                    state,
                    keep_focus=_has_focus_reference(user_message),
                )
            else:
                response = handle_clarify_answer(state, user_message, collection, bundles)
                state["messages"].append({"role": "assistant", "content": response})
                return state, response
        
        # 2.5 프론트 버튼에서 온 명시적 패턴 → Prompt A 안 타고 바로 확정
        tasks, clarify_reasons = preclassified_tasks, preclassified_clarifies
        msg = user_message.strip() if user_message else ""
        
        if tasks is not None:
            pass
        elif _looks_like_multi_task_request(msg):
            # 복합 요청은 Prompt A 한 번에서 task별 대상까지 구조화한다.
            tasks, clarify_reasons = analyze_user_turn(state, user_message)
            # GPT가 복합 문장을 한 Task로 축약해도 사용자 문장에 명시된 작업을
            # 버리지 않는다. 모델 Workflow와 결정론적 Atomic Unit을 병합하여
            # Task 수, 순서, 각 정책/분야 대상이 모두 보존되는지 검증한다.
            tasks, clarify_reasons = ensure_multi_task_workflow(
                state,
                msg,
                tasks,
                clarify_reasons,
                bundles,
            )
        elif _is_other_policy_switch_request(msg):
            # 기존 정책을 명시적으로 배제하면 예전 focus를 재사용하지 않는다.
            state["focus_policy_id"] = None
            state["selected_policy_id"] = None
            state["_policy_mention"] = None
            state["_rewritten_query"] = None
            state.pop("_policy_candidates", None)
            tasks = ["ELIGIBILITY"]
            clarify_reasons = ["CLARIFY_POLICY"]
        elif _is_multi_interest_eligibility_request(msg) and not _contains_exact_policy_name(bundles, msg):
            # "기본소득과 농업 자격확인"처럼 여러 분야를 한 번에 말하면
            # 한 정책을 임의로 고르지 않는다. 관련 정책 후보를 보여주고
            # 사용자가 실제로 판정할 단일 정책을 선택하게 한다.
            interest = normalize_interests(msg)
            state["interest_query"] = interest
            state["focus_policy_id"] = None
            state["selected_policy_id"] = None
            state["_policy_mention"] = None
            state["_rewritten_query"] = None
            state["_policy_candidates"] = _eligibility_candidates_for_interests(bundles, interest)
            tasks = ["ELIGIBILITY"]
            clarify_reasons = ["CLARIFY_POLICY"]
        elif _is_multi_explain_recommend_request(msg):
            # 서로 다른 요구가 한 문장에 있어도 추천 동사 하나만 보고 EXPLAIN을
            # 버리지 않는다. 첫 설명 대상과 추천 관심 분야를 각각 보존한다.
            state["_policy_mention"] = _extract_multi_policy_mention(msg)
            state["_rewritten_query"] = state["_policy_mention"]
            interest = normalize_interests(msg)
            if interest:
                state["interest_query"] = interest
            tasks = ["EXPLAIN", "RECOMMEND"]
            if "조건 없이" in msg:
                state["_skip_profile_check"] = True
                state["_explore_mode"] = True
            elif not interest:
                clarify_reasons = ["CLARIFY_PREFERENCE"]
            elif get_missing_profile_fields(state["profile"]):
                clarify_reasons = ["CLARIFY_PROFILE"]
        elif _is_multi_explain_eligibility_request(msg):
            state["_policy_mention"] = _extract_multi_policy_mention(msg)
            state["_rewritten_query"] = state["_policy_mention"]
            tasks = ["EXPLAIN", "ELIGIBILITY"]
        elif (
            _contains_exact_policy_name(bundles, msg)
            and re.search(r"(설명해\s*줘|알려\s*줘|뭐야)[?!.]?$", msg)
        ):
            # 정책명이 정확히 들어간 설명 요청은 이름에 '정책/지원/청년' 같은
            # 일반 힌트가 없어도 GPT Intent 분류로 보내지 않는다.
            state["_policy_mention"] = _extract_policy_mention(msg)
            state["_rewritten_query"] = state["_policy_mention"]
            tasks = ["EXPLAIN"]
        elif _is_broad_interest_policy_request(msg, bundles):
            # "창업 정책 알려줘"처럼 분야 전체를 묻는 요청은 임의의 정책
            # 하나를 설명하지 않고, 개인 판정 없는 분야별 탐색 목록을 보여준다.
            state["interest_query"] = normalize_interests(msg)
            state["_skip_profile_check"] = True
            state["_explore_mode"] = True
            state["_policy_mention"] = None
            state["_rewritten_query"] = None
            tasks = ["RECOMMEND"]
        elif msg.endswith("정책에 대해 알려줘"):
            policy_name = msg.removesuffix("정책에 대해 알려줘").strip()
            state["_policy_mention"] = policy_name
            tasks = ["EXPLAIN"]
        elif _is_explicit_policy_explain_request(msg):
            # 명확한 정책 설명 요청은 LLM Intent 분류의 변동성에서 분리한다.
            # 예: "청년월세 지원사업 설명해줘", "응시료 지원 정책 알려줘"
            state["_policy_mention"] = _extract_policy_mention(msg)
            state["_rewritten_query"] = state["_policy_mention"]
            tasks = ["EXPLAIN"]
        elif (
            resolve_policy_alias(msg)
            and not _is_explicit_recommend_request(msg)
            and not re.search(
                r"자격\s*(?:이\s*)?(?:확인|봐|검토|되|돼|있)|"
                r"가능한지|되는지\s*(?:안\s*되는지)?|"
                r"참여\s*(?:할\s*)?(?:수\s*있|가능)|"
                r"신청\s*(?:할\s*)?(?:수\s*있|가능)|"
                r"지원\s*(?:받을\s*수\s*있|가능)",
                msg,
            )
        ):
            # "면접 컨설팅", "월세", "자격증비"처럼 동사 없이 핵심
            # 키워드만 입력해도 공식 별칭으로 연결되는 정책을 설명한다.
            state["_policy_mention"] = msg
            state["_rewritten_query"] = msg
            tasks = ["EXPLAIN"]
        elif msg.endswith("자격 확인해줘") or msg.endswith("자격확인해줘"):
            policy_name = msg.replace("자격 확인해줘", "").replace("자격확인해줘", "").strip()
            state["_policy_mention"] = policy_name
            tasks = ["ELIGIBILITY"]
        elif _is_explicit_recommend_request(msg):
            interest = normalize_interests(msg)
            if interest:
                state["interest_query"] = interest
            else:
                # 새 추천에 분야가 없으면 이전 추천 분야를 재사용하지 않는다.
                state["interest_query"] = None
            tasks = ["RECOMMEND"]
            if "조건 없이" in msg:
                state["_skip_profile_check"] = True
                state["_explore_mode"] = True
            elif not interest:
                clarify_reasons = ["CLARIFY_PREFERENCE"]
            elif get_missing_profile_fields(state["profile"]):
                clarify_reasons = ["CLARIFY_PROFILE"]
        elif msg.endswith("분야 정책 추천해줘"):
            interest = normalize_interests(msg.replace("분야 정책 추천해줘", "").strip())
            state["interest_query"] = interest
            tasks = ["RECOMMEND"]
            # Profile 부족하면 Clarify 필요
            if get_missing_profile_fields(state["profile"]):
                clarify_reasons = ["CLARIFY_PROFILE"]
        elif "추천해줘" in msg and ("분야" in msg or "조건 없이" in msg or "맞춤" in msg):
            state["interest_query"] = msg.replace("추천해줘", "").replace("조건 없이", "").replace("맞춤", "").replace("전체", "").strip() or "전체"
            tasks = ["RECOMMEND"]
            if "조건 없이" in msg:
                state["_skip_profile_check"] = True
                state["_explore_mode"] = True
            elif get_missing_profile_fields(state["profile"]):
                clarify_reasons = ["CLARIFY_PROFILE"]
        elif msg == "프로필 입력 완료, 맞춤 추천해줘":
            tasks = ["RECOMMEND"]
        
        # 패턴 매칭 안 되면 → Prompt A로 Intent 분석
        if tasks is None:
            tasks, clarify_reasons = analyze_user_turn(state, user_message)

        inferred_action = state.pop("_normalized_action", None)
        if inferred_action:
            if inferred_action["action"] in {"RESET", "CLARIFY"}:
                _, _, direct_response = apply_action_transition(state, inferred_action, bundles)
                state["messages"].append({"role": "assistant", "content": direct_response})
                return state, direct_response
            tasks = list(inferred_action.get("tasks") or tasks)
            if not state.get("_intent_workflow"):
                state["_intent_workflow"] = [{
                    "action": inferred_action["action"],
                    "task": tasks[0] if len(tasks) == 1 else None,
                    "policy_id": inferred_action.get("policy_id"),
                    "policy_mention": inferred_action.get("policy_mention"),
                    "topic": inferred_action.get("topic"),
                    "exclude_topics": inferred_action.get("exclude_topics", []),
                    "exclude_policy_ids": inferred_action.get("exclude_policy_ids", []),
                    "follow_up_field": inferred_action.get("follow_up_field"),
                    "use_previous_context": inferred_action.get("use_previous_context", False),
                }]

        # 코드 레벨 보정: "자격 확인" 정확한 구문일 때만 ELIGIBILITY 보장
        # "자격증비", "자격증" 같은 단어는 제외
        msg_lower = user_message.lower() if user_message else ""
        is_eligibility_request = False
        eligibility_pattern = re.search(
            r"자격\s*(?:이\s*)?(?:확인|되|돼|있)|"
            r"가능한지|되는지\s*(?:안\s*되는지)?|나도\s*가능|"
            r"참여\s*(?:할\s*)?(?:수\s*있|가능)|"
            r"신청\s*(?:할\s*)?(?:수\s*있|가능)|"
            r"지원\s*(?:받을\s*수\s*있|가능)|해당되는지|대상인지",
            msg_lower,
        )
        if eligibility_pattern:
            # "자격증"이 포함된 경우는 자격확인이 아님 (자격증비, 자격증 시험 등)
            if "자격증" not in msg_lower:
                is_eligibility_request = True
        
        if is_eligibility_request and not _is_multi_interest_eligibility_request(user_message):
            # 설명을 함께 요청한 복합문장이 아니면 GPT가 EXPLAIN을 추가했더라도 제거한다.
            if _is_multi_explain_eligibility_request(user_message):
                if "ELIGIBILITY" not in tasks:
                    tasks.append("ELIGIBILITY")
            else:
                tasks = ["ELIGIBILITY"]
            # 정책명도 지시어도 없는 새 자격 요청은 과거 focus를 재사용하지 않는다.
            if not state.get("_policy_mention") and not _has_focus_reference(user_message):
                state["focus_policy_id"] = None
                state["selected_policy_id"] = None
            # GPT가 축약 정책명을 놓쳐도 공식 별칭 사전으로 대상 정책을 확정한다.
            if not state.get("_policy_mention"):
                alias_policy_id = resolve_policy_alias(user_message)
                alias_bundle = next(
                    (bundle for bundle in bundles if bundle["policy_id"] == alias_policy_id),
                    None,
                )
                if alias_bundle:
                    state["_policy_mention"] = alias_bundle["policy_name"]
            if any(ref in user_message for ref in ["나도 가능", "저도 가능", "내가 가능", "나도 되", "저도 되", "이 정책 가능", "그 정책 가능"]):
                state["_policy_mention"] = user_message
            for suffix in [
                "자격 확인해줘", "자격확인해줘", "가능한지 알아보기",
                "참여할 수 있어?", "참여할 수 있나요?", "참여 가능해요?", "참여 가능해?",
                "신청할 수 있어?", "신청할 수 있나요?", "신청 가능해요?", "신청 가능해?",
                "지원받을 수 있어?", "지원받을 수 있나요?", "지원 가능해요?", "지원 가능해?",
                "자격 확인", "가능해?",
            ]:
                if suffix in user_message:
                    mention = user_message.replace(suffix, "").strip()
                    if mention and mention not in ("나도", "저도", "내가"):
                        state["_policy_mention"] = mention
                    break

    
    # 추천 분야는 구조화해 저장하되 GPT 의미 판정에는 사용자의 원문도
    # 함께 보존한다. '정장 대여', 정확한 정책명, 부정 표현 같은 의미가
    # 단순 관심분야명으로 축약되며 사라지는 것을 막는다.
    if not ui_event and tasks and "RECOMMEND" in tasks and user_message.strip():
        state["_recommend_query"] = user_message.strip()

    # 4. Profile patch 적용 (analyze에서 추출된 경우)
    # 이미 analyze_user_turn 내부에서 처리됨
    
    # 5. 모든 단일/복합 요청을 같은 Workflow 실행기로 보낸다.
    validated_clarifies = validate_clarify_reasons(state, tasks, clarify_reasons)

    if tasks:
        start_active_workflow(
            state,
            tasks,
            user_message,
            bundles=bundles,
            clarify_reasons=validated_clarifies,
        )
        response = execute_active_workflow(state, collection, bundles)
    else:
        user_msg_lower = (user_message or "").lower()
        if any(word in user_msg_lower for word in ["확정", "보장", "무조건"]):
            response = (
                "자격 확인 결과는 안내형 가능성이며, 실제 합격이나 선정을 보장하지 않습니다. "
                "정확한 확인은 공식 홈페이지나 담당 부서에 문의해주세요."
            )
        elif any(word in user_msg_lower for word in ["추측", "알아서", "적당히"]):
            response = (
                "개인 정보를 추측하여 채울 수 없습니다. "
                "정확한 맞춤 추천을 위해 직접 입력해주세요. 무엇을 도와드릴까요?"
            )
        else:
            response = "무엇을 도와드릴까요? 정책 설명, 맞춤 추천, 자격 확인 중 선택해주세요."

    state["messages"].append({"role": "assistant", "content": response})
    return state, response


def route_ui_event(event, state):
    """UI 버튼 이벤트를 task로 변환"""
    event_map = {
        "ASK_POLICY": (["EXPLAIN"], ["CLARIFY_POLICY"]),
        "REQUEST_RECOMMENDATION": (["RECOMMEND"], ["CLARIFY_PREFERENCE"]),
        "START_RECOMMEND": (["RECOMMEND"], []),
        "SUBMIT_RECOMMEND_PROFILE": (["RECOMMEND"], []),
        "CHECK_ELIGIBILITY": (["ELIGIBILITY"], ["CLARIFY_POLICY"]),
        "START_ELIGIBILITY": (["ELIGIBILITY"], ["CLARIFY_POLICY"]),
        "SUBMIT_PROFILE": (state.get("pending_tasks", []) or ["ELIGIBILITY"], []),
        "SUBMIT_ADDITIONAL_ANSWERS": (["ELIGIBILITY"], []),
        "EDIT_ADDITIONAL_ANSWERS": (["ELIGIBILITY"], []),
    }
    return event_map.get(event, ([], []))


def analyze_user_turn(state, user_message):
    """Prompt A 한 번으로 Intent, Action, 복합 Workflow를 함께 구조화한다."""
    recent = state["messages"][-6:] if len(state["messages"]) > 6 else state["messages"]
    recent_text = "\n".join([f"{m['role']}: {m['content']}" for m in recent])

    prompt = PROMPT_A_INTENT.format(
        focus_policy_id=state.get("focus_policy_id"),
        profile=json.dumps(state["profile"], ensure_ascii=False),
        interest_query=state.get("interest_query"),
        user_message=user_message,
        recent_messages=recent_text,
    )
    result = parse_json_response(call_openai(prompt, json_mode=True))
    if not result:
        state["_normalized_action"] = validate_action_payload({
            "action": "CLARIFY",
            "tasks": [],
            "confidence": "low",
            "clarify_reason": "INVALID_MODEL_OUTPUT",
        })
        return [], []

    raw_tasks = result.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    tasks = [task for task in ("EXPLAIN", "RECOMMEND", "ELIGIBILITY") if task in raw_tasks]
    raw_clarifies = result.get("clarify_reasons", [])
    if not isinstance(raw_clarifies, list):
        raw_clarifies = []
    clarify_reasons = [
        item for item in raw_clarifies
        if item in ("CLARIFY_POLICY", "CLARIFY_PREFERENCE", "CLARIFY_PROFILE")
    ]

    turn_kind = result.get("turn_kind")
    if turn_kind not in {"CLARIFY_ANSWER", "NEW_TASK", "SWITCH_POLICY", "CANCEL", "SMALL_TALK"}:
        turn_kind = "NEW_TASK" if tasks else "SMALL_TALK"
    reuse_focus = result.get("reuse_focus") is True and turn_kind != "SWITCH_POLICY"
    confidence = result.get("confidence")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    action = validate_action_payload({
        "action": result.get("action"),
        "tasks": tasks,
        "topic": result.get("topic") or result.get("interest_query"),
        "policy_mention": result.get("policy_mention"),
        "exclude_topics": result.get("exclude_topics"),
        "exclude_policy_mentions": result.get("exclude_policy_mentions"),
        "follow_up_field": result.get("follow_up_field"),
        "use_previous_context": result.get("use_previous_context"),
        "confidence": confidence,
        "workflow": result.get("workflow"),
        "explore_without_profile": result.get("explore_without_profile"),
    })
    if not action and turn_kind in {"NEW_TASK", "SWITCH_POLICY", "CANCEL"}:
        # 구버전/테스트 payload는 기존 전환 규칙을 유지한다.
        reset_task_context(state, keep_focus=reuse_focus, keep_interest=False)

    patch = result.get("profile_patch", {})
    if isinstance(patch, dict):
        clean_patch = {key: value for key, value in patch.items() if value is not None}
        if clean_patch:
            update_profile(state, clean_patch)

    workflow = []
    workflow_invalid = False
    raw_workflow = result.get("workflow", [])
    if isinstance(raw_workflow, list):
        for raw_step in raw_workflow:
            if not isinstance(raw_step, dict) or raw_step.get("task") not in VALID_TASKS:
                continue
            step_task = raw_step["task"]
            step_action = validate_action_payload({
                "action": raw_step.get("action") or (
                    action.get("action") if action and len(tasks) == 1 else "NORMAL"
                ),
                "tasks": [step_task],
                "policy_mention": raw_step.get("policy_mention"),
                "topic": raw_step.get("topic"),
                "use_previous_context": raw_step.get("use_previous_context"),
                "exclude_topics": raw_step.get("exclude_topics"),
                "exclude_policy_mentions": raw_step.get("exclude_policy_mentions"),
                "follow_up_field": raw_step.get("follow_up_field"),
                "explore_without_profile": raw_step.get("explore_without_profile"),
                "confidence": confidence,
            })
            if not step_action or step_action["action"] == "CLARIFY":
                workflow_invalid = True
                continue
            workflow.append({
                "action": step_action["action"],
                "task": step_task,
                "policy_mention": str(raw_step.get("policy_mention") or "").strip() or None,
                "topic": (_clean_action_topics(raw_step.get("topic")) or [None])[0],
                "use_previous_context": step_action.get("use_previous_context", False),
                "exclude_topics": step_action.get("exclude_topics", []),
                "exclude_policy_ids": step_action.get("exclude_policy_ids", []),
                "exclude_policy_mentions": step_action.get("exclude_policy_mentions", []),
                "follow_up_field": step_action.get("follow_up_field"),
                "explore_without_profile": step_action.get("explore_without_profile", False),
            })
    if workflow_invalid:
        action = validate_action_payload({
            "action": "CLARIFY",
            "tasks": [],
            "confidence": "low",
            "clarify_reason": "INVALID_WORKFLOW_STEP",
        })
        tasks = []
        clarify_reasons = []
        workflow = []
    if action:
        state["_normalized_action"] = action
    if workflow:
        state["_intent_workflow"] = workflow

    # Action이 없을 때만 기존 직접 상태 반영을 사용한다. Action이 있으면
    # apply_action_transition이 이전 Context를 본 뒤 원자적으로 전환한다.
    if not action:
        interest_value = result.get("interest_query")
        if interest_value:
            normalized_interest = normalize_interests(interest_value)
            state["interest_query"] = normalized_interest or interest_value
        if result.get("policy_mention"):
            state["_policy_mention"] = result["policy_mention"]

    if result.get("rewritten_query"):
        state["_rewritten_query"] = result["rewritten_query"]

    state["_intent_turn_kind"] = turn_kind
    state["_intent_reuse_focus"] = reuse_focus
    state["_intent_confidence"] = confidence
    return tasks, clarify_reasons


def validate_clarify_reasons(state, tasks, clarify_reasons):
    """Clarify 필요 여부를 State와 대조하여 검증"""
    validated = []
    
    for reason in clarify_reasons:
        if reason == "CLARIFY_POLICY":
            # EXPLAIN이나 ELIGIBILITY에서 정책 대상이 없는 경우
            if ("EXPLAIN" in tasks or "ELIGIBILITY" in tasks) and not state.get("focus_policy_id"):
                if not state.get("_policy_mention"):
                    validated.append(reason)
        elif reason == "CLARIFY_PREFERENCE":
            # RECOMMEND에서 관심 분야가 없는 경우
            if "RECOMMEND" in tasks and not state.get("interest_query"):
                validated.append(reason)
        elif reason == "CLARIFY_PROFILE":
            # RECOMMEND에서 Profile이 부족한 경우 (COMPLETE이면 안 물어봄)
            if "RECOMMEND" in tasks and get_profile_status(state["profile"]) != "COMPLETE":
                missing = get_missing_profile_fields(state["profile"])
                if missing:
                    validated.append(reason)
    
    # 우선순위: POLICY > PREFERENCE > PROFILE
    priority = ["CLARIFY_POLICY", "CLARIFY_PREFERENCE", "CLARIFY_PROFILE"]
    validated.sort(key=lambda x: priority.index(x) if x in priority else 99)
    
    return validated


def generate_clarify_question(state, clarify_type):
    """Clarify 질문 생성"""
    if clarify_type == "CLARIFY_POLICY" and "ELIGIBILITY" in state.get("pending_tasks", []):
        return "어떤 정책의 자격을 확인하고 싶으신가요?\n아래에서 선택해주세요."
    if clarify_type == "CLARIFY_PROFILE":
        missing = get_missing_profile_fields(state["profile"])
        if missing:
            # 실제 입력 항목은 바로 아래 Profile 카드가 담당한다. 말풍선에도
            # 같은 질문을 반복하면 모바일에서 중복되고 길어지므로 안내만 한다.
            return "맞춤 추천을 위해 몇 가지 여쭤볼게요! 😊"
    
    return CLARIFY_MESSAGES.get(clarify_type, "추가 정보가 필요합니다. 자세히 알려주세요.")


def handle_clarify_answer(state, user_message, collection, bundles):
    """Clarify 답변을 저장하고 단일/복합 Workflow를 정확한 지점부터 재개한다."""
    clarify_type = state["active_clarify"]

    if clarify_type == "CLARIFY_POLICY":
        state["_policy_mention"] = user_message
        workflow = state.get("active_workflow")
        if isinstance(workflow, dict):
            index = workflow.get("index", 0)
            steps = workflow.get("steps", [])
            if index < len(steps):
                steps[index]["policy_mention"] = user_message
        state["active_clarify"] = None
        state["_policy_candidates"] = None

    elif clarify_type == "CLARIFY_PREFERENCE":
        state["interest_query"] = normalize_interests(user_message) or user_message
        workflow = state.get("active_workflow")
        if isinstance(workflow, dict):
            index = workflow.get("index", 0)
            steps = workflow.get("steps", [])
            if index < len(steps):
                steps[index]["topic"] = state["interest_query"]
        state["active_clarify"] = None

    elif clarify_type == "CLARIFY_ADDITIONAL":
        aq = state.get("_active_additional_q", {})
        policy_id = aq.get("policy_id", state.get("selected_policy_id"))
        if "|" in user_message or ":" in user_message:
            for pair in user_message.split("|"):
                if ":" in pair:
                    qid, answer = pair.split(":", 1)
                    save_policy_answer(state, policy_id, qid.strip(), answer.strip())
        else:
            questions = aq.get("questions", [])
            if questions:
                save_policy_answer(
                    state, policy_id, questions[0]["question_id"], user_message.strip()
                )
        state["active_clarify"] = None
        state["_skip_profile_check"] = True
        state.pop("_active_additional_q", None)
        state["selected_policy_id"] = policy_id
        state["current_policy_id"] = policy_id

    elif clarify_type == "CLARIFY_PROFILE":
        if (
            "프로필 입력 완료" in user_message
            or "프로필 설정 완료" in user_message
            or "추천" in user_message
        ):
            state["active_clarify"] = None
            state["_skip_profile_check"] = True
        else:
            explicit_age = extract_explicit_age(user_message)
            if explicit_age is not None:
                update_profile(state, {"age": explicit_age})
            else:
                parse_prompt = f"""사용자가 Profile 정보를 입력했습니다. 다음 JSON으로 추출하세요.
사용자 답변: "{user_message}"

{{"age": null 또는 정수, "residency": null 또는 "예"/"아니오", "employment": null 또는 "취업"/"미취업", "student": null 또는 해당값, "startup": null 또는 해당값, "housing": null 또는 해당값, "marriage": null 또는 해당값}}

규칙: 명확히 알 수 있는 값만 채우고 나머지는 null로 두세요. "만 24세" → age: 24"""
                patch = parse_json_response(call_openai(parse_prompt, json_mode=True))
                if patch:
                    clean_patch = {key: value for key, value in patch.items() if value is not None}
                    if clean_patch:
                        update_profile(state, clean_patch)
            state["active_clarify"] = None

    if state.get("active_workflow"):
        return execute_active_workflow(state, collection, bundles)

    pending = state.get("pending_tasks", [])
    if not state.get("_skip_profile_check"):
        remaining = validate_clarify_reasons(
            state,
            pending,
            ["CLARIFY_POLICY", "CLARIFY_PREFERENCE", "CLARIFY_PROFILE"],
        )
        if remaining:
            state["active_clarify"] = remaining[0]
            return generate_clarify_question(state, remaining[0])

    tasks = pending
    original_query = state.get("_original_query", "")
    state["pending_tasks"] = []
    state["_original_query"] = None
    task_results = state.pop("_partial_results", {})
    for task in sort_tasks(tasks):
        if task in task_results:
            continue
        if task == "EXPLAIN":
            task_results[task] = run_explain(
                state, state.get("_policy_mention") or original_query, collection
            )
        elif task == "RECOMMEND":
            task_results[task] = run_recommend(state, bundles)
        elif task == "ELIGIBILITY":
            task_results[task] = run_eligibility(state, bundles)
    return (
        compose_multi_response(task_results)
        if len(task_results) > 1
        else next(iter(task_results.values()), "무엇을 도와드릴까요?")
    )


def _policy_id_from_result_reference(state, text):
    """'그중 두 번째' 같은 추천 결과 지시어를 직전 결과 ID로 고정한다."""
    value = re.sub(r"\s+", "", text or "")
    indexes = {
        "첫번째": 0, "첫째": 0, "1번": 0,
        "두번째": 1, "둘째": 1, "2번": 1,
        "세번째": 2, "셋째": 2, "3번": 2,
        "네번째": 3, "넷째": 3, "4번": 3,
        "다섯번째": 4, "다섯째": 4, "5번": 4,
    }
    ids = state.get("last_result_policy_ids") or []
    for token, index in indexes.items():
        if token in value and index < len(ids):
            return ids[index]
    return None


def _normalize_workflow_steps(state, tasks, bundles=None, original_query=""):
    """각 Atomic Unit의 Action+Task와 대상을 State 변경 전에 고정한다."""
    bundles = bundles or []
    raw_steps = state.pop("_intent_workflow", [])
    previous_policy_id = state.get("current_policy_id") or state.get("focus_policy_id")
    previous_topic = state.get("current_topic") or state.get("interest_query")
    workflow_policy_id = previous_policy_id
    workflow_topic = previous_topic
    steps = []

    for raw in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(raw, dict) or raw.get("task") not in VALID_TASKS:
            continue
        task = raw["task"]
        action = validate_action_payload({
            "action": raw.get("action") or "NORMAL",
            "tasks": [task],
            "policy_id": raw.get("policy_id"),
            "policy_mention": raw.get("policy_mention"),
            "topic": raw.get("topic"),
            "exclude_topics": raw.get("exclude_topics"),
            "exclude_policy_ids": raw.get("exclude_policy_ids"),
            "exclude_policy_mentions": raw.get("exclude_policy_mentions"),
            "follow_up_field": raw.get("follow_up_field"),
            "use_previous_context": raw.get("use_previous_context"),
            "explore_without_profile": raw.get("explore_without_profile"),
            "confidence": "high",
        })
        if not action or action["action"] == "CLARIFY":
            state["_workflow_build_error"] = "INVALID_ACTION_TASK"
            continue

        explicit_policy_id = _policy_id_for_action(action, bundles)
        result_policy_id = _policy_id_from_result_reference(
            state,
            action.get("policy_mention") or original_query,
        )
        resolved_policy_id = explicit_policy_id or result_policy_id
        if action["action"] == "FOLLOW_UP" and not resolved_policy_id:
            # 같은 발화의 앞 Atomic Unit이 정책을 확정했다면 세션 시작 전
            # policy보다 그 정책을 먼저 이어받는다. 예: "월세 설명하고 나도 돼?"
            resolved_policy_id = workflow_policy_id or previous_policy_id

        topic = action.get("topic")
        if task == "RECOMMEND" and action["action"] == "FOLLOW_UP" and not topic:
            topic = _topic_for_policy_id(resolved_policy_id, bundles)

        exclude_policy_ids = list(action.get("exclude_policy_ids") or [])
        exclude_topics = list(action.get("exclude_topics") or [])
        for mention in action.get("exclude_policy_mentions") or []:
            excluded_id = _policy_id_for_action({
                "policy_id": None,
                "policy_mention": mention,
            }, bundles)
            if excluded_id:
                exclude_policy_ids.append(excluded_id)
        if action["action"] == "SHOW_ALTERNATIVES":
            if not exclude_policy_ids and workflow_policy_id and workflow_policy_id != resolved_policy_id:
                exclude_policy_ids.append(workflow_policy_id)
            elif not exclude_policy_ids and workflow_topic and not exclude_topics:
                exclude_topics.extend(_clean_action_topics(workflow_topic))

        steps.append({
            "action": action["action"],
            "task": task,
            "policy_id": resolved_policy_id,
            "policy_mention": action.get("policy_mention"),
            "topic": topic,
            "exclude_topics": list(dict.fromkeys(exclude_topics)),
            "exclude_policy_ids": list(dict.fromkeys(exclude_policy_ids)),
            "follow_up_field": action.get("follow_up_field"),
            "use_previous_context": action.get("use_previous_context") or action["action"] == "FOLLOW_UP",
            "explore_without_profile": action.get("explore_without_profile", False),
            "transition_applied": False,
            "pre_clarifies": [],
        })
        if resolved_policy_id:
            workflow_policy_id = resolved_policy_id
        if topic:
            workflow_topic = topic

    if not steps:
        mention = state.get("_policy_mention")
        topic = state.get("interest_query")
        for task in sort_tasks(tasks):
            policy_mention = mention if task in {"EXPLAIN", "ELIGIBILITY"} else None
            action = "NORMAL"
            resolved_policy_id = None
            if policy_mention:
                resolved_policy_id = _policy_id_for_action({
                    "policy_mention": policy_mention,
                    "policy_id": None,
                }, bundles)
            steps.append({
                "action": action,
                "task": task,
                "policy_id": resolved_policy_id,
                "policy_mention": policy_mention,
                "topic": topic if task == "RECOMMEND" else None,
                "exclude_topics": [],
                "exclude_policy_ids": [],
                "follow_up_field": None,
                "use_previous_context": False,
                "explore_without_profile": False,
                "transition_applied": False,
                "pre_clarifies": [],
            })

    # 기존 구조처럼 같은 Task 중복은 막되 Prompt가 지정한 서로 다른 Task 순서는 유지한다.
    result, seen = [], set()
    for step in steps:
        task = step["task"]
        if task in seen:
            continue
        seen.add(task)
        result.append(step)
    return result


def start_active_workflow(
    state,
    tasks,
    original_query,
    bundles=None,
    clarify_reasons=None,
):
    steps = _normalize_workflow_steps(
        state,
        tasks,
        bundles=bundles,
        original_query=original_query,
    )
    if not steps:
        return None
    for reason in clarify_reasons or []:
        target_task = {
            "CLARIFY_POLICY": {"EXPLAIN", "ELIGIBILITY"},
            "CLARIFY_PREFERENCE": {"RECOMMEND"},
            "CLARIFY_PROFILE": {"RECOMMEND", "ELIGIBILITY"},
        }.get(reason, set())
        step = next((item for item in steps if item["task"] in target_task), None)
        if step and reason not in step["pre_clarifies"]:
            step["pre_clarifies"].append(reason)
    workflow = {
        "steps": steps,
        "index": 0,
        "results": {},
        "original_query": original_query or "",
        "build_error": state.pop("_workflow_build_error", None),
    }
    state["active_workflow"] = workflow
    state["pending_tasks"] = [step["task"] for step in steps]
    state["_original_query"] = original_query or ""
    return workflow


def _workflow_candidates(state, bundles):
    ids = state.get("last_result_policy_ids") or []
    return [
        {"policy_id": bundle["policy_id"], "policy_name": bundle["policy_name"]}
        for bundle in bundles
        if bundle["policy_id"] in ids and bundle.get("eligibility_mode") != "INFO_ONLY"
    ][:8]


def _workflow_pause_response(workflow, task, question):
    """수집 중인 결과는 노출 범위를 제어하고 최종 묶음 답변을 예고한다."""
    results = workflow.get("results", {})
    steps = workflow.get("steps", [])
    if task == "EXPLAIN" and any(step.get("task") == "RECOMMEND" for step in steps):
        recommend_step = next(
            (step for step in steps if step.get("task") == "RECOMMEND"),
            {},
        )
        topic = recommend_step.get("topic")
        next_text = (
            f"그다음 {topic} 분야 추천까지 이어갈게요."
            if topic
            else "그다음 맞춤 추천까지 이어갈게요."
        )
        return (
            "먼저 설명할 정책을 정확히 선택해주세요. "
            + next_text
            + " 두 단계가 끝나면 한 답변으로 묶어드릴게요.\n\n"
            + question
        )
    if task == "RECOMMEND" and "EXPLAIN" in results:
        return (
            "정책 설명은 준비해두었어요. 맞춤 추천에 필요한 정보까지 확인한 뒤 "
            "두 결과를 한 답변으로 묶어드릴게요.\n\n" + question
        )
    if task == "ELIGIBILITY" and "EXPLAIN" in results:
        return (
            "자격 확인에 필요한 정보를 먼저 받을게요. 확인이 끝나면 "
            "정책 설명과 자격 결과를 함께 정리해드릴게요.\n\n" + question
        )
    if task == "ELIGIBILITY" and "RECOMMEND" in results:
        return (
            compose_multi_response(
                {"RECOMMEND": results["RECOMMEND"]},
                steps=workflow.get("steps", []),
            )
            + "\n\n추천 정책 중 자격을 확인할 정책을 선택해주세요.\n\n"
            + question
        )
    return question


def execute_active_workflow(state, collection, bundles):
    """복합 task를 순서대로 실행하며 Clarify에서 멈추고 다음 턴에 재개한다."""
    workflow = state.get("active_workflow")
    if not isinstance(workflow, dict):
        return "무엇을 도와드릴까요?"
    if workflow.get("build_error"):
        state["active_workflow"] = None
        state["pending_tasks"] = []
        state["last_action"] = "CLARIFY"
        state["last_intent_failure"] = workflow["build_error"]
        return _intent_clarify_response(workflow["build_error"])
    steps = workflow.get("steps", [])
    results = workflow.setdefault("results", {})

    while workflow.get("index", 0) < len(steps):
        index = workflow["index"]
        step = steps[index]
        task = step["task"]
        mention = step.get("policy_mention")

        # 추천 뒤 자격 대상이 명시되지 않았으면 첫 후보를 임의 선택하지 않는다.
        if (
            task == "ELIGIBILITY"
            and not mention
            and not step.get("policy_id")
            and "RECOMMEND" in results
        ):
            candidates = _workflow_candidates(state, bundles)
            state["_policy_candidates"] = candidates or None
            state["active_clarify"] = "CLARIFY_POLICY"
            state["pending_tasks"] = [item["task"] for item in steps[index:]]
            question = "어떤 정책의 자격을 확인할까요?\n아래에서 선택해주세요."
            return _workflow_pause_response(workflow, task, question)

        if not step.get("transition_applied"):
            _, transition_clarifies, direct_response = apply_action_transition(
                state,
                {
                    "action": step.get("action") or "NORMAL",
                    "tasks": [task],
                    "policy_id": step.get("policy_id"),
                    "policy_mention": mention,
                    "topic": step.get("topic"),
                    "exclude_topics": step.get("exclude_topics", []),
                    "exclude_policy_ids": step.get("exclude_policy_ids", []),
                    "follow_up_field": step.get("follow_up_field"),
                    "use_previous_context": step.get("use_previous_context", False),
                    "explore_without_profile": step.get("explore_without_profile", False),
                    "confidence": "high",
                },
                bundles,
                preserve_workflow=True,
            )
            step["transition_applied"] = True
            if direct_response:
                state["active_workflow"] = None
                state["pending_tasks"] = []
                return direct_response
            clarifies = list(dict.fromkeys(
                list(step.get("pre_clarifies") or []) + list(transition_clarifies or [])
            ))
            step["pre_clarifies"] = []
            if clarifies:
                clarify = clarifies[0]
                state["active_clarify"] = clarify
                state["pending_tasks"] = [item["task"] for item in steps[index:]]
                if clarify == "CLARIFY_PROFILE" and task == "RECOMMEND":
                    state["_missing_fields"] = []
                question = generate_clarify_question(state, clarify)
                return _workflow_pause_response(workflow, task, question)

        if task == "EXPLAIN":
            if step.get("action") == "FOLLOW_UP":
                result = run_policy_follow_up(state, bundles)
            else:
                result = run_explain(
                    state,
                    mention or workflow.get("original_query", ""),
                    collection,
                )
        elif task == "RECOMMEND":
            state["_recommend_query"] = workflow.get("original_query", "")
            result = run_recommend(state, bundles)
        else:
            result = run_eligibility(state, bundles)

        if state.get("active_clarify"):
            state["pending_tasks"] = [item["task"] for item in steps[index:]]
            return _workflow_pause_response(workflow, task, result)

        results[task] = result
        state["last_action"] = step.get("action") or "NORMAL"
        state["last_task"] = task
        workflow["index"] = index + 1
        state["pending_tasks"] = [item["task"] for item in steps[index + 1:]]

    final_results = dict(results)
    state["active_workflow"] = None
    state["pending_tasks"] = []
    state["last_tasks"] = [step["task"] for step in steps]
    state["last_task"] = state["last_tasks"][-1] if state["last_tasks"] else None
    state["_original_query"] = None
    state.pop("_partial_results", None)
    return (
        compose_multi_response(final_results, steps=steps, bundles=bundles)
        if len(final_results) > 1
        else next(iter(final_results.values()), "무엇을 도와드릴까요?")
    )


def sort_tasks(tasks):
    """EXPLAIN → RECOMMEND → ELIGIBILITY 순서 정렬"""
    order = {"EXPLAIN": 0, "RECOMMEND": 1, "ELIGIBILITY": 2}
    return sorted(tasks, key=lambda t: order.get(t, 99))


# === EXPLAIN ===
def run_explain(state, query, collection):
    """EXPLAIN 파이프라인: VectorStore 검색 → 정책 선택 → 설명 생성"""
    # policy_mention이 있으면 query로 사용
    search_query = state.get("_policy_mention") or query
    if not search_query:
        return "어떤 정책에 대해 알고 싶으신지 말씀해주세요."

    policy_references = {"이거", "이 정책", "그거", "그 정책", "저 정책", "위에 거"}
    if search_query.strip() in policy_references and not state.get("focus_policy_id"):
        state["active_clarify"] = "CLARIFY_POLICY"
        state["pending_tasks"] = ["EXPLAIN"]
        return "어떤 정책을 말씀하시는지 확인이 필요해요. 정책명이나 관련 키워드를 알려주세요."

    alias_policy_id = resolve_policy_alias(search_query)
    if alias_policy_id:
        selected = get_policy_from_collection(collection, alias_policy_id)
        if selected:
            return _generate_explanation(state, selected)

    # 공식 정책명 전체가 직접 들어간 경우에만 임베딩보다 이름 매칭을 우선한다.
    # 일부 일반 단어가 이름에 걸렸다는 이유로 고르는 느슨한 부분 매칭은 사용하지 않는다.
    explicit_policy_id = _explicit_policy_id_from_name(search_query)
    if explicit_policy_id:
        selected = get_policy_from_collection(collection, explicit_policy_id)
        if selected:
            return _generate_explanation(state, selected)
    
    # focus_policy_id가 이미 있고, 검색어가 "이거", "이 정책" 같은 지시어면 → 바로 사용
    focus_id = state.get("focus_policy_id")
    if focus_id and search_query.strip() in ["이거", "이 정책", "그거", "그 정책", "위에 거"]:
        # focus 정책을 VectorStore에서 직접 가져오기
        result = collection.get(ids=[focus_id], include=["documents", "metadatas"])
        if result and result["ids"]:
            selected = {
                "policy_id": focus_id,
                "policy_name": result["metadatas"][0]["policy_name"],
                "category": result["metadatas"][0]["category"],
                "source": result["metadatas"][0]["source"],
                "content": result["documents"][0],
            }
            return _generate_explanation(state, selected)
    
    # Query Rewriting: Prompt A에서 이미 추출된 rewritten_query 사용
    rewritten = state.get("_rewritten_query") or search_query
    
    # 원본 + 재작성 둘 다 검색
    candidates_original = retrieve_policy_candidates(collection, search_query)
    candidates_rewritten = retrieve_policy_candidates(collection, rewritten) if rewritten != search_query else []
    
    # 두 결과 합치기 (중복 제거, 원본 우선)
    seen_ids = set()
    candidates = []
    for c in candidates_original + candidates_rewritten:
        if c["policy_id"] not in seen_ids:
            seen_ids.add(c["policy_id"])
            candidates.append(c)
    candidates = candidates[:5]  # 최대 5개
    
    if not candidates:
        return "관련 정책을 찾지 못했습니다. 다른 키워드로 시도해보세요."
    
    normalized_query = re.sub(r"[^0-9a-z가-힣]", "", search_query.lower())
    exact = [
        c for c in candidates
        if re.sub(r"[^0-9a-z가-힣]", "", c["policy_name"].lower()) in normalized_query
    ]
    if exact:
        return _generate_explanation(state, exact[0])

    broad_queries = {
        "정책", "청년정책", "지원정책", "교육프로그램", "지원사업",
        "청년지원", "남양주시청년지원", "남양주시청년지원정책",
    }
    if normalized_query in broad_queries:
        names = [c["policy_name"] for c in candidates[:3]]
        return "질문 범위가 넓어요. 다음 중 궁금한 정책명을 조금 더 구체적으로 말씀해주세요.\n\n" + "\n".join(f"• {n}" for n in names)

    # 임베딩은 항상 가장 가까운 후보를 돌려주므로, 공식 요약에서 사용자의
    # 핵심어를 다시 확인한다. 직접 근거가 없는 후보는 아무리 1위여도 설명하지 않는다.
    evidenced = []
    for order, candidate in enumerate(candidates):
        score = _policy_query_evidence_score(search_query, candidate)
        if score >= 3:
            distance = candidate.get("distance")
            evidenced.append((score, -(distance if isinstance(distance, (int, float)) else 999), -order, candidate))

    if not evidenced:
        return _no_direct_policy_match_response(search_query)

    evidenced.sort(key=lambda item: item[:3], reverse=True)
    return _generate_explanation(state, evidenced[0][3])


POLICY_QUERY_ALIASES = [
    (("월세", "월새"), "NYJ-YOUTH-011"),
    (("응시료", "자격증비", "시험비", "토익 비용"), "NYJ-YOUTH-004"),
    (("헤어", "스타일링", "머리 손질"), "NYJ-YOUTH-005"),
    (("정장 대여", "정장대여", "정장 빌", "정장 렌탈", "양복 대여", "면접 정장", "면접정장", "정장 무료", "면접 복장", "면접복장", "면접 옷", "면접 사진"), "NYJ-YOUTH-002"),
    (("면접 컨설팅", "면접 코칭", "모의면접", "자소서 컨설팅", "자기소개서 컨설팅"), "NYJ-YOUTH-003"),
    (("취업 로드맵", "공간 대여", "사진 무료 대여"), "NYJ-YOUTH-002"),
    (("말산업", "승마장 인턴", "말농가 인턴"), "NYJ-YOUTH-006"),
    (("행정 체험", "행정체험", "시정업무 체험", "대학생 행정 알바"), "NYJ-YOUTH-007"),
    (("창업 컨설팅", "창업 코칭", "창업 멘토링"), "NYJ-YOUTH-021"),
    (("창업 교육", "창업 강의", "창업 특강", "창업특강"), "NYJ-YOUTH-020"),
    (("정약용 후예", "청년 인재 플랫폼"), "NYJ-YOUTH-008"),
    (("청년꿈틀", "꿈틀 프로젝트", "청년 성장 프로젝트"), "NYJ-YOUTH-009"),
    (("기본소득", "24세 지역화폐", "분기별 25만원"), "NYJ-YOUTH-010"),
    (("내일저축", "저축계좌", "자산 형성", "자산형성"), "NYJ-YOUTH-012"),
    (("주거급여", "부모와 별도 거주"), "NYJ-YOUTH-013"),
    (("마음건강", "마음 건강", "심리 상담", "정신건강 상담"), "NYJ-YOUTH-014"),
    (("정신건강 치료비", "정신질환 치료비", "찾아가는 심리지원"), "NYJ-YOUTH-015"),
    (("입영지원", "입대 지원금", "군 입대", "군인 지원금"), "NYJ-YOUTH-016"),
    (("영농정착", "청년농업인", "영농 초기"), "NYJ-YOUTH-017"),
    (("농업인 자녀", "농어촌 학자금", "학자금 무이자"), "NYJ-YOUTH-018"),
    (("청년 전용 공간", "청년 공간", "커뮤니티 공간"), "NYJ-YOUTH-023"),
    (("해외 연수", "댄스 교육", "음악 교육"), "NYJ-YOUTH-022"),
    (("시정 참여", "봉사활동", "대학생 멘토링"), "NYJ-YOUTH-024"),
    (("청년예술인", "창작 지원", "작품 발표"), "NYJ-YOUTH-025"),
]


def _contains_positive_term(text, term):
    """'미창업', '월세 말고'처럼 부정된 단어는 긍정적 의도로 세지 않는다."""
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        before = text[max(0, index - 3):index]
        after = text[index + len(term):index + len(term) + 7]
        negated_before = before.endswith(("미", "비"))
        negated_after = after.startswith(("말고", "말구", "제외", "빼고", "아니", "하지않", "외"))
        if not negated_before and not negated_after:
            return True
        start = index + len(term)


def _explicit_policy_id_from_name(query):
    compact = re.sub(r"[^0-9a-z가-힣]", "", (query or "").lower())
    matches = []
    for policy_id, summary in _summary_by_policy_id().items():
        name = re.sub(r"[^0-9a-z가-힣]", "", summary.get("policy_name", "").lower())
        if name and name in compact:
            matches.append(policy_id)
    return matches[0] if len(matches) == 1 else None


def resolve_policy_alias(query):
    compact = re.sub(r"\s+", "", (query or "").lower())
    for aliases, policy_id in POLICY_QUERY_ALIASES:
        if any(_contains_positive_term(compact, re.sub(r"\s+", "", alias.lower())) for alias in aliases):
            return policy_id
    return None


def get_policy_from_collection(collection, policy_id):
    result = collection.get(ids=[policy_id], include=["documents", "metadatas"])
    if not result or not result.get("ids"):
        return None
    metadata = result["metadatas"][0]
    return {
        "policy_id": policy_id,
        "policy_name": metadata["policy_name"],
        "category": metadata["category"],
        "source": metadata["source"],
        "content": result["documents"][0],
    }


def _generate_explanation(state, selected):
    """선택된 정책으로 설명 생성 + focus 저장"""
    # focus 저장
    state["focus_policy_id"] = selected["policy_id"]
    state["current_policy_id"] = selected["policy_id"]
    state["_policy_mention"] = None
    state.pop("_rewritten_query", None)
    
    return format_grounded_explanation(selected)


@lru_cache(maxsize=1)
def _summary_by_policy_id():
    return {doc["policy_id"]: doc for doc in load_summary_documents()}


@lru_cache(maxsize=1)
def _origin_policy_names():
    """자격 Bundle에 없는 설명 전용 정책명까지 포함한 전체 원문 이름."""
    return tuple(
        doc.get("policy_name", "").strip()
        for doc in load_origin_documents()
        if doc.get("policy_name")
    )


POLICY_QUERY_NOISE = {
    "남양주", "남양주시", "청년", "정책", "지원", "지원사업", "사업", "관련",
    "정보", "혜택", "내용", "설명", "설명해줘", "알려줘", "알려", "뭐야", "대해",
    "받을", "받는", "있는", "가능한", "가능", "해줘", "좀", "어떤", "어느",
    "도움", "도와줘", "원해", "싶어", "찾아줘", "찾아", "이용", "신청",
    "수", "것", "거", "나", "내가", "저", "저도",
}


def _policy_query_tokens(query):
    """설명 요청에서 정책 선택에 의미가 있는 핵심 토큰만 남긴다."""
    raw_tokens = re.findall(r"[0-9a-z가-힣]+", (query or "").lower())
    cleaned = []
    particles = ("에게", "에서", "으로", "로", "을", "를", "이", "가", "은", "는", "과", "와", "도", "의", "에")
    suffixes = ("지원정책", "지원사업", "정책", "사업", "지원")
    for raw in raw_tokens:
        token = raw
        for particle in particles:
            if token.endswith(particle) and len(token) - len(particle) >= 2:
                token = token[:-len(particle)]
                break
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                token = token[:-len(suffix)]
                break
        if len(token) < 2 or token.isdigit() or token in POLICY_QUERY_NOISE:
            continue
        if token not in cleaned:
            cleaned.append(token)
    return cleaned


def _policy_query_evidence_score(query, candidate):
    """공식 요약 필드에 질의 핵심어가 직접 존재하는 정도를 계산한다."""
    tokens = _policy_query_tokens(query)
    if not tokens:
        return 0

    summary = _summary_by_policy_id().get(candidate.get("policy_id"), {})
    title_text = " ".join(str(summary.get(key, "")) for key in (
        "policy_name", "sub_category", "tags"
    )) or str(candidate.get("policy_name", ""))
    detail_text = " ".join(str(summary.get(key, "")) for key in (
        "summary", "main_target", "benefit"
    ))
    # 설명 전용 원문처럼 Summary가 없는 문서만 원문을 보조 근거로 사용한다.
    if not summary:
        detail_text = str(candidate.get("content", ""))

    title_compact = re.sub(r"\s+", "", title_text.lower())
    detail_compact = re.sub(r"\s+", "", detail_text.lower())
    score = 0
    matched_count = 0
    for token in tokens:
        compact = re.sub(r"\s+", "", token)
        if compact in title_compact:
            score += 4
            matched_count += 1
        elif compact in detail_compact:
            score += 3 if len(compact) >= 3 else 2
            matched_count += 1
    # 핵심어가 여러 개인데 절반만 맞는 경우(예: '농업인 노트북')에는
    # 분야 단어 하나만으로 전혀 다른 혜택을 안내하지 않는다.
    if matched_count / len(tokens) < 0.6:
        return 0
    return score


def _no_direct_policy_match_response(query):
    core = " ".join(_policy_query_tokens(query)) or (query or "요청 내용").strip()
    return (
        f"요청하신 핵심어 '{core}'에 직접 일치하는 정책은 현재 보유한 남양주시 공식 자료에서 찾지 못했어요.\n\n"
        "비슷하다는 이유만으로 다른 정책을 대신 안내하지 않겠습니다. "
        "정책명이나 핵심 서비스(예: 월세, 면접 정장, 창업 교육)를 다시 적어주세요."
    )


def _clean_source_line(line):
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
    line = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+"[^"]*")?\)', r"\1 (\2)", line)
    line = re.sub(r"^[\s#*+\-①-⑳]+", "", line).strip()
    line = re.sub(r"^\\+\s*", "", line)
    line = re.sub(r"^[1-9](?=\(?[가-힣])", "", line)
    line = re.sub(r"\*+", "", line)
    return re.sub(r"\s+", " ", line).strip(" |")


def _source_detail_lines(content, keywords, limit):
    found = []
    for raw in (content or "").splitlines():
        cleaned = _clean_source_line(raw)
        if not cleaned or len(cleaned) > 220:
            continue
        if any(keyword in cleaned for keyword in keywords) and cleaned not in found:
            found.append(cleaned)
        if len(found) >= limit:
            break
    return found


def format_grounded_explanation(selected):
    """공식 원문과 구조화 요약만으로 일관된 정책 설명을 만든다."""
    summary = _summary_by_policy_id().get(selected["policy_id"], {})
    content = selected.get("content", "")
    target = summary.get("main_target")
    benefit = summary.get("benefit") or summary.get("summary")
    period = summary.get("application_period") or summary.get("business_period")

    if not target:
        candidates = _source_detail_lines(content, ["지원대상", "신청대상", "교육대상", "이용대상", "대상"], 2)
        target = candidates[0] if candidates else "공식 원문에서 대상 조건을 확인해주세요."
    if not benefit:
        candidates = _source_detail_lines(content, ["지원내용", "지급내용", "교육내용", "이용방법", "운영"], 2)
        benefit = candidates[0] if candidates else "공식 원문에 안내된 사업 내용과 이용 방법을 확인해주세요."
    if not period:
        candidates = _source_detail_lines(content, ["신청기간", "접수기간", "사업기간", "교육기간"], 1)
        period = candidates[0] if candidates else "공식 공고 확인 필요"

    methods = _source_detail_lines(content, ["온라인", "방문 신청", "방문하여", "이메일", "읍면동", "행정복지센터"], 2)
    documents = _source_detail_lines(content, ["신청서", "주민등록", "증명서", "통장사본", "계약서", "제출서류"], 3)
    methods = methods or ["정확한 접수 방법은 아래 공식 출처에서 확인해주세요."]
    documents = documents or ["정책별 제출서류는 아래 공식 출처에서 확인해주세요."]

    # 이모지는 긴 설명의 섹션을 구분하는 제목에만 한 번씩 사용한다.
    # 일반 문장과 목록에는 붙이지 않아 화면이 산만해지지 않게 한다.
    lines = [
        f"📋 {selected['policy_name']}", "", "🎯 지원대상:", f"- {target}", "",
        "💰 지원내용:", f"- {benefit}", "", f"📅 신청기간: {period}", "", "📝 신청방법:",
    ]
    lines.extend(f"- {line}" for line in methods)
    lines.extend(["", "📎 제출서류:"])
    lines.extend(f"- {line}" for line in documents)
    source = (selected.get("source") or "").strip()
    if source.startswith(("http://", "https://")):
        source_text = f"🔗 공식 출처: [정책 페이지 바로가기]({source})"
    else:
        source_text = "🔗 공식 출처: 최신 공고에서 확인해주세요."
    lines.extend(["", source_text, "", "더 궁금한 점이 있으면 물어봐주세요!"])
    return "\n".join(lines)


# === RECOMMEND ===
def run_recommend(state, bundles):
    """RECOMMEND: 규칙 후보 → GPT 의미 적합성 판정/순위 → 검증/복구."""
    interest = state.get("interest_query", "")
    profile = state["profile"]
    
    # 관심분야 정규화 검증 — 허용값이 없으면 CLARIFY_PREFERENCE
    normalized = normalize_interests(interest) if interest else ""
    if not normalized:
        state["active_clarify"] = "CLARIFY_PREFERENCE"
        state["pending_tasks"] = ["RECOMMEND"]
        return "어떤 분야의 정책에 관심이 있으세요?\n아래에서 선택해주세요."
    interest = normalized
    state["interest_query"] = normalized
    
    # "조건 없이" 탐색 모드 여부
    was_skip_mode = state.get("_explore_mode", False)
    state["_explore_mode"] = False
    
    # "조건 없이"가 아닌 경우에만 Profile 체크
    skip_profile = state.get("_skip_profile_check", False)
    if not skip_profile:
        if get_profile_status(profile) == "INCOMPLETE":
            missing = get_missing_profile_fields(profile)
            if len(missing) > 4:
                return generate_clarify_question(state, "CLARIFY_PROFILE")
    
    # 사용 후 플래그 초기화
    state["_skip_profile_check"] = False
    
    # 1) 원본 Bundle과 구조화 규칙으로 후보/자격 상태를 먼저 고정한다.
    # 조건 없이 탐색은 세션에 예전 Profile이 남아 있어도 자격으로 제외하지 않는다.
    # 예: 만 34세 Profile 뒤에 기본소득을 조건 없이 보면 만 24세 조건 때문에
    # 사라지던 문제를 막는다. 이 모드는 관심 분야 탐색이지 개인 판정이 아니다.
    ranking_profile = {key: None for key in profile} if was_skip_mode else profile
    ranking_answers = {} if was_skip_mode else state.get("policy_answers", {})
    results = build_grounded_recommendations(
        bundles, ranking_profile, ranking_answers, interest
    )
    matched_results = results if interest == "전체" else [r for r in results if r["relevance"] >= 0.35]

    # 명확한 자격 FAIL은 추천 후보에서 제외한다. 그 외에는 코드 키워드가
    # 놓친 정책까지 GPT가 의미적으로 판단할 수 있도록 전체 정책을 전달한다.
    # GPT 장애/형식 오류 때만 결정론적 키워드 후보로 안전하게 복구한다.
    hard_fail_results = [r for r in matched_results if r.get("eligibility_status") == "FAIL"]
    ai_candidates = [r for r in results if r.get("eligibility_status") != "FAIL"]
    fallback_candidates = [r for r in matched_results if r.get("eligibility_status") != "FAIL"]
    semantic_query = state.pop("_recommend_query", None) or interest
    ai_results = rerank_recommendations_with_ai(
        ai_candidates, bundles, ranking_profile, semantic_query
    )
    # 사용자가 정확한 공식 정책명을 직접 적었다면 그 정책은 의미상 명시된
    # 대상이다. 모델이 관련 없음으로 잘못 누락해도 공식 후보를 결과에 고정한다.
    query_compact = re.sub(r"\s+", "", semantic_query.lower())
    pinned = [
        item for item in ai_candidates
        if re.sub(r"\s+", "", item["policy_name"].lower()) in query_compact
    ]
    if ai_results is not None:
        # GPT가 명시 정책을 관련 후보에 넣어도 낮은 순위를 주면
        # 화면 상위 N개에서 잘릴 수 있다. 기존 항목도 제거한 뒤
        # 공식 Bundle 값으로 다시 만들어 최상단에 고정한다.
        pinned_ids = {item["policy_id"] for item in pinned}
        ai_results = [item for item in ai_results if item["policy_id"] not in pinned_ids]
        for item in reversed(pinned):
            fixed = dict(item)
            fixed["relevance"] = 1.0
            fixed["_ai_selected"] = True
            fixed["_ai_rank"] = 0
            fixed["_ai_semantic_relevant"] = True
            ai_results.insert(0, fixed)
        relevant_results = ai_results
        state["_last_recommendation_mode"] = "AI_HYBRID"
    else:
        pinned_ids = {item["policy_id"] for item in pinned}
        relevant_results = [
            item for item in fallback_candidates if item["policy_id"] not in pinned_ids
        ]
        for item in reversed(pinned):
            fixed = dict(item)
            fixed["relevance"] = 1.0
            fixed["_ai_selected"] = True
            fixed["_ai_rank"] = 0
            relevant_results.insert(0, fixed)
        state["_last_recommendation_mode"] = "RULE_FALLBACK"
    
    # Action의 제외 조건은 GPT 결과에도 다시 적용해 주제 전환이 뒤집히지 않게 한다.
    excluded_ids = set(state.get("_exclude_policy_ids") or [])
    excluded_topics = set(state.get("_exclude_topics") or [])
    bundle_by_id = {bundle["policy_id"]: bundle for bundle in bundles}

    def is_excluded(item):
        if item["policy_id"] in excluded_ids:
            return True
        if not excluded_topics:
            return False
        bundle = bundle_by_id.get(item["policy_id"], {})
        _, matched = _interest_match(bundle, list(excluded_topics))
        return bool(set(matched) & excluded_topics)

    relevant_results = [item for item in relevant_results if not is_excluded(item)]
    state["current_topic"] = interest
    state["last_result_policy_ids"] = [
        item["policy_id"] for item in relevant_results[:8]
    ]
    if len(relevant_results) == 1:
        state["current_policy_id"] = relevant_results[0]["policy_id"]
    else:
        state["current_policy_id"] = None

    # PASS/UNKNOWN/FAIL 분리
    pass_results = [r for r in relevant_results if r.get("eligibility_status") == "PASS"]
    unknown_results = [r for r in relevant_results if r.get("eligibility_status") == "UNKNOWN"]
    
    # GPT가 검증된 순위를 돌려준 정책을 먼저, 나머지는 키워드 관련성순으로 정렬한다.
    pass_results.sort(key=_recommendation_sort_key, reverse=True)
    unknown_results.sort(key=_recommendation_sort_key, reverse=True)
    
    # 제한 적용 (PASS 최대 5, UNKNOWN 최대 3)
    pass_display = pass_results[:5]
    unknown_display = unknown_results[:3]
    
    # 응답 텍스트 생성
    if was_skip_mode:
        response = format_explore_response(relevant_results, interest)
    else:
        response = format_recommend_response(
            pass_display, unknown_display, len(pass_results), len(unknown_results), interest,
            matched_total=len(matched_results), excluded_results=hard_fail_results,
        )
    if relevant_results:
        response += "\n\n[ACTION_BTN:SHOW_ALTERNATIVES:다른 정책 보기]"
    return response


INTEREST_KEYWORDS = {
    "취업": ["일자리", "취업", "면접", "응시료", "인턴", "행정체험", "구직"],
    "주거": ["주거", "월세", "임차", "주택", "보증금"],
    "창업": ["창업", "사업화", "창업코칭", "창업멘토링"],
    # '프로그램'처럼 모든 정책에 흔한 단어는 분야 근거로 사용하지 않는다.
    "교육": ["교육", "학자금", "역량강화", "연수", "강의", "응시료"],
    # 일반 '지원금'은 입영·취업·농업을 모두 섞으므로 구체적인 복지 표현만 사용한다.
    "복지": ["복지", "소득", "저축", "급여", "정신건강", "치료비", "입영지원금"],
    # 기본소득은 일반적인 '지원금/급여'까지 잡으면 취업수당·학자금이 섞이므로 고유 표현만 사용한다.
    "기본소득": ["청년기본소득", "기본소득", "분기별 25만원", "최대 100만원 지역화폐"],
    "참여·문화": ["참여", "문화", "예술", "축제", "꽃간", "봉사", "시정참여"],
    "농업": ["농업", "농가", "영농", "말산업"],
}


CATEGORY_INTERESTS = {
    "청년일자리": {"취업"},
    "청년복지": {"복지"},
    "청년교육": {"교육"},
    "참여·문화": {"참여·문화"},
}


def _positive_interest_text(bundle, interest):
    """관심 분야를 실제로 설명하는 공식 필드만 모으고 부정 표현을 제거한다."""
    # 지원대상·본문에 우연히 등장하는 단어(미창업, 대관 임차료, 교육 제공 등)는
    # 분야 분류 근거로 삼지 않는다. 공식 이름·세부분류·태그만 사용한다.
    fields = ("policy_name", "sub_category", "tags", "recommendation_interests")
    text = " ".join(str(bundle.get(key, "")) for key in fields).lower()
    if interest == "창업":
        text = re.sub(
            r"미\s*창업|비\s*창업|취\s*[·ㆍ]?\s*창업\s*외|창업\s*외|창업하지\s*않\w*",
            " ",
            text,
        )
    return text


def _requires_namyangju_registration(residency_condition):
    """공통 Profile의 '남양주시 주민등록 아니오'만으로 확정 FAIL 가능한지."""
    text = re.sub(r"\s+", "", str(residency_condition or ""))
    if not text or text == "해당없음" or "경기도" in text:
        return False
    # 거주 외에 생활·활동이라는 대체 조건이 있으면 주민등록 '아니오'만으로
    # 탈락시킬 수 없고 정책별 추가 질문으로 확인해야 한다.
    if "또는" in text or "거주하거나" in text or "생활" in text or "활동" in text:
        return False
    return "남양주" in text


def _profile_fail_reasons_for_bundle(bundle, profile):
    basic = bundle.get("basic_condition", {})
    failures = []
    age = profile.get("age")
    if age is not None and basic.get("age") not in (None, "", "해당없음"):
        min_age, max_age = _parse_age_bounds(basic["age"])
        if (min_age is not None and age < min_age) or (max_age is not None and age > max_age):
            failures.append(f"나이 조건({basic['age']})")
    if _requires_namyangju_registration(basic.get("residency")) and profile.get("residency") == "아니오":
        failures.append("거주지 조건")
    if basic.get("employment") == "미취업" and profile.get("employment") == "취업":
        failures.append("미취업 조건")
    if (
        basic.get("employment", "").startswith("재직자")
        and profile.get("employment") is not None
        and profile.get("employment") != "취업"
    ):
        failures.append("재직 조건")
    if (
        basic.get("student") == "대학생"
        and profile.get("student") is not None
        and profile.get("student") != "대학생"
    ):
        failures.append("대학생 조건")
    if (
        basic.get("housing") == "무주택"
        and profile.get("housing") is not None
        and profile.get("housing") != "무주택"
    ):
        failures.append("무주택 조건")
    if (
        basic.get("marriage") == "미혼"
        and profile.get("marriage") is not None
        and profile.get("marriage") != "미혼"
    ):
        failures.append("미혼 조건")
    if basic.get("startup") == "예비창업자,기창업자" and profile.get("startup") == "창업하지 않음":
        failures.append("예비·기창업자 조건")
    if basic.get("startup") == "미창업" and profile.get("startup") in ("창업 중", "창업 준비 중"):
        failures.append("미창업 조건")
    return failures


def _interest_match(bundle, interests):
    if "전체" in interests:
        return 0.5, ["전체"]
    matched = []
    hit_count = 0
    category_matches = CATEGORY_INTERESTS.get(bundle.get("category", ""), set())
    for interest in interests:
        text = _positive_interest_text(bundle, interest)
        keywords = INTEREST_KEYWORDS.get(interest, [interest])
        hits = sum(1 for keyword in keywords if keyword.lower() in text)
        category_hit = interest in category_matches
        if category_hit or hits:
            matched.append(interest)
            hit_count += hits + (2 if category_hit else 0)
    if not matched:
        return 0.05, []
    return min(1.0, 0.55 + 0.08 * hit_count), matched


def build_grounded_recommendations(bundles, profile, policy_answers, interest):
    interests = [i.strip() for i in interest.split(",") if i.strip()] or ["전체"]
    results = []
    for bundle in bundles:
        failures = _profile_fail_reasons_for_bundle(bundle, profile)
        answers = policy_answers.get(bundle["policy_id"], {})
        questions = bundle.get("additional_questions", [])
        if bundle.get("eligibility_mode") == "INFO_ONLY":
            status = "UNKNOWN"
            missing = [bundle.get("info_only_reason") or "개별 신청 자격을 판정하는 정책이 아닌 정보·시설 안내입니다."]
        elif failures:
            status = "FAIL"
            missing = []
        elif any(q["question_id"] not in answers for q in questions):
            status = "UNKNOWN"
            missing = [q["question"] for q in questions if q["question_id"] not in answers]
        else:
            evaluated = evaluate_eligibility_answers(bundle, answers)
            status = evaluated["eligibility_status"]
            missing = evaluated["missing_conditions"]

        relevance, matched_interests = _interest_match(bundle, interests)
        prefix = ", ".join(matched_interests) + " 분야: " if matched_interests and matched_interests != ["전체"] else ""
        target = str(bundle.get("main_target") or "청년 대상")
        benefit = str(bundle.get("benefit") or bundle.get("summary") or "정책 지원")
        reason = f"{prefix}{target}에게 {benefit}"
        results.append({
            "policy_id": bundle["policy_id"],
            "policy_name": bundle["policy_name"],
            "eligibility_status": status,
            "relevance": relevance,
            "recommendation_reason": reason,
            "matched_interests": matched_interests,
            "matched_conditions": [],
            "failed_conditions": failures,
            "missing_conditions": missing,
            "caution_condition": bundle.get("caution_condition", []),
            "application_period": bundle.get("application_period", ""),
            "source": bundle.get("source", ""),
            "eligibility_mode": bundle.get("eligibility_mode", "FULL"),
        })
    return results


def _compact_recommendation_candidate(bundle, result):
    """GPT에 보내는 후보 필드를 최소화하고 공식 Bundle 값만 포함한다."""
    return {
        "policy_id": result["policy_id"],
        "policy_name": result["policy_name"],
        "category": bundle.get("category", ""),
        "sub_category": bundle.get("sub_category", ""),
        "summary": bundle.get("summary", ""),
        "main_target": bundle.get("main_target", ""),
        "benefit": bundle.get("benefit", ""),
        "baseline_status": result.get("eligibility_status", "UNKNOWN"),
        "keyword_relevance": result.get("relevance", 0),
        "missing_conditions": result.get("missing_conditions", [])[:5],
    }


def apply_ai_recommendation_judgments(candidates, parsed_payload):
    """GPT의 후보별 의미 판정을 검증하고 관련 후보만 보존한다."""
    if not isinstance(parsed_payload, dict):
        return None
    judgments = parsed_payload.get("judgments")
    if not isinstance(judgments, list):
        return None

    by_id = {item["policy_id"]: item for item in candidates}
    ranked = []
    seen = set()
    for item in judgments:
        if not isinstance(item, dict):
            continue
        policy_id = item.get("policy_id")
        if policy_id not in by_id or policy_id in seen:
            continue
        is_relevant = item.get("is_relevant")
        score = item.get("score")
        reason = str(item.get("reason") or "").strip()
        if isinstance(is_relevant, str) and is_relevant.lower() in {"true", "false"}:
            is_relevant = is_relevant.lower() == "true"
        if not isinstance(is_relevant, bool):
            continue
        if isinstance(score, str):
            try:
                score = float(score)
            except ValueError:
                score = None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        selected_name = str(by_id[policy_id].get("policy_name") or "")
        if not selected_name:
            continue
        # reason은 내부 진단용일 뿐 화면에 노출하지 않는다. 모델이 정책명을
        # 줄여 쓰거나 이유를 비워도 ID·관련성·점수가 유효하면 판정은 사용하고,
        # 사용자가 보는 이유는 항상 해당 policy_id의 공식 Bundle에서 만든다.
        if not reason:
            reason = f"{selected_name} 의미 관련성 판정"
        seen.add(policy_id)
        if not is_relevant:
            continue
        clean = dict(by_id[policy_id])
        clean["relevance"] = max(0.0, min(1.0, float(score) / 100.0))
        # GPT 원문은 내부 검증용으로만 보존한다. 사용자가 보는 추천 이유는
        # 이미 해당 policy_id의 공식 대상·혜택으로 만든 recommendation_reason을 사용한다.
        clean["_ai_reason"] = reason[:400]
        clean["_ai_selected"] = True
        clean["_ai_semantic_relevant"] = True
        ranked.append(clean)

    # 일부 후보가 빠진 응답은 누락을 '관련 없음'으로 추측하지 않고 폐기한다.
    required_seen = max(1, (len(by_id) * 4 + 4) // 5)
    if len(seen) < required_seen:
        return None

    ranked.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    for rank, item in enumerate(ranked, 1):
        item["_ai_rank"] = rank
    return ranked


def judge_recommendations_with_ai(candidates, bundles, profile, interest):
    """GPT가 모든 후보의 의미 적합성을 판정한다. 불완전 응답은 규칙 복구."""
    if not candidates:
        return None
    bundle_by_id = {bundle["policy_id"]: bundle for bundle in bundles}
    compact = [
        _compact_recommendation_candidate(bundle_by_id[item["policy_id"]], item)
        for item in candidates
        if item["policy_id"] in bundle_by_id
    ]
    prompt = PROMPT_C_RECOMMEND.format(
        interest_query=interest,
        profile=json.dumps(profile, ensure_ascii=False),
        bundles_text=json.dumps(compact, ensure_ascii=False),
    )
    raw = call_openai(prompt, json_mode=True)
    if not raw or raw.startswith("[ERROR]"):
        return None
    return apply_ai_recommendation_judgments(candidates, parse_json_response(raw))


def rerank_recommendations_with_ai(candidates, bundles, profile, interest):
    """이전 공개 함수 이름 호환용. 실제 동작은 의미 적합성 판정이다."""
    return judge_recommendations_with_ai(candidates, bundles, profile, interest)


def _recommendation_sort_key(item):
    """GPT 선택 여부와 순위를 우선하고 키워드 관련성을 보조 정렬로 사용한다."""
    if item.get("_ai_selected"):
        return 2, -int(item.get("_ai_rank", 999)), item.get("relevance", 0)
    return 1, item.get("relevance", 0), 0


def _official_policy_link(source, indent=""):
    """유효한 공식 출처만 채팅용 Markdown 링크로 만든다."""
    source = str(source or "").strip()
    if not source.startswith(("http://", "https://")):
        return ""
    return f"{indent}[공식 정책 페이지]({source})"


def format_explore_response(results, interest=""):
    """조건 없이 탐색 모드 — 개인 자격 판정처럼 표현하지 않음"""
    lines = []
    interests = [item.strip() for item in interest.split(",") if item.strip()] or ["전체"]
    multi_interest = len(interests) > 1

    if multi_interest:
        lines.append("📌 선택한 관심 분야별 정책을 살펴볼게요.\n")
    elif interest and interest != "전체":
        lines.append(f"📌 {interest} 분야 정책을 살펴볼게요.\n")
    else:
        lines.append("📌 남양주시 청년 정책을 살펴볼게요.\n")
    
    lines.append("프로필을 입력하지 않으셨으니 개인 자격 판정 없이 관련 정책 개요만 안내해드려요.\n")
    
    if multi_interest:
        shown_ids = set()
        for field in interests:
            field_results = [
                item for item in results
                if field in item.get("matched_interests", [])
                and item["policy_id"] not in shown_ids
            ]
            field_results.sort(key=_recommendation_sort_key, reverse=True)
            field_results = field_results[:3]
            lines.append(f"\n📍 [{field}] 분야\n")
            if not field_results:
                lines.append(
                    f"현재 보유한 남양주시 정책 자료에서 '{field}' 분야로 연결된 정책을 찾지 못했어요. "
                    "조건 없이 탐색에서는 나이·취업 상태 때문에 정책을 제외하지 않으므로, "
                    "분야 분류 데이터에 후보가 없는 경우예요.\n"
                )
                continue
            for i, item in enumerate(field_results, 1):
                shown_ids.add(item["policy_id"])
                lines.append(f"{i}. {item['policy_name']}")
                if item.get("recommendation_reason"):
                    lines.append(f"   {item['recommendation_reason']}")
                if item.get("application_period"):
                    lines.append(f"   신청기간: {item['application_period']}")
                link = _official_policy_link(item.get("source"), "   ")
                if link:
                    lines.append(link)
                if item.get("eligibility_mode") != "INFO_ONLY":
                    lines.append(f"   [ELIG_BTN:{item['policy_id']}:{item['policy_name']}]")
                lines.append("")
    else:
        # 단일 분야는 관련성 높은 순으로 최대 6개를 표시한다.
        sorted_results = sorted(results, key=_recommendation_sort_key, reverse=True)[:6]
        for i, item in enumerate(sorted_results, 1):
            lines.append(f"{i}. {item['policy_name']}")
            if item.get("recommendation_reason"):
                lines.append(f"   {item['recommendation_reason']}")
            if item.get("application_period"):
                lines.append(f"   신청기간: {item['application_period']}")
            link = _official_policy_link(item.get("source"), "   ")
            if link:
                lines.append(link)
            if item.get("eligibility_mode") != "INFO_ONLY":
                lines.append(f"   [ELIG_BTN:{item['policy_id']}:{item['policy_name']}]")
            lines.append("")
        if not sorted_results:
            lines.append(
                "현재 보유한 남양주시 정책 자료에서 선택 분야로 연결된 정책을 찾지 못했어요. "
                "조건 없이 탐색에서는 Profile 때문에 제외하지 않으므로, 다른 분야를 선택하거나 "
                "정책 키워드를 조금 더 구체적으로 입력해주세요."
            )
    
    lines.append("\n프로필(👤)을 입력하면 내 조건에 맞는 맞춤 추천을 받을 수 있어요!")
    return "\n".join(lines)


def format_recommend_response(
    pass_list, unknown_list, total_pass, total_unknown, interest="",
    matched_total=0, excluded_results=None,
):
    """추천 결과를 자연어로 포맷 — 복수 관심분야면 분야별 구분"""
    lines = []
    excluded_results = excluded_results or []
    
    # 복수 관심분야 파싱
    interests = [i.strip() for i in interest.split(",")] if interest else ["전체"]
    
    if len(interests) > 1:
        # 분야별로 그룹핑 (중복 제거: 이미 표시한 policy_id는 스킵)
        shown_ids = set()
        for field in interests:
            field_pass = [
                p for p in pass_list
                if (field in p.get("matched_interests", []) or field in p.get("recommendation_reason", ""))
                and p["policy_id"] not in shown_ids
            ]
            field_unknown = [
                u for u in unknown_list
                if (field in u.get("matched_interests", []) or field in u.get("recommendation_reason", ""))
                and u["policy_id"] not in shown_ids
            ]
            
            lines.append(f"\n📌 [{field}] 분야\n")
            
            if field_pass:
                lines.append(f"  ✅ 신청 가능성 높은 정책 {len(field_pass)}개")
                for i, p in enumerate(field_pass, 1):
                    shown_ids.add(p["policy_id"])
                    lines.append(f"  {i}. {p['policy_name']}")
                    lines.append(f"     추천이유: {p.get('recommendation_reason', '')}")
                    if p.get('application_period'):
                        lines.append(f"     신청기간: {p['application_period']}")
                    link = _official_policy_link(p.get("source"), "     ")
                    if link:
                        lines.append(link)
                lines.append("")
            
            if field_unknown:
                lines.append(f"  🔍 추가 확인 필요 {len(field_unknown)}개")
                for i, u in enumerate(field_unknown, 1):
                    shown_ids.add(u["policy_id"])
                    lines.append(f"  {i}. {u['policy_name']}")
                    lines.append(f"     추천이유: {u.get('recommendation_reason', '')}")
                    link = _official_policy_link(u.get("source"), "     ")
                    if link:
                        lines.append(link)
                    if u.get("eligibility_mode") != "INFO_ONLY":
                        lines.append(f"     [ELIG_BTN:{u['policy_id']}:{u['policy_name']}]")
                lines.append("")
            
            if not field_pass and not field_unknown:
                field_excluded = [
                    item for item in excluded_results
                    if field in item.get("matched_interests", [])
                ]
                if field_excluded:
                    reasons = list(dict.fromkeys(
                        reason
                        for item in field_excluded
                        for reason in item.get("failed_conditions", [])
                    ))
                    reason_text = ", ".join(reasons[:3]) or "입력한 Profile의 명확한 불충족 조건"
                    lines.append(
                        f"  관련 정책 {len(field_excluded)}개는 찾았지만 맞춤 조건에서 제외됐어요: {reason_text}.\n"
                        "  자격 판정 없이 보려면 '조건 없이 찾아보기'를 이용해주세요.\n"
                    )
                else:
                    lines.append(
                        "  현재 보유한 정책 자료에서 이 분야로 연결된 새 후보를 찾지 못했어요. "
                        "앞 분야에 같은 정책이 이미 표시된 경우도 중복해서 보여주지 않아요.\n"
                    )
    else:
        # 단일 분야
        if interest and interest != "전체":
            lines.append(f"📌 관심 분야: {interest}\n")
        
        if pass_list:
            lines.append(f"✅ 신청 가능성이 높은 정책 {total_pass}개\n")
            for i, p in enumerate(pass_list, 1):
                lines.append(f"{i}. {p['policy_name']}")
                lines.append(f"   추천이유: {p.get('recommendation_reason', '')}")
                if p.get('application_period'):
                    lines.append(f"   신청기간: {p['application_period']}")
                link = _official_policy_link(p.get("source"), "   ")
                if link:
                    lines.append(link)
                lines.append("")
        
        if unknown_list:
            lines.append(f"🔍 추가 확인이 필요한 정책 {total_unknown}개 중 관련성 높은 {len(unknown_list)}개\n")
            for i, u in enumerate(unknown_list, 1):
                lines.append(f"{i}. {u['policy_name']}")
                lines.append(f"   추천이유: {u.get('recommendation_reason', '')}")
                if u.get('missing_conditions'):
                    lines.append(f"   확인 필요: {', '.join(u['missing_conditions'])}")
                link = _official_policy_link(u.get("source"), "   ")
                if link:
                    lines.append(link)
                if u.get("eligibility_mode") != "INFO_ONLY":
                    lines.append(f"   [ELIG_BTN:{u['policy_id']}:{u['policy_name']}]")
                lines.append("")
    
    if not pass_list and not unknown_list:
        if excluded_results:
            reasons = list(dict.fromkeys(
                reason
                for item in excluded_results
                for reason in item.get("failed_conditions", [])
            ))
            reason_text = ", ".join(reasons[:4]) or "입력한 Profile의 명확한 불충족 조건"
            lines.append(
                f"관련 정책 {len(excluded_results)}개는 찾았지만 맞춤 조건에서 제외됐어요.\n"
                f"제외 이유: {reason_text}\n"
                "자격 판정 없이 정책 자체를 보려면 '조건 없이 찾아보기'를 이용해주세요."
            )
        elif matched_total == 0:
            lines.append(
                "현재 보유한 남양주시 정책 자료에서 선택한 관심 분야와 연결된 후보를 찾지 못했어요.\n"
                "다른 관심 분야를 선택하거나 '조건 없이 찾아보기'로 전체 개요를 확인해주세요."
            )
        else:
            lines.append(
                "입력 정보만으로 화면에 확정해 보여줄 후보를 만들지 못했어요.\n"
                "조건 없이 찾아보거나 Profile을 다시 확인해주세요."
            )
    
    lines.append("\n더 자세히 알고 싶은 정책이 있으면 정책명을 말씀해주세요!")
    
    return "\n".join(lines)


# === ELIGIBILITY ===
def run_eligibility(state, bundles):
    """ELIGIBILITY 파이프라인: 공식 질문 → 규칙 판정 → GPT 보조 검토."""
    # 대상 정책 확정: _policy_mention이 있으면 focus보다 우선
    policy_id = None
    policy_mention = state.get("_policy_mention")
    
    # 직전 정책을 명시적으로 가리키는 지시어 (focus 재사용 허용)
    FOCUS_REFERENCES = ["이거", "이 정책", "그거", "그 정책", "방금 정책", "위에 거", "나도 가능", "나도 되"]
    
    if policy_mention:
        mention_clean = policy_mention.strip()
        is_focus_ref = any(ref in mention_clean for ref in FOCUS_REFERENCES)
        state["_policy_mention"] = None
        
        if is_focus_ref:
            policy_id = state.get("selected_policy_id") or state.get("focus_policy_id")
        else:
            # 새 정책 언급 → UNIQUE / AMBIGUOUS / NOT_FOUND
            alias_policy_id = resolve_policy_alias(mention_clean)
            if alias_policy_id and any(b["policy_id"] == alias_policy_id for b in bundles):
                status = "UNIQUE"
                candidates = [b for b in bundles if b["policy_id"] == alias_policy_id]
            else:
                status, candidates = match_policy_candidates(bundles, mention_clean)
            if status == "NOT_FOUND":
                rewritten = state.get("_rewritten_query")
                if rewritten:
                    alias_policy_id = resolve_policy_alias(rewritten)
                    if alias_policy_id and any(b["policy_id"] == alias_policy_id for b in bundles):
                        status = "UNIQUE"
                        candidates = [b for b in bundles if b["policy_id"] == alias_policy_id]
                    else:
                        status, candidates = match_policy_candidates(bundles, rewritten)
            
            if status == "UNIQUE":
                policy_id = candidates[0]["policy_id"]
                state["focus_policy_id"] = policy_id
                state["selected_policy_id"] = policy_id
            elif status == "AMBIGUOUS":
                # focus fallback 금지 → 후보 제시
                state["active_clarify"] = "CLARIFY_POLICY"
                state["pending_tasks"] = ["ELIGIBILITY"]
                state["_policy_candidates"] = [
                    {"policy_id": c["policy_id"], "policy_name": c["policy_name"]}
                    for c in candidates[:8]
                ]
                return "어떤 정책의 자격을 확인할까요?\n아래에서 선택해주세요."
            else:
                state["active_clarify"] = "CLARIFY_POLICY"
                state["pending_tasks"] = ["ELIGIBILITY"]
                state["_policy_candidates"] = None
                return "어떤 정책의 자격을 확인하고 싶으신가요?\n아래에서 선택해주세요."
    
    if not policy_id:
        policy_id = state.get("selected_policy_id") or state.get("focus_policy_id")
    
    if not policy_id:
        state["active_clarify"] = "CLARIFY_POLICY"
        state["pending_tasks"] = ["ELIGIBILITY"]
        state["_policy_candidates"] = None
        return "어떤 정책의 자격을 확인하고 싶으신가요?\n아래에서 선택해주세요."
    
    # Bundle 로드
    bundle = next((b for b in bundles if b["policy_id"] == policy_id), None)
    if not bundle:
        return "해당 정책의 자격 규칙을 찾을 수 없습니다."
    
    state["selected_policy_id"] = policy_id
    state["current_policy_id"] = policy_id
    state["focus_policy_id"] = policy_id

    # 시설·종합정보 페이지는 개별 신청 자격을 임의 판정하지 않는다.
    if bundle.get("eligibility_mode") == "INFO_ONLY":
        state["_last_eligibility_mode"] = "INFO_ONLY"
        state["active_clarify"] = None
        state["pending_tasks"] = []
        reason = bundle.get("info_only_reason") or "개별 신청 자격을 판정하는 정책이 아닌 정보·시설 안내 페이지입니다."
        lines = [
            f"📋 {bundle['policy_name']} 자격 확인 안내\n",
            "🔍 이 항목은 개별 신청 자격을 PASS/FAIL로 판정할 수 없어요.\n",
            reason,
            "입주·대관·이용 조건은 해당 시점의 공식 모집공고나 예약 페이지에서 확인해주세요.",
        ]
        link = _official_policy_link(bundle.get("source"))
        if link:
            lines.extend(["", link])
        return "\n".join(lines)
    
    # 필요한 Profile 필드 확인
    needed_fields = get_policy_needed_fields(bundle)
    profile = state["profile"]
    missing_needed = [f for f in needed_fields if f in profile and profile[f] is None]
    
    # needed_fields가 모두 채워져 있으면 카드 생략 (전체 7개 COMPLETE 여부와 무관)
    if missing_needed and not state.get("_skip_profile_check"):
        state["_last_eligibility_mode"] = "COLLECTING_INPUT"
        state["_missing_fields"] = missing_needed[:3]
        state["active_clarify"] = "CLARIFY_PROFILE"
        state["pending_tasks"] = ["ELIGIBILITY"]
        # 실제 질문과 선택지는 바로 아래 Profile 카드가 담당한다. 말풍선에
        # 같은 항목을 다시 나열하지 않아 모바일에서 중복 질문이 생기지 않게 한다.
        message = f"{bundle['policy_name']} 자격 확인을 위해 몇 가지 여쭤볼게요."
        link = _official_policy_link(bundle.get("source"))
        return f"{message}\n\n{link}" if link else message
    state["_skip_profile_check"] = False
    state["_missing_fields"] = []
    
    # basic_condition 코드 레벨 비교 — 명확한 불충족 시 즉시 FAIL
    basic = bundle.get("basic_condition", {})
    fail_reasons = []
    
    # 거주지 체크
    if _requires_namyangju_registration(basic.get("residency")):
        if profile.get("residency") == "아니오":
            fail_reasons.append("거주지: 남양주시 거주자만 신청 가능합니다.")
    
    # 나이 체크 (단일 정확 나이, 범위, '미만' 표현 포함)
    if basic.get("age") and basic["age"] != "해당없음" and profile.get("age"):
        age = profile["age"]
        age_text = basic["age"]
        min_age, max_age = _parse_age_bounds(age_text)
        if min_age is not None and age < min_age:
            fail_reasons.append(f"나이: {age_text} 조건을 충족해야 합니다. (현재 만 {age}세)")
        if max_age is not None and age > max_age:
            fail_reasons.append(f"나이: {age_text} 조건을 충족해야 합니다. (현재 만 {age}세)")
    
    # 고용 상태 체크
    if basic.get("employment") and basic["employment"] != "해당없음":
        if profile.get("employment"):
            basic_emp = basic["employment"]
            user_emp = profile["employment"]
            if "미취업" in basic_emp and user_emp == "취업":
                fail_reasons.append("고용 상태: 미취업자만 신청 가능합니다.")
            if basic_emp.startswith("재직자") and user_emp != "취업":
                fail_reasons.append("고용 상태: 근로·사업소득이 있는 재직자만 신청 가능합니다.")

    # 구조화 Profile로 명확하게 비교할 수 있는 항목만 코드에서 확정한다.
    if basic.get("student") == "대학생" and profile.get("student") != "대학생":
        fail_reasons.append("학생 여부: 대학생만 신청 가능합니다.")
    if basic.get("housing") == "무주택" and profile.get("housing") != "무주택":
        fail_reasons.append("주택 보유: 무주택자만 신청 가능합니다.")
    if basic.get("marriage") == "미혼" and profile.get("marriage") != "미혼":
        fail_reasons.append("결혼 여부: 미혼 청년만 신청 가능합니다.")
    if basic.get("startup") == "미창업" and profile.get("startup") in ("창업 중", "창업 준비 중"):
        fail_reasons.append("창업 여부: 미창업 청년만 대상입니다.")
    if (
        basic.get("startup") == "예비창업자,기창업자"
        and profile.get("startup") == "창업하지 않음"
    ):
        fail_reasons.append("창업 여부: 예비창업자 또는 기창업자만 대상입니다.")
    
    if fail_reasons:
        state["_last_eligibility_mode"] = "RULE_ONLY_HARD_FAIL"
        result_text = f"📋 {bundle['policy_name']} 자격 확인 결과\n\n"
        result_text += "❌ 아쉽지만 조건이 맞지 않아요.\n\n"
        result_text += "불충족 조건:\n"
        for r in fail_reasons:
            result_text += f"  ✗ {r}\n"
        link = _official_policy_link(bundle.get("source"))
        if link:
            result_text += f"\n{link}\n"
        result_text += "\n[ACTION_BTN:RESET_PROFILE:프로필 다시 설정하기]\n[ACTION_BTN:RESET_CHAT:대화 초기화하기]"
        return result_text
    
    # additional_questions 중 아직 답변 안 된 것 확인
    existing_answers = state.get("policy_answers", {}).get(policy_id, {})
    # 남양주시 주민등록이 확인되면 더 넓은 경기도 거주나
    # 남양주시 거주·생활/활동 조건은 자동 충족시켜 중복 질문을 막는다.
    if profile.get("residency") == "예":
        auto_residency_questions = {
            "gyeonggi_resident", "local_resident_or_living", "local_resident_or_active",
        }
        for question in bundle.get("additional_questions", []):
            qid = question.get("question_id")
            if qid in auto_residency_questions and qid not in existing_answers:
                save_policy_answer(state, policy_id, qid, "예")
        existing_answers = state.get("policy_answers", {}).get(policy_id, {})
    additional_qs = bundle.get("additional_questions", [])
    unanswered = [q for q in additional_qs if q["question_id"] not in existing_answers]
    

    
    if unanswered:
        state["_last_eligibility_mode"] = "COLLECTING_INPUT"
        # 한번에 최대 3개 질문을 카드로 표시
        batch = unanswered[:3]
        q_start = len(additional_qs) - len(unanswered) + 1
        state["_active_additional_q"] = {
            "policy_id": policy_id,
            "questions": [
                {
                    "question_id": q["question_id"],
                    "question": q["question"],
                    "options": q.get("options", []),
                    "q_num": q_start + i,
                }
                for i, q in enumerate(batch)
            ],
            "total": len(additional_qs),
            "answered": len(existing_answers),
            "remaining": len(unanswered),
            "batch_start": q_start,
            "batch_end": q_start + len(batch) - 1,
        }
        state["active_clarify"] = "CLARIFY_ADDITIONAL"
        state["pending_tasks"] = ["ELIGIBILITY"]
        
        message = f"{bundle['policy_name']} 추가 자격 조건 확인이 필요해요. ({len(unanswered)}개 남음)"
        link = _official_policy_link(bundle.get("source"))
        return f"{message}\n\n{link}" if link else message
    
    # 모든 질문 완료 → 구조화 규칙을 먼저 판정한 뒤 GPT가 보조 검토한다.
    # 명확한 FAIL은 즉시 확정하고, PASS/UNKNOWN만 GPT에 전달한다.
    rule_result = evaluate_eligibility_answers(bundle, existing_answers)
    if rule_result.get("eligibility_status") == "FAIL":
        result = rule_result
        state["_last_eligibility_mode"] = "RULE_ONLY_HARD_FAIL"
    else:
        ai_result = review_eligibility_with_ai(bundle, profile, existing_answers, rule_result)
        if ai_result is None:
            result = rule_result
            state["_last_eligibility_mode"] = "RULE_FALLBACK"
        else:
            result = ai_result
            state["_last_eligibility_mode"] = "AI_HYBRID"
    
    # _active_additional_q 클리어
    state.pop("_active_additional_q", None)
    
    return format_eligibility_response(
        bundle["policy_name"],
        result,
        bundle.get("source", ""),
        policy_id=policy_id,
        has_additional=bool(additional_qs),
    )


NEGATIVE_ELIGIBILITY_QUESTION_IDS = {
    "government_employment_program", "participated_last_three_years",
    "participated_last_two_years", "relative_owned_rental", "public_rental_housing",
    "sublease_shared_room", "previous_24_months_support", "other_housing_support",
    "excluded_income_only", "restricted_savings_account", "similar_asset_program",
    "excluded_service_type", "discharged", "previous_enlistment_support",
}


def _parse_age_bounds(age_text):
    nums = [int(n) for n in re.findall(r"\d+", age_text or "")]
    if not nums:
        return None, None
    if len(nums) == 1:
        value = nums[0]
        if "이하" in age_text:
            return None, value
        if "이상" in age_text:
            return value, None
        return value, value
    min_age, max_age = nums[0], nums[1]
    if "미만" in age_text:
        max_age -= 1
    return min_age, max_age


def evaluate_eligibility_answers(bundle, answers):
    """추가 질문 답변을 정책 규칙의 방향대로 결정론적으로 판정한다."""
    matched, failed, missing = [], [], []
    questions = bundle.get("additional_questions", [])
    by_id = {q["question_id"]: q for q in questions}
    handled = set()

    # 청년기본소득 거주기간은 두 조건 중 하나만 충족하면 된다(OR 조건).
    residence_or = ["gyeonggi_three_years", "gyeonggi_ten_years"]
    if all(qid in by_id for qid in residence_or):
        values = [answers.get(qid) for qid in residence_or]
        handled.update(residence_or)
        if "예" in values:
            matched.append("경기도 거주기간 요건(3년 계속 또는 합산 10년)을 충족함")
        elif all(value == "아니오" for value in values):
            failed.append("경기도 3년 계속 거주 또는 합산 10년 거주 요건을 충족하지 않음")
        else:
            missing.append("경기도 거주기간 요건 확인 필요")

    for q in questions:
        qid = q["question_id"]
        if qid in handled:
            continue
        answer = answers.get(qid)
        label = q["question"].rstrip("?")
        options = q.get("options", [])

        if not answer or answer in ("잘 모르겠음", "모르겠음"):
            missing.append(label)
            continue

        yes_no_question = "예" in options and "아니오" in options
        if not yes_no_question:
            # 생년월일처럼 별도 공고 기준 대조가 필요한 자유입력은 확정하지 않는다.
            if qid == "birth_date":
                missing.append(f"분기별 지급대상 생년월일 대조 필요 ({answer})")
            else:
                matched.append(f"{label}: {answer}")
            continue

        expected = "아니오" if qid in NEGATIVE_ELIGIBILITY_QUESTION_IDS else "예"
        if answer == expected:
            matched.append(label)
        else:
            failed.append(label)

    # 원문에 구체 조건이 없어서 데이터 단계에서 확정할 수 없는 항목은,
    # 화면 질문에 모두 답해도 PASS로 과대 판정하지 않는다.
    if not failed:
        for condition in bundle.get("unverified_conditions", []):
            condition = str(condition or "").strip()
            if condition and condition not in missing:
                missing.append(condition)

    if failed:
        status = "FAIL"
        explanation = "입력한 답변 중 필수 조건과 맞지 않는 항목이 확인됐어요."
    elif missing:
        status = "UNKNOWN"
        explanation = "명확한 불충족은 없지만 공고 또는 증빙으로 더 확인해야 할 조건이 있어요."
    else:
        status = "PASS"
        explanation = "입력한 기본 정보와 추가 답변에서는 필수 조건을 모두 충족했어요. 실제 선정은 제출서류 심사와 최신 공고를 기준으로 합니다."

    return {
        "eligibility_status": status,
        "matched_conditions": matched,
        "failed_conditions": failed,
        "missing_conditions": missing,
        "next_questions": [],
        "caution_condition": bundle.get("caution_condition", []),
        "explanation": explanation,
    }


def _clean_ai_text_list(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:8]:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text[:240])
    return cleaned


def merge_eligibility_review(rule_result, parsed_payload):
    """규칙 결과와 검증된 GPT 검토를 보수적으로 병합한다. 잘못된 응답은 None."""
    if not isinstance(rule_result, dict) or not isinstance(parsed_payload, dict):
        return None
    rule_status = rule_result.get("eligibility_status")
    if rule_status == "FAIL":
        # 명확한 규칙 FAIL은 어떤 모델 결과로도 뒤집지 않는다.
        return dict(rule_result)

    ai_status = parsed_payload.get("ai_status")
    reason = str(parsed_payload.get("reason") or "").strip()
    if ai_status not in {"PASS", "FAIL", "UNKNOWN"} or not reason:
        return None

    merged = dict(rule_result)
    rule_matched = list(rule_result.get("matched_conditions", []))
    rule_missing = list(rule_result.get("missing_conditions", []))
    ai_matched = _clean_ai_text_list(parsed_payload.get("matched_conditions"))
    ai_failed = _clean_ai_text_list(parsed_payload.get("failed_conditions"))
    ai_missing = _clean_ai_text_list(parsed_payload.get("missing_conditions"))

    if rule_status == "PASS" and ai_status == "PASS":
        merged["eligibility_status"] = "PASS"
        merged["matched_conditions"] = list(dict.fromkeys(rule_matched + ai_matched))
        merged["explanation"] = (
            f"{rule_result.get('explanation', '')} "
            f"AI 보조 검토도 같은 방향입니다: {reason[:400]}"
        ).strip()
        return merged

    # 규칙이 UNKNOWN이거나 규칙과 GPT가 충돌하면 확정하지 않는다.
    merged["eligibility_status"] = "UNKNOWN"
    extra_checks = ai_missing + [f"AI 추가 확인: {item}" for item in ai_failed]
    merged["missing_conditions"] = list(dict.fromkeys(rule_missing + extra_checks))
    merged["failed_conditions"] = []
    merged["explanation"] = (
        f"{rule_result.get('explanation', '')} "
        f"AI 보조 검토에서도 확정하지 않고 추가 확인이 필요합니다: {reason[:400]}"
    ).strip()
    return merged


def review_eligibility_with_ai(bundle, profile, existing_answers, rule_result):
    """완료된 공식 질문 답변과 규칙 결과를 GPT에 한 번 전달한다."""
    policy_bundle = {
        "policy_id": bundle.get("policy_id"),
        "policy_name": bundle.get("policy_name"),
        "summary": bundle.get("summary", ""),
        "main_target": bundle.get("main_target", ""),
        "benefit": bundle.get("benefit", ""),
        "basic_condition": bundle.get("basic_condition", {}),
        "additional_questions": bundle.get("additional_questions", []),
        "caution_condition": bundle.get("caution_condition", []),
    }
    prompt = PROMPT_D_ELIGIBILITY.format(
        policy_bundle=json.dumps(policy_bundle, ensure_ascii=False),
        profile=json.dumps(profile, ensure_ascii=False),
        existing_answers=json.dumps(existing_answers, ensure_ascii=False),
        rule_result=json.dumps(rule_result, ensure_ascii=False),
    )
    raw = call_openai(prompt, json_mode=True)
    if not raw or raw.startswith("[ERROR]"):
        return None
    return merge_eligibility_review(rule_result, parse_json_response(raw))


def format_eligibility_response(
    policy_name, result, source="", policy_id=None, has_additional=False
):
    """자격 판정 결과를 텍스트로 포맷 (마크다운 없이 채팅체)"""
    status = result.get("eligibility_status", "UNKNOWN")
    lines = [f"📋 {policy_name} 자격 확인 결과\n"]
    
    if status == "PASS":
        lines.append("✅ 신청 가능성이 높아요!\n")
    elif status == "FAIL":
        lines.append("❌ 아쉽지만 조건이 맞지 않아요.\n")
    else:
        lines.append("🔍 추가 확인이 필요해요.\n")
    
    lines.append(f"{result.get('explanation', '')}\n")
    
    if result.get("matched_conditions"):
        lines.append("충족 조건:")
        for c in result["matched_conditions"]:
            lines.append(f"  ✓ {c}")
    
    if result.get("failed_conditions"):
        lines.append("\n불충족 조건:")
        for c in result["failed_conditions"]:
            lines.append(f"  ✗ {c}")
    
    if result.get("missing_conditions"):
        lines.append("\n미확인 조건:")
        for c in result["missing_conditions"]:
            lines.append(f"  ? {c}")
    
    # 추가 질문은 첫 번째만 표시 (한번에 1개씩)
    if result.get("next_questions") and status != "FAIL":
        q = result["next_questions"][0]
        lines.append(f"\n추가 질문:")
        lines.append(f"  {q.get('question', '')}")
        if q.get("options"):
            lines.append(f"  → {' / '.join(q['options'])}")
    
    if result.get("caution_condition"):
        lines.append("\n⚠️ 주의사항:")
        for c in result["caution_condition"]:
            lines.append(f"  {c}")

    link = _official_policy_link(source)
    if link:
        lines.append(f"\n{link}")

    if has_additional and policy_id:
        lines.append(
            f"\n[ACTION_BTN:EDIT_ADDITIONAL:{policy_id}:추가 답변 다시 입력하기]"
        )
    
    # FAIL 또는 UNKNOWN이면 액션 버튼 추가
    if status in ("FAIL", "UNKNOWN"):
        lines.append("\n[ACTION_BTN:RESET_PROFILE:프로필 다시 설정하기]")
        lines.append("[ACTION_BTN:RESET_CHAT:대화 초기화하기]")
    
    return "\n".join(lines)


def find_bundle_by_name(bundles, name_query):
    """이름으로 Bundle 검색 (부분 매칭) — 첫 번째 후보 반환 (하위 호환)"""
    status, candidates = match_policy_candidates(bundles, name_query)
    if status == "UNIQUE":
        return candidates[0]
    return None


ALLOWED_INTERESTS = ["취업", "주거", "창업", "교육", "복지", "참여·문화", "기본소득", "농업", "전체"]

INTEREST_ALIASES = {
    "취업": ["취업", "일자리", "구직", "취준", "인턴", "면접", "자소서", "자기소개서", "정장", "면접복장"],
    "주거": ["주거", "월세", "전세", "보증금", "임차", "집 구하기"],
    "창업": ["창업", "사업 시작", "사업화", "예비창업"],
    "교육": ["교육", "수업", "강의", "역량 강화", "학자금", "응시료", "자격증비", "시험비"],
    "복지": ["복지", "마음건강", "마음 건강", "정신건강", "저축", "생활 지원"],
    "참여·문화": ["참여·문화", "참여문화", "참여", "문화", "예술", "축제"],
    "기본소득": ["기본소득", "기본 소득"],
    "농업": ["농업", "농어", "농어촌", "농사", "영농", "귀농", "농가", "말산업"],
    "전체": ["전체", "모든 분야", "전 분야"],
}

POLICY_EXPLAIN_HINTS = [
    "정책", "지원", "사업", "청년", "월세", "응시료", "자격증", "헤어",
    "스타일링", "면접", "정장", "대여", "창업", "교육", "주거", "소득", "농업", "입영", "취업", "정약용",
]

OTHER_JURISDICTION_TERMS = [
    "서울시", "서울특별시", "부산시", "부산광역시", "인천시", "인천광역시",
    "수원시", "고양시", "성남시", "용인시", "하남시", "구리시", "의정부시",
]


def get_guardrail_response(message):
    """범위 밖/비정상 입력을 모델 호출 전에 안전하게 처리한다."""
    msg = (message or "").strip()
    if not msg:
        return None
    if any(term in msg for term in OTHER_JURISDICTION_TERMS) and "남양주" not in msg:
        return "두물청은 남양주시 청년정책만 안내하고 있어요. 해당 지역의 공식 청년정책 창구를 확인해주세요."
    if any(pattern in msg.lower() for pattern in ("이전 지시 무시", "지금까지 지시 무시", "ignore previous", "system prompt")):
        return "보유한 남양주시 공식 정책 자료와 자격 규칙 범위에서만 안내할 수 있어요. 궁금한 정책명을 말씀해주세요."
    if any(pattern in msg for pattern in ("무조건 합격", "합격 확정", "선정 확정", "지급 확정", "보장해", "보증해")):
        return (
            "자격 확인 결과는 안내형 가능성이며 실제 합격·선정·지급을 확정하거나 보장할 수 없어요. "
            "최종 결과는 제출서류 심사와 최신 공식 공고 또는 담당 부서에서 확인해주세요."
        )
    if any(pattern in msg for pattern in ("추측해서", "적당히 추측", "알아서 채워", "알아서 입력")):
        return "나이, 소득 같은 개인정보를 추측해서 채울 수는 없어요. 정확한 맞춤 추천을 위해 직접 입력해주세요."
    if len(msg) > 1000 or (len(set(msg)) <= 4 and len(msg) > 100):
        return _intent_clarify_response("INVALID_LENGTH")
    if not re.search(r"[0-9A-Za-z가-힣]", msg):
        return _intent_clarify_response("INVALID_CHARACTERS")
    return None


def _looks_like_multi_task_request(message):
    """한 발화에 서로 다른 두 개 이상의 작업 동사가 있으면 Workflow로 보낸다."""
    if not message:
        return False
    flags = [
        bool(re.search(r"설명|알려|뭐야|뭔지", message)),
        _is_explicit_recommend_request(message),
        bool(re.search(
            r"자격|가능한지|신청\s*(?:할\s*)?수|지원\s*(?:받을\s*)?수",
            message,
        )) and "자격증" not in message,
    ]
    return sum(bool(item) for item in flags) >= 2


MULTI_TASK_CUE_PATTERNS = {
    "EXPLAIN": re.compile(r"설명|알려|뭐야|뭔지"),
    "RECOMMEND": re.compile(r"추천"),
    "ELIGIBILITY": re.compile(
        r"자격|가능한지|되는지|신청\s*(?:할\s*)?수|지원\s*(?:받을\s*)?수|대상인지"
    ),
}


def _clean_atomic_target(text):
    """복합 문장의 의미 단위에서 인사·연결어만 제거하고 대상어는 보존한다."""
    value = (text or "").strip(" ,/·;!?\t\n")
    value = re.sub(r"^(?:안녕(?:하세요)?|저기|그럼|그러면)\s*", "", value)
    value = re.sub(r"^(?:나|나는|저|저는)\s+", "", value)
    value = re.sub(r"^(?:그리고|또|이어서|그다음(?:에)?|하고|해주고)\s*", "", value)
    value = re.sub(r"(?:해\s*주고|해주고|하고|한\s*뒤|다음에)\s*$", "", value)
    return value.strip(" ,/·;!?")


def infer_atomic_workflow_from_message(state, message, bundles):
    """명시 작업 동사의 순서와 각 동사 앞 대상을 보존하는 결정론적 보조 분석기.

    Prompt A를 대체하지 않는다. 복합 발화에서 GPT가 Task 하나를 누락하거나
    서로 다른 대상(정책/분야)을 하나로 합친 경우에만 완전성 기준으로 사용한다.
    """
    if not _looks_like_multi_task_request(message):
        return []

    cues = []
    for task, pattern in MULTI_TASK_CUE_PATTERNS.items():
        if task == "ELIGIBILITY" and "자격증" in message:
            continue
        match = pattern.search(message)
        if match:
            cues.append((match.start(), match.end(), task))
    cues.sort(key=lambda item: item[0])
    if len(cues) < 2:
        return []

    steps = []
    previous_end = 0
    previous_policy_id = state.get("current_policy_id") or state.get("focus_policy_id")
    for index, (start, end, task) in enumerate(cues):
        target_text = _clean_atomic_target(message[previous_end:start])
        next_start = cues[index + 1][0] if index + 1 < len(cues) else len(message)
        clause_text = message[previous_end:next_start]
        previous_end = end

        alternative = bool(re.search(
            r"말고|다른\s*(?:정책|분야|것|거|것도)?|(?:이거|그거)\s*말고",
            clause_text,
        ))
        focus_reference = _has_focus_reference(target_text or clause_text)
        intra_workflow_reference = bool(
            steps
            and re.search(
                r"^(?:나도|저도|내가|그것도|이것도|그\s*정책|이\s*정책|그거|이거)",
                target_text,
            )
        )
        action = "SHOW_ALTERNATIVES" if alternative else (
            "FOLLOW_UP"
            if (focus_reference and previous_policy_id) or intra_workflow_reference
            else "NORMAL"
        )

        topic = None
        policy_mention = None
        if task == "RECOMMEND":
            topics = _clean_action_topics(normalize_interests(target_text or clause_text))
            topic = topics[0] if topics else None
        else:
            # 특정 정책명/별칭이 포함된 문구는 그대로 보존한다. broad topic만
            # 있는 "창업 정책 설명"도 임의 정책을 택하지 않고 선택 Clarify로 간다.
            policy_mention = target_text or None
            if (focus_reference and previous_policy_id) or intra_workflow_reference:
                policy_mention = None

        exclude_policy_ids = []
        if action == "SHOW_ALTERNATIVES":
            exclude_policy_ids = list(state.get("last_result_policy_ids") or [])
            if not exclude_policy_ids and previous_policy_id:
                exclude_policy_ids = [previous_policy_id]

        steps.append({
            "action": action,
            "task": task,
            "policy_mention": policy_mention,
            "topic": topic,
            "exclude_policy_ids": exclude_policy_ids,
            "exclude_topics": [],
            "use_previous_context": action == "FOLLOW_UP",
            "explore_without_profile": "조건 없이" in clause_text,
        })
    return steps


def ensure_multi_task_workflow(state, message, tasks, clarify_reasons, bundles):
    """GPT Workflow를 사용자 문장의 명시 Task와 교차검증해 누락을 복구한다."""
    inferred = infer_atomic_workflow_from_message(state, message, bundles)
    if len(inferred) < 2:
        return tasks or [], clarify_reasons or []

    model_steps = state.get("_intent_workflow")
    if not isinstance(model_steps, list):
        model_steps = []
    model_by_task = {
        step.get("task"): step
        for step in model_steps
        if isinstance(step, dict) and step.get("task") in VALID_TASKS
    }

    merged = []
    for fallback in inferred:
        existing = dict(model_by_task.get(fallback["task"]) or {})
        step = dict(fallback)
        # GPT가 구체적인 대상/Action을 제대로 분리한 값은 우선하되, 비어 있는
        # 필드는 문장 기반 분석으로 채운다.
        for key in (
            "action", "policy_id", "policy_mention", "topic",
            "exclude_topics", "exclude_policy_ids", "exclude_policy_mentions",
            "follow_up_field", "use_previous_context", "explore_without_profile",
        ):
            value = existing.get(key)
            if value not in (None, "", []):
                step[key] = value
        # 현재 문장 조각에 공식 정책/관심 분야가 명시되어 있으면 과거 Context에
        # 끌린 모델 대상보다 우선한다.
        fallback_policy_id = _policy_id_for_action(fallback, bundles)
        existing_policy_id = _policy_id_for_action(existing, bundles) if existing else None
        if fallback_policy_id and fallback_policy_id != existing_policy_id:
            step["policy_id"] = fallback_policy_id
            step["policy_mention"] = fallback.get("policy_mention")
        if fallback["task"] == "RECOMMEND" and fallback.get("topic"):
            step["topic"] = fallback["topic"]
        step["task"] = fallback["task"]
        merged.append(step)

    tasks = [step["task"] for step in merged]
    state["_intent_workflow"] = merged
    state["_normalized_action"] = validate_action_payload({
        "action": "RUN_WORKFLOW",
        "tasks": tasks,
        "workflow": merged,
        "confidence": "high",
    })

    clarifies = list(clarify_reasons or [])
    for step in merged:
        if step["task"] == "RECOMMEND" and not step.get("topic"):
            clarifies.append("CLARIFY_PREFERENCE")
        if step["task"] == "RECOMMEND" and not step.get("explore_without_profile"):
            if get_profile_status(state["profile"]) == "INCOMPLETE":
                clarifies.append("CLARIFY_PROFILE")
    return tasks, list(dict.fromkeys(clarifies))


def _is_multi_explain_eligibility_request(message):
    if not message:
        return False
    has_explain = bool(re.search(r"(설명|알려|뭐야)", message))
    has_eligibility = any(p in message for p in ("자격 확인", "자격확인", "가능한지", "가능해", "나도 가능", "신청할 수"))
    return has_explain and has_eligibility and any(hint in message for hint in POLICY_EXPLAIN_HINTS)


def _is_multi_explain_recommend_request(message):
    """한 문장에 정책 설명과 관심분야 추천이 함께 있는지 판별한다."""
    if not message:
        return False
    has_explain = bool(re.search(r"(설명|알려|뭐야|뭔지)", message))
    has_recommend = _is_explicit_recommend_request(message)
    return has_explain and has_recommend and any(hint in message for hint in POLICY_EXPLAIN_HINTS)


def _is_multi_interest_eligibility_request(message):
    """복수 관심 분야 + 자격 요청은 단일 정책 선택 단계로 보낸다."""
    if not message or "자격증" in message:
        return False
    has_eligibility = bool(re.search(
        r"자격\s*(?:확인|봐|검토)|가능한지|신청\s*(?:할\s*)?수|지원\s*받을\s*수|대상인지",
        message,
    ))
    interests = [item.strip() for item in normalize_interests(message).split(",") if item.strip()]
    return has_eligibility and len(interests) >= 2


def _eligibility_candidates_for_interests(bundles, interest):
    """복수 분야 자격 요청에 표시할 공식 자격 정책 후보만 만든다."""
    interests = [item.strip() for item in (interest or "").split(",") if item.strip()]
    candidates = []
    for bundle in bundles:
        relevance, matched = _interest_match(bundle, interests)
        if relevance >= 0.35 and matched:
            candidates.append({
                "policy_id": bundle["policy_id"],
                "policy_name": bundle["policy_name"],
                "_relevance": relevance,
            })
    candidates.sort(key=lambda item: (-item["_relevance"], item["policy_name"]))
    return [
        {"policy_id": item["policy_id"], "policy_name": item["policy_name"]}
        for item in candidates[:8]
    ]


def _contains_exact_policy_name(bundles, message):
    """공식 정책명이 문장에 있으면 분야 단어가 여러 개여도 단일 정책 요청이다."""
    compact = re.sub(r"[^0-9a-z가-힣]", "", (message or "").lower())
    names = [bundle["policy_name"] for bundle in bundles]
    names.extend(_origin_policy_names())
    return any(
        re.sub(r"[^0-9a-z가-힣]", "", name.lower()) in compact
        for name in names if name
    )


def _extract_multi_policy_mention(message):
    """복합 질문의 첫 설명 동사 앞부분을 정책명 검색어로 사용한다."""
    head = re.split(r"\s*(?:설명|알려)", message, maxsplit=1)[0]
    return re.sub(r"\s*정책$", "", head).strip()


def _is_explicit_recommend_request(message):
    if not message:
        return False
    return any(p in message for p in ("정책 추천", "추천해줘", "추천해 줘", "추천 받고 싶", "추천받고 싶"))


def _is_explicit_policy_explain_request(message):
    """정책 영역의 명확한 설명 요청인지 코드 수준에서 판별한다."""
    if not message:
        return False
    compact = message.strip().rstrip("?!.")
    has_explain_verb = bool(re.search(r"(설명해\s*줘|알려\s*줘|뭐야)$", compact))
    return has_explain_verb and any(hint in compact for hint in POLICY_EXPLAIN_HINTS)


def _is_broad_interest_policy_request(message, bundles):
    """특정 정책명이 아닌 '분야 정책 알려줘'를 목록 탐색 요청으로 구분한다."""
    if not message or _is_explicit_recommend_request(message):
        return False
    if resolve_policy_alias(message) or _contains_exact_policy_name(bundles, message):
        return False
    if re.search(r"자격\s*(?:확인|봐)|가능한지|신청\s*(?:할\s*)?수", message):
        return False
    interests = [item.strip() for item in normalize_interests(message).split(",") if item.strip()]
    has_list_wording = "정책" in message and bool(re.search(r"알려|보여|찾아|살펴", message))
    return bool(interests) and has_list_wording


def _extract_policy_mention(message):
    """설명 동사와 군더더기 조사를 제거해 검색용 정책 언급을 만든다."""
    mention = message.strip().rstrip("?!.")
    mention = re.sub(r"\s*(?:정책)?\s*(?:에\s*대해\s*)?(?:설명해\s*줘|알려\s*줘|뭐야)$", "", mention)
    if mention.strip() in ("이", "그", "저"):
        mention = mention.strip() + " 정책"
    return mention.strip()

FOCUS_REFERENCE_PATTERNS = (
    "이 정책", "그 정책", "방금 정책", "아까 정책", "위 정책", "이거", "그거",
)


def _has_focus_reference(message):
    msg = (message or "").strip()
    return any(reference in msg for reference in FOCUS_REFERENCE_PATTERNS)


def _should_reclassify_clarify_with_ai(message, active_clarify, bundles):
    """카드 답변처럼 보이지 않는 자유 문장을 GPT Intent에 다시 보낼지 결정한다."""
    msg = (message or "").strip()
    if not msg or msg in CLARIFY_ANSWER_VALUES:
        return False
    if ":" in msg and "|" in msg:
        return False

    task_or_switch = bool(re.search(
        r"말고|다른|바꾸|취소|그만|넘어가|됐고|새로|처음부터|"
        r"정책|추천|설명|자격|알려|찾아|신청|지원|도움|보고\s*싶",
        msg,
    ))
    if not task_or_switch:
        return False

    # 정책 선택 카드에서 동사 없는 정책명·별칭은 현재 질문의 정상 답변이다.
    if active_clarify == "CLARIFY_POLICY":
        direct_policy = resolve_policy_alias(msg) or _contains_exact_policy_name(bundles, msg)
        request_verb = bool(re.search(r"추천|설명|자격|알려|찾아|바꾸|말고|다른", msg))
        if direct_policy and not request_verb:
            return False
    return True


# Clarify 중에도 새 작업으로 전환시키는 명시적 요청 패턴
NEW_REQUEST_PATTERNS = [
    "자격 확인해줘", "자격확인해줘", "자격 확인해 줘", "자격이 있는지",
    "자격 되는지", "자격되는지", "자격이 되는지", "되는지 안되는지",
    "정책에 대해 알려줘", "설명해줘", "설명해 줘", "알려줘",
    "추천해줘", "추천해 줘", "추천 받고 싶", "추천받고 싶",
]

# Clarify 답변으로 봐야 하는 값들 (새 요청으로 오인 방지)
CLARIFY_ANSWER_VALUES = ["예", "아니오", "잘 모르겠음", "모르겠음", "네", "아니요"]


def is_explicit_new_request(user_message, active_clarify):
    """
    Clarify 진행 중에 들어온 메시지가 '명확한 새 작업 요청'인지 판단.
    - 짧은 선택지 답변(예/아니오/잘 모르겠음)은 새 요청이 아님
    - CLARIFY_POLICY 중 단순 정책명 입력은 새 요청이 아님(답변임)
    """
    if not user_message:
        return False
    msg = user_message.strip()

    # 선택지 답변은 절대 새 요청이 아님
    if msg in CLARIFY_ANSWER_VALUES:
        return False
    # 구조화 답변 형식(qid:answer|...)도 답변
    if ":" in msg and "|" in msg:
        return False

    # 카드 진행 중에도 사용자가 정책/작업을 바꾸는 문장을 말하면
    # 입력값으로 소비하지 않고 새 Intent 분석으로 보낸다.
    has_switch_cue = bool(re.search(r"말고|다른\s*(?:정책|거|것)|바꾸|새로운\s*정책", msg))
    has_task_cue = bool(re.search(r"자격|정책|추천|설명|알려|신청|지원", msg))
    if has_switch_cue and has_task_cue:
        return True

    # CLARIFY_POLICY 중에는 정책명만 입력하는 게 정상 답변이므로,
    # 동사 없는 정책명은 답변으로, 설명·추천·자격 동사가 있으면 새 요청으로 본다.
    if active_clarify == "CLARIFY_POLICY":
        return any(p in msg for p in NEW_REQUEST_PATTERNS) or bool(
            re.search(r"(뭐야|뭔지|가능한지|가능해\??)$", msg)
        )

    # CLARIFY_PROFILE 중에는 Profile 관련 자연어 답변일 수 있으므로
    # 명시적 task 동사가 있을 때만 새 요청으로 본다.
    return any(p in msg for p in NEW_REQUEST_PATTERNS)


def _is_other_policy_switch_request(message):
    """직전 focus를 배제하고 다른 정책 자격을 선택하려는 문장."""
    msg = (message or "").strip()
    if not msg:
        return False
    has_switch = bool(re.search(
        r"말고\s*다른|다른\s*(?:정책|거|것)|"
        r"자격\s*다른\s*(?:거|것)|정책\s*바꾸|새로운\s*정책",
        msg,
    ))
    has_eligibility = bool(re.search(
        r"자격|가능|되는지|신청|지원\s*받",
        msg,
    ))
    return has_switch and has_eligibility


def normalize_interests(raw):
    """띄어쓰기·생활어·오탈자를 허용된 관심 분야로 정규화한다."""
    if not raw:
        return ""
    compact_raw = re.sub(r"\s+", "", str(raw).replace("·", "").lower())
    found = []
    for it in ALLOWED_INTERESTS:
        aliases = INTEREST_ALIASES.get(it, [it])
        if any(_contains_positive_term(
            compact_raw,
            re.sub(r"\s+", "", alias.replace("·", "").lower()),
        ) for alias in aliases):
            found.append(it)
    if "전체" in found and len(found) > 1:
        found = [f for f in found if f != "전체"]
    return ", ".join(found) if found else ""


def match_policy_candidates(bundles, name_query):
    """
    정책명 매칭 결과를 상태와 함께 반환
    Returns: (status, candidates)
      status: "UNIQUE" | "AMBIGUOUS" | "NOT_FOUND"
    """
    if not name_query:
        return "NOT_FOUND", []

    q = name_query.lower().replace(" ", "")

    # 1) 완전 일치 우선
    exact = [b for b in bundles if b["policy_name"].lower().replace(" ", "") == q]
    if len(exact) == 1:
        return "UNIQUE", exact

    # 2) 부분 일치 (질의가 정책명에 포함)
    partial = [b for b in bundles if q in b["policy_name"].lower().replace(" ", "")]
    if len(partial) == 1:
        return "UNIQUE", partial
    if len(partial) > 1:
        return "AMBIGUOUS", partial

    # 3) 역방향 부분 일치 (정책명이 질의에 포함) — 정책명이 충분히 길 때만
    reverse = [
        b for b in bundles
        if len(b["policy_name"].replace(" ", "")) >= 4
        and b["policy_name"].lower().replace(" ", "") in q
    ]
    if len(reverse) == 1:
        return "UNIQUE", reverse
    if len(reverse) > 1:
        return "AMBIGUOUS", reverse

    # 4) 키워드 기반 후보 (2글자 이상 토큰이 정책명에 포함)
    tokens = [t for t in re.split(r'[\s,]+', name_query) if len(t) >= 2]
    keyword_hits = []
    for b in bundles:
        bn = b["policy_name"].lower().replace(" ", "")
        if any(t.lower().replace(" ", "") in bn for t in tokens):
            keyword_hits.append(b)
    if len(keyword_hits) == 1:
        return "UNIQUE", keyword_hits
    if len(keyword_hits) > 1:
        return "AMBIGUOUS", keyword_hits

    return "NOT_FOUND", []


def _workflow_step_intro(step, bundles):
    """복합 답변에서 사용자가 요청한 대상과 작업을 먼저 확인시킨다."""
    task = step.get("task")
    if task == "EXPLAIN":
        policy_name = _policy_name_for_id(step.get("policy_id"), bundles)
        target = policy_name or step.get("policy_mention") or "선택한 정책"
        return f"먼저 {target}에 대해 설명해드릴게요."
    if task == "RECOMMEND":
        topic = step.get("topic")
        return (
            f"이어서 {topic} 분야에서 정책을 추천해드릴게요."
            if topic and topic != "전체"
            else "이어서 조건에 맞는 정책을 추천해드릴게요."
        )
    if task == "ELIGIBILITY":
        policy_name = _policy_name_for_id(step.get("policy_id"), bundles)
        target = policy_name or step.get("policy_mention") or "선택한 정책"
        return f"이어서 {target}의 자격을 확인해드릴게요."
    return ""


def compose_multi_response(task_results, steps=None, bundles=None):
    """복합 Task 결과를 요청 순서와 대상 안내를 보존해 결합한다."""
    steps = steps or []
    bundles = bundles or []
    step_by_task = {step.get("task"): step for step in steps if isinstance(step, dict)}
    ordered_tasks = []
    for step in steps:
        task = step.get("task") if isinstance(step, dict) else None
        if task in task_results and task not in ordered_tasks:
            ordered_tasks.append(task)
    for task in ("EXPLAIN", "RECOMMEND", "ELIGIBILITY"):
        if task in task_results and task not in ordered_tasks:
            ordered_tasks.append(task)
    parts = []
    labels = {
        "EXPLAIN": "정책 설명",
        "RECOMMEND": "맞춤 추천 결과",
        "ELIGIBILITY": "자격 확인 결과",
    }
    for task in ordered_tasks:
        intro = _workflow_step_intro(step_by_task.get(task, {}), bundles)
        parts.append(f"[{labels[task]}]\n{intro}\n\n{task_results[task]}".strip())
    
    return "\n\n---\n\n".join(parts)
