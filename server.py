"""
두물청 - FastAPI 백엔드 서버
HTML 프론트엔드와 통신
"""
import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import json

from data_loader import (
    load_summary_documents, load_eligibility_rules,
    load_origin_documents, enrich_origin_policy_ids, build_policy_bundles
)
from vector_store import initialize_vector_store
from state import get_default_state, update_profile
from agent import configure_openai, handle_turn

# === 앱 초기화 ===
app = FastAPI(title="두물청 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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

# 세션 저장소 (메모리, MVP)
sessions = {}


def get_session(session_id):
    if session_id not in sessions:
        sessions[session_id] = get_default_state()
    return sessions[session_id]


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
    """채팅 메시지 처리"""
    body = await request.json()
    session_id = body.get("session_id", "default")
    message = body.get("message", "")
    ui_event = body.get("ui_event", None)
    
    state = get_session(session_id)
    
    # Profile 업데이트가 있으면 handle_turn 전에 반드시 적용
    profile_data = body.get("profile", None)
    if profile_data:
        update_profile(state, profile_data)
    
    # interest를 별도 필드로 받으면 정규화하여 저장
    interest_raw = body.get("interest") or (profile_data or {}).get("interest")
    if interest_raw:
        from agent import normalize_interests
        norm = normalize_interests(interest_raw)
        state["interest_query"] = norm if norm else interest_raw
    
    # SUBMIT_ADDITIONAL_ANSWERS: 추가 질문 답변을 구조화된 형태로 저장
    if ui_event == "SUBMIT_ADDITIONAL_ANSWERS":
        from state import save_policy_answer
        target_policy = body.get("policy_id") or state.get("selected_policy_id")
        answers = body.get("answers", {}) or {}
        for qid, ans in answers.items():
            if target_policy and qid and ans:
                save_policy_answer(state, target_policy, qid, ans)
        state["active_clarify"] = None
        state.pop("_active_additional_q", None)
        if target_policy:
            state["selected_policy_id"] = target_policy
            state["focus_policy_id"] = target_policy
        state["_skip_profile_check"] = True
    
    # START_RECOMMEND_RESET: State만 정리하고 task 실행 없이 즉시 반환
    if ui_event == "START_RECOMMEND_RESET":
        state["active_clarify"] = None
        state["pending_tasks"] = []
        state.pop("_active_additional_q", None)
        state.pop("_partial_results", None)
        state.pop("_policy_candidates", None)
        state.pop("_explore_mode", None)
        state["interest_query"] = None
        state["selected_policy_id"] = None
        sessions[session_id] = state
        return JSONResponse({
            "response": "",
            "state": {
                "profile": state.get("profile"),
                "profile_status": state.get("profile_status"),
                "active_clarify": None,
                "pending_tasks": [],
                "interest_query": None,
            }
        })
    
    # START_RECOMMEND: 새 추천 시작 — 이전 작업 State 정리
    if ui_event == "START_RECOMMEND":
        state["active_clarify"] = None
        state["pending_tasks"] = []
        state.pop("_active_additional_q", None)
        state.pop("_partial_results", None)
        state.pop("_policy_candidates", None)
        state["selected_policy_id"] = None
    
    # START_ELIGIBILITY / ASK_POLICY: 이전 추천 잔여 State 정리
    if ui_event in ("START_ELIGIBILITY", "ASK_POLICY"):
        state["active_clarify"] = None
        state["pending_tasks"] = []
        state.pop("_active_additional_q", None)
        state.pop("_partial_results", None)
        state.pop("_policy_candidates", None)
        state.pop("_explore_mode", None)
    
    # SUBMIT_PROFILE / SUBMIT_RECOMMEND_PROFILE: Profile 제출
    if ui_event in ("SUBMIT_PROFILE", "SUBMIT_RECOMMEND_PROFILE"):
        state["active_clarify"] = None
        state["_skip_profile_check"] = True
    
    # Agent 실행
    state, response = handle_turn(state, message, collection, bundles, ui_event)
    sessions[session_id] = state
    
    return JSONResponse({
        "response": response,
        "state": {
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
        }
    })


@app.post("/api/reset")
async def reset(request: Request):
    """세션 초기화"""
    body = await request.json()
    session_id = body.get("session_id", "default")
    sessions[session_id] = get_default_state()
    return JSONResponse({"status": "ok"})


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
    uvicorn.run(app, host="0.0.0.0", port=8000)
