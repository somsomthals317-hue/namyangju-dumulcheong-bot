"""
두물청 - FastAPI 백엔드 서버
HTML 프론트엔드와 통신
"""
import asyncio
import os
import re
import time

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import json

import agent as agent_module
from data_loader import (
    load_summary_documents, load_eligibility_rules,
    load_origin_documents, enrich_origin_policy_ids, build_policy_bundles
)
from vector_store import initialize_vector_store
from state import (
    get_default_state, get_policy_needed_fields, get_profile_status,
    reset_task_context, update_profile,
)
from agent import (
    POLICY_QUERY_ALIASES, analyze_user_turn, configure_openai,
    get_guardrail_response, handle_turn, resolve_policy_alias,
)


NAVIGATION_PROMPT_RULES = """

추가 UI 전환 규칙:
- "프로필 다시 설정할게", "프로필 다시 입력할래", "프로필 수정해서 다시 볼게"처럼 현재 자격조회 정책의 프로필만 다시 확인하려는 말은 전체 상담 RESET이 아닙니다. 현재 자격조회 정책을 유지하고 action=FOLLOW_UP, tasks=["ELIGIBILITY"], reuse_focus=true, use_previous_context=true로 판단하세요.
- "다른 자격 조회할게", "다른 정책 자격 확인할래", "다른 자격도 볼래"처럼 새 정책의 자격을 고르려는 말은 action=SHOW_ALTERNATIVES, tasks=["ELIGIBILITY"], policy_mention=null, reuse_focus=false, use_previous_context=false로 판단하고 CLARIFY_POLICY가 필요합니다.
- "정책 알아보자", "맞춤 추천해보자", "자격조회하자"처럼 정책명이 없는 단일 메뉴 발화는 각각 EXPLAIN, RECOMMEND, ELIGIBILITY 메뉴 시작 의미입니다.
- "청년꽃간 자격조회하자"처럼 구체적인 정책명이 함께 있으면 메뉴 시작이 아니라 해당 정책의 ELIGIBILITY 요청으로 판단하세요.
- "대화 초기화할게", "대화 전부 초기화", "처음부터 새로 시작할게"처럼 상담 전체를 지우겠다는 의미일 때만 action=RESET을 사용하세요.
- '프로필'이라는 말이 명시되어 있으면 프로필 재확인과 대화 전체 초기화를 구분하세요. 프로필 재확인을 RESET으로 분류하지 마세요.
"""
if NAVIGATION_PROMPT_RULES not in agent_module.PROMPT_A_INTENT:
    agent_module.PROMPT_A_INTENT += NAVIGATION_PROMPT_RULES

# === 앱 초기화 ===
app = FastAPI(title="두물청 API")

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

# 정적 파일 (HTML, CSS, JS, 이미지)
app.mount("/static", StaticFiles(directory="static"), name="static")

# === 데이터 초기화 (서버 시작 시 1회) ===
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY가 .env에 설정되지 않았습니다!")

configure_openai(api_key)

print("[서버] 데이터 로딩 중...")
summary_docs = load_summary_documents()
rules_docs = load_eligibility_rules()
origin_docs = load_origin_documents()
enriched = enrich_origin_policy_ids(origin_docs, summary_docs)
bundles = build_policy_bundles(summary_docs, rules_docs, enriched)
_, collection = initialize_vector_store(enriched)
print(f"[서버] 초기화 완료! 정책 {collection.count()}개 적재")

# 세션 저장소 (메모리). 만료·상한·세션별 잠금으로 장시간 운영을 보호한다.
sessions = {}
session_last_seen = {}
session_locks = {}
SESSION_TTL_SECONDS = max(300, int(os.getenv("SESSION_TTL_SECONDS", "7200")))
MAX_SESSIONS = max(10, int(os.getenv("MAX_SESSIONS", "1000")))
MAX_CONCURRENT_AGENT_CALLS = max(
    1, int(os.getenv("MAX_CONCURRENT_AGENT_CALLS", "8"))
)
agent_slots = asyncio.Semaphore(MAX_CONCURRENT_AGENT_CALLS)
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def validate_session_id(value):
    session_id = str(value or "").strip()
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(
            status_code=400,
            detail="유효한 session_id가 필요합니다.",
        )
    return session_id


def _drop_session(session_id):
    sessions.pop(session_id, None)
    session_last_seen.pop(session_id, None)
    session_locks.pop(session_id, None)


def cleanup_sessions():
    now = time.monotonic()
    expired = [
        session_id
        for session_id, touched_at in session_last_seen.items()
        if now - touched_at > SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        _drop_session(session_id)

    overflow = len(sessions) - MAX_SESSIONS + 1
    if overflow > 0:
        oldest = sorted(session_last_seen, key=session_last_seen.get)[:overflow]
        for session_id in oldest:
            _drop_session(session_id)


def get_session(session_id):
    cleanup_sessions()
    if session_id not in sessions:
        sessions[session_id] = get_default_state()
    session_last_seen[session_id] = time.monotonic()
    return sessions[session_id]


def get_session_lock(session_id):
    lock = session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[session_id] = lock
    return lock


def _load_chat_html():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


def _is_bare_policy_alias_message(message):
    """별칭 자체만 입력한 짧은 검색은 기존 빠른 EXPLAIN 경로를 유지한다."""
    compact = re.sub(r"[^0-9a-z가-힣]", "", str(message or "").lower())
    if not compact:
        return False
    return any(
        compact == re.sub(r"[^0-9a-z가-힣]", "", alias.lower())
        for aliases, _ in POLICY_QUERY_ALIASES
        for alias in aliases
    )


def _simple_menu_type(message):
    compact = re.sub(r"[^0-9a-z가-힣]", "", str(message or "").lower())
    # 문장 전체가 메뉴 명령으로만 구성될 때만 여기서 확정한다.
    # 정책명/분야/조건이 붙은 구체 요청은 아래 Prompt A/Agent가 의미를 판단한다.
    explain = {
        "정책", "정책보기", "정책알아보자", "정책알아보기", "정책찾아보자", "정책좀보자",
        "청년정책", "청년정책보기", "청년정책알아보자",
    }
    recommend = {
        "맞춤추천", "맞춤추천하자", "맞춤추천해보자", "맞춤추천받자", "맞춤추천받기",
        "추천", "추천하자", "추천받자", "추천받기",
    }
    eligibility = {
        "자격조회", "자격조회하자", "자격조회해보자", "자격조회해줘",
        "자격확인", "자격확인하자", "자격확인해보자", "자격확인해줘", "자격확인하기",
    }
    if compact in explain:
        return "START_EXPLAIN"
    if compact in recommend:
        return "START_RECOMMEND"
    if compact in eligibility:
        return "START_ELIGIBILITY"
    return None


def _is_profile_reset_candidate(message):
    text = str(message or "")
    return bool(
        "프로필" in text
        and re.search(r"다시|재설정|재입력|수정|바꿔|바꾸", text)
    )


def _is_other_eligibility_candidate(message):
    text = str(message or "")
    return bool(
        re.search(r"다른|새(?:로운)?", text)
        and re.search(r"자격|자격조회|자격\s*조회|자격\s*확인", text)
    )


def _is_chat_reset_candidate(message):
    text = str(message or "")
    return bool(re.search(r"대화\s*(?:전부\s*)?초기화|처음부터\s*(?:다시|새로)|전체\s*초기화|리셋", text))


def _is_navigation_prompt_candidate(message):
    return (
        _simple_menu_type(message) is not None
        or _is_profile_reset_candidate(message)
        or _is_other_eligibility_candidate(message)
        or _is_chat_reset_candidate(message)
    )


def _clear_intent_probe_artifacts(state):
    for key in (
        "_intent_workflow", "_intent_turn_kind", "_intent_reuse_focus",
        "_intent_confidence", "_rewritten_query", "_normalized_action",
    ):
        state.pop(key, None)


def _resolve_action_policy_id(action, message=""):
    if not isinstance(action, dict):
        return None
    policy_id = str(action.get("policy_id") or "").strip() or None
    if policy_id and any(bundle.get("policy_id") == policy_id for bundle in bundles):
        return policy_id

    mention = str(action.get("policy_mention") or "").strip()
    for text in (mention, message):
        if not text:
            continue
        alias_id = resolve_policy_alias(text)
        if alias_id and any(bundle.get("policy_id") == alias_id for bundle in bundles):
            return alias_id
        compact = re.sub(r"[^0-9a-z가-힣]", "", text.lower())
        exact = [
            bundle.get("policy_id")
            for bundle in bundles
            if re.sub(r"[^0-9a-z가-힣]", "", str(bundle.get("policy_name") or "").lower()) in compact
        ]
        exact = [item for item in exact if item]
        if len(exact) == 1:
            return exact[0]
    return None


def _eligibility_bundle(policy_id):
    return next(
        (
            bundle for bundle in bundles
            if bundle.get("policy_id") == policy_id
            and bundle.get("eligibility_mode") != "INFO_ONLY"
        ),
        None,
    )


def _navigation_ui_command(state, message, inferred_action):
    """Prompt A 결과를 세 가지 기존 버튼 UI 동작으로 수렴시킨다."""
    action = inferred_action.get("action") if isinstance(inferred_action, dict) else None
    tasks = inferred_action.get("tasks") or [] if isinstance(inferred_action, dict) else []

    menu_type = _simple_menu_type(message)
    if menu_type:
        return {"type": menu_type}

    if _is_profile_reset_candidate(message):
        policy_id = (
            state.get("_eligibility_policy_id")
            or state.get("selected_policy_id")
            or state.get("current_policy_id")
            or state.get("focus_policy_id")
        )
        bundle = _eligibility_bundle(policy_id)
        if not bundle:
            return None
        # Prompt A를 먼저 호출해 의미를 확인하되, '프로필' 재설정 문장이
        # 파괴적인 RESET으로 잘못 분류되어 전체 세션이 지워지는 것은 금지한다.
        if action == "CLARIFY":
            return None
        if "ELIGIBILITY" not in tasks and action not in {"RESET", None}:
            return None
        return {
            "type": "RESET_PROFILE",
            "policy_id": policy_id,
            "fields": get_policy_needed_fields(bundle),
        }

    if _is_other_eligibility_candidate(message):
        if action == "CLARIFY":
            return None
        if "ELIGIBILITY" in tasks or action == "SHOW_ALTERNATIVES":
            return {"type": "START_ELIGIBILITY"}
        return None

    if _is_chat_reset_candidate(message) and action == "RESET":
        return {"type": "RESET_CHAT"}
    return None


def public_state(state):
    return {
        "focus_policy_id": state.get("focus_policy_id"),
        "profile_status": state.get("profile_status"),
        "profile": state.get("profile"),
        "active_clarify": state.get("active_clarify"),
        "pending_tasks": state.get("pending_tasks", []),
        "missing_fields": state.get("_missing_fields", []),
        "additional_question": state.get("_active_additional_q"),
        "policy_candidates": state.get("_policy_candidates"),
        "pending_age_confirmation": state.get("_pending_age_confirmation"),
        "interest_query": state.get("interest_query"),
        "recommendation_mode": state.get("_last_recommendation_mode"),
        "eligibility_mode": state.get("_last_eligibility_mode"),
        "eligibility_policy_id": state.get("_eligibility_policy_id"),
        "eligibility_profile_fields": state.get("_eligibility_profile_fields", []),
        "current_topic": state.get("current_topic"),
        "current_policy_id": state.get("current_policy_id"),
        "last_result_policy_ids": state.get("last_result_policy_ids", []),
        "last_action": state.get("last_action"),
    }


def _chat_response(state, response="", ui_command=None):
    payload = {
        "response": response,
        "state": public_state(state),
    }
    if ui_command:
        payload["ui_command"] = ui_command
    return JSONResponse(payload)


# === API 엔드포인트 ===

@app.get("/", response_class=HTMLResponse)
async def root():
    """기존 두물청 상담 앱"""
    content = _load_chat_html()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


@app.get("/landing", response_class=HTMLResponse)
async def landing_page():
    """두물청 서비스 소개 랜딩 페이지"""
    with open("static/landing.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    """두물청 상담 화면"""
    content = _load_chat_html()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


@app.post("/api/chat")
async def chat(request: Request):
    """채팅 메시지를 세션별로 직렬 처리하고 동기 AI 호출은 작업 스레드에서 실행한다."""
    body = await request.json()
    session_id = validate_session_id(body.get("session_id"))
    message = str(body.get("message") or "")
    ui_event = body.get("ui_event")
    input_action = body.get("action")
    profile_data = body.get("profile")
    lock = get_session_lock(session_id)

    async with lock:
        state = get_session(session_id)

        # 0. Bare Menu Command Layer
        # "자격조회"와 "자격조회하자"처럼 의미가 완전히 같은 메뉴 명령은
        # GPT 변동성을 허용하지 않고 하단 버튼과 동일한 ui_command로 즉시 수렴한다.
        # exact compact match이므로 "청년월세 자격조회" 같은 구체 요청은 절대 잡지 않는다.
        simple_menu_type = (
            _simple_menu_type(message)
            if not ui_event and input_action is None and message.strip()
            else None
        )
        if simple_menu_type:
            reset_task_context(state)
            state["messages"].append({"role": "user", "content": message.strip()})
            sessions[session_id] = state
            session_last_seen[session_id] = time.monotonic()
            return _chat_response(state, "", {"type": simple_menu_type})

        # 정책 별칭이 포함된 자연어와 나머지 UI 전환 자연어는 Prompt A가
        # Action+Task 의미를 먼저 판단한다. 코드는 그 결과를 버튼 동작으로 수렴시킨다.
        should_probe_intent = (
            not ui_event
            and input_action is None
            and message.strip()
            and get_guardrail_response(message) is None
            and (
                (
                    resolve_policy_alias(message)
                    and not _is_bare_policy_alias_message(message)
                )
                or _is_navigation_prompt_candidate(message)
            )
        )
        if should_probe_intent:
            profile_before_probe = dict(state.get("profile") or {})
            analyze_user_turn(state, message)
            inferred_action = state.pop("_normalized_action", None)
            # UI 전환 문장이 Profile 값을 바꾸는 일은 없어야 한다.
            if _is_navigation_prompt_candidate(message):
                state["profile"] = profile_before_probe
                state["profile_status"] = get_profile_status(state["profile"])
                ui_command = _navigation_ui_command(state, message, inferred_action)
                _clear_intent_probe_artifacts(state)
                if ui_command:
                    if message.strip():
                        state["messages"].append({"role": "user", "content": message.strip()})

                    if ui_command["type"] == "RESET_CHAT":
                        state.clear()
                        state.update(get_default_state())
                        response = "대화가 초기화되었어요. 정책 검색, 맞춤 추천, 자격 확인 중 무엇을 도와드릴까요?"
                    elif ui_command["type"] in {"START_ELIGIBILITY", "START_EXPLAIN", "START_RECOMMEND"}:
                        # 단일 메뉴 자연어는 하단 퀵 버튼과 같은 새 작업 단위를 시작한다.
                        # 공통 Profile은 reset_task_context에서 삭제되지 않는다.
                        reset_task_context(state)
                        response = ""
                    else:
                        policy_id = ui_command["policy_id"]
                        # 진행 중 추가질문/Workflow와 재설정 카드가 충돌하지 않게
                        # 현재 자격 단위를 닫고 같은 정책만 다시 고정한다.
                        reset_task_context(state, keep_interest=True)
                        state["selected_policy_id"] = policy_id
                        state["current_policy_id"] = policy_id
                        state["focus_policy_id"] = policy_id
                        state["_eligibility_policy_id"] = policy_id
                        state["_eligibility_profile_fields"] = list(ui_command.get("fields") or [])
                        response = "직전 정책의 자격을 다시 확인할 수 있도록 프로필을 수정해주세요."

                    if response:
                        state["messages"].append({"role": "assistant", "content": response})
                    sessions[session_id] = state
                    session_last_seen[session_id] = time.monotonic()
                    return _chat_response(state, response, ui_command)
            if inferred_action:
                input_action = inferred_action

        # 새 정책의 자격조회는 기존 공통 Profile 값을 기억하되 카드 자체는
        # 항상 다시 확인한다. 필요한 필드를 잠시 비워 Workflow를 멈춘 뒤,
        # 응답 직전에 원래 값을 복원하여 프론트 카드의 prefill로 사용한다.
        eligibility_profile_snapshot = None
        eligibility_needed_fields = []
        if isinstance(input_action, dict) and not profile_data and not ui_event:
            action_kind = input_action.get("action")
            action_tasks = input_action.get("tasks") or []
            target_policy = _resolve_action_policy_id(input_action, message)
            is_new_eligibility_unit = (
                "ELIGIBILITY" in action_tasks
                and target_policy
                and (
                    action_kind in {"NORMAL", "SHOW_ALTERNATIVES"}
                    or input_action.get("use_previous_context") is not True
                )
            )
            if is_new_eligibility_unit:
                target_bundle = _eligibility_bundle(target_policy)
                if target_bundle:
                    input_action = dict(input_action)
                    input_action["policy_id"] = target_policy
                    eligibility_needed_fields = get_policy_needed_fields(target_bundle)
                    eligibility_profile_snapshot = {
                        field: state.get("profile", {}).get(field)
                        for field in eligibility_needed_fields
                    }
                    for field in eligibility_needed_fields:
                        if field in state.get("profile", {}):
                            state["profile"][field] = None
                    state["profile_status"] = get_profile_status(state["profile"])
                    # 새 자격조회 단위는 그 정책의 과거 추가질문 답변을 재사용하지 않는다.
                    state.setdefault("policy_answers", {}).pop(target_policy, None)

        # Profile 업데이트가 있으면 handle_turn 전에 반드시 적용
        if profile_data:
            update_profile(state, profile_data)

        # '프로필 다시 설정하기'에서 같은 정책의 Profile을 제출한 경우,
        # 이전 추가질문 답변을 유지하면 run_eligibility가 질문을 완료된 것으로
        # 보고 바로 결과로 넘어간다. 이 진입점에서만 해당 정책 답변을 지워
        # 수정된 Profile 뒤 추가질문을 처음부터 자연스럽게 이어간다.
        if (
            ui_event == "SUBMIT_PROFILE"
            and isinstance(input_action, dict)
            and input_action.get("action") == "FOLLOW_UP"
            and "ELIGIBILITY" in (input_action.get("tasks") or [])
            and input_action.get("policy_id")
        ):
            state.setdefault("policy_answers", {}).pop(input_action["policy_id"], None)

        # interest를 별도 필드로 받으면 정규화하여 저장
        interest_raw = body.get("interest") or (profile_data or {}).get("interest")
        if interest_raw:
            from agent import normalize_interests
            normalized = normalize_interests(interest_raw)
            state["interest_query"] = normalized if normalized else interest_raw

        # 추가 질문 답변 저장
        if ui_event == "SUBMIT_ADDITIONAL_ANSWERS":
            from state import save_policy_answer
            target_policy = body.get("policy_id") or state.get("selected_policy_id")
            answers = body.get("answers") or {}
            for question_id, answer in answers.items():
                if target_policy and question_id and answer:
                    save_policy_answer(state, target_policy, question_id, answer)
            state["active_clarify"] = None
            state.pop("_active_additional_q", None)
            if target_policy:
                state["selected_policy_id"] = target_policy
                state["focus_policy_id"] = target_policy
            state["_skip_profile_check"] = True

        # 최종 결과에서 사용자가 추가 답변을 고치려는 경우 처음부터 다시 받는다.
        if ui_event == "EDIT_ADDITIONAL_ANSWERS":
            target_policy = body.get("policy_id") or state.get("selected_policy_id")
            if not any(
                bundle.get("policy_id") == target_policy
                and bundle.get("eligibility_mode") != "INFO_ONLY"
                for bundle in bundles
            ):
                raise HTTPException(status_code=400, detail="자격 확인 정책을 찾을 수 없습니다.")
            state.setdefault("policy_answers", {}).pop(target_policy, None)
            reset_task_context(state, keep_interest=True)
            state["selected_policy_id"] = target_policy
            state["focus_policy_id"] = target_policy
            state["_skip_profile_check"] = True
            # 답변을 지울 때 정책 대상까지 잃지 않도록 같은 정책의 자격
            # Workflow를 명시적으로 재개한다.
            state["_intent_workflow"] = [{
                "action": "FOLLOW_UP",
                "task": "ELIGIBILITY",
                "policy_id": target_policy,
                "policy_mention": None,
                "use_previous_context": True,
            }]

        # 추천 시작 화면은 task를 실행하지 않고 깨끗한 작업 상태만 반환한다.
        # reset_task_context는 사용자 공통 Profile 자체는 지우지 않는다.
        if ui_event == "START_RECOMMEND_RESET":
            reset_task_context(state)
            sessions[session_id] = state
            session_last_seen[session_id] = time.monotonic()
            return _chat_response(state)

        if ui_event == "START_RECOMMEND":
            reset_task_context(state, keep_interest=True)

        if ui_event in ("START_ELIGIBILITY", "ASK_POLICY"):
            reset_task_context(state)

        if ui_event in ("SUBMIT_PROFILE", "SUBMIT_RECOMMEND_PROFILE"):
            state["active_clarify"] = None
            state["_skip_profile_check"] = True

        try:
            async with agent_slots:
                state, response = await run_in_threadpool(
                    handle_turn,
                    state,
                    message,
                    collection,
                    bundles,
                    ui_event,
                    input_action,
                )
        finally:
            # 새 자격조회 시작 전에 잠시 비웠던 공통 Profile은 항상 복원한다.
            # Workflow는 CLARIFY_PROFILE에서 멈춰 있으므로 사용자는 값을 확인/수정한 뒤 제출한다.
            if eligibility_profile_snapshot is not None:
                for field, value in eligibility_profile_snapshot.items():
                    if field in state.get("profile", {}):
                        state["profile"][field] = value
                state["profile_status"] = get_profile_status(state["profile"])
                if (
                    state.get("active_clarify") == "CLARIFY_PROFILE"
                    and "ELIGIBILITY" in state.get("pending_tasks", [])
                ):
                    # Agent의 3개 단위 배치 제한 대신 이 정책이 실제 사용하는
                    # 기본 Profile 필드를 한 카드에 모두 보여준다.
                    state["_missing_fields"] = list(eligibility_needed_fields)

        sessions[session_id] = state
        session_last_seen[session_id] = time.monotonic()
        return _chat_response(state, response)


@app.post("/api/reset")
async def reset(request: Request):
    """세션 초기화"""
    body = await request.json()
    session_id = validate_session_id(body.get("session_id"))
    lock = get_session_lock(session_id)
    async with lock:
        sessions[session_id] = get_default_state()
        session_last_seen[session_id] = time.monotonic()
    return JSONResponse({"status": "ok"})


@app.get("/healthz")
async def healthz():
    """Render와 운영 점검용 경량 헬스 체크."""
    cleanup_sessions()
    return {
        "status": "ok",
        "policies": len(summary_docs),
        "vector_documents": collection.count(),
        "active_sessions": len(sessions),
    }


@app.get("/api/policies")
async def get_policies():
    """설명·추천 32개와 자격 선택 가능한 신청형 25개를 구분해 반환한다."""
    all_policies = [{"policy_id": d["policy_id"], "policy_name": d["policy_name"]} for d in summary_docs]
    info_only = [
        {"policy_id": d["policy_id"], "policy_name": d["policy_name"]}
        for d in summary_docs if d.get("service_type") in {"INFORMATION", "FACILITY"}
    ]
    return JSONResponse({
        "policies": all_policies,
        "eligibility_policies": [
            item for item in all_policies
            if item["policy_id"].startswith("NYJ-YOUTH-")
        ],
        "explain_only": info_only,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
