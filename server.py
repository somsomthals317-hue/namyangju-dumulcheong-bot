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

from data_loader import (
    load_summary_documents, load_eligibility_rules,
    load_origin_documents, enrich_origin_policy_ids, build_policy_bundles
)
from vector_store import initialize_vector_store
from state import get_default_state, reset_task_context, update_profile
from agent import configure_openai, handle_turn

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


# === API 엔드포인트 ===

@app.get("/", response_class=HTMLResponse)
async def root():
    """기존 두물청 상담 앱"""
    with open("static/index.html", "r", encoding="utf-8") as f:
        content = f.read()
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
    with open("static/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


@app.post("/api/chat")
async def chat(request: Request):
    """채팅 메시지를 세션별로 직렬 처리하고 동기 AI 호출은 작업 스레드에서 실행한다."""
    body = await request.json()
    session_id = validate_session_id(body.get("session_id"))
    message = str(body.get("message") or "")
    ui_event = body.get("ui_event")
    input_action = body.get("action")
    lock = get_session_lock(session_id)

    async with lock:
        state = get_session(session_id)

        # Profile 업데이트가 있으면 handle_turn 전에 반드시 적용
        profile_data = body.get("profile")
        if profile_data:
            update_profile(state, profile_data)

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

        # 추천 시작 화면은 task를 실행하지 않고 깨끗한 상태만 반환한다.
        if ui_event == "START_RECOMMEND_RESET":
            reset_task_context(state)
            sessions[session_id] = state
            session_last_seen[session_id] = time.monotonic()
            return JSONResponse({
                "response": "",
                "state": public_state(state),
            })

        if ui_event == "START_RECOMMEND":
            reset_task_context(state, keep_interest=True)

        if ui_event in ("START_ELIGIBILITY", "ASK_POLICY"):
            reset_task_context(state)

        if ui_event in ("SUBMIT_PROFILE", "SUBMIT_RECOMMEND_PROFILE"):
            state["active_clarify"] = None
            state["_skip_profile_check"] = True

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
        sessions[session_id] = state
        session_last_seen[session_id] = time.monotonic()

        return JSONResponse({
            "response": response,
            "state": public_state(state),
        })


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

