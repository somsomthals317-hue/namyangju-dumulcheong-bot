# 두물청 — 남양주시 청년정책 AI Agent 코드 흐름 정리

> Kiro에서 만든 문서 형식을 유지하면서 현재 FastAPI 구현, 안정화 로직, 테스트 결과와 수정 내역을 반영한 문서입니다.

- 최종 정리일: 2026-08-27
- 실행 방식: FastAPI + HTML/CSS/JavaScript
- 기본 주소: `http://localhost:8000`
- 대상 정책: 설명·추천 32개, 자격 선택 25개(신청형 25개 + 정보·시설 INFO_ONLY 7개)

---

## 1. 현재 완성된 기능

| 기능 | 설명 | 현재 방식 |
|---|---|---|
| EXPLAIN | 정책 내용·대상·지원·기간·신청 방법·공식 출처 설명 | 정책 원문과 요약 데이터 기반 |
| RECOMMEND | 관심 분야와 사용자 조건에 맞는 정책 추천 | 규칙 FAIL 제외 + GPT 의미 관련/무관 판정·순위 + 공식 근거 이유 |
| ELIGIBILITY | 특정 정책의 신청 가능성 확인 | 명확한 규칙 우선 + GPT 보조 검토 + 보수적 병합 |
| 자연어 질문 | 버튼을 누르지 않고 문장으로 질문 | 명시 패턴 우선, 애매할 때만 OpenAI Intent 분석 |
| 멀티쿼리 | 설명·추천·자격 중 둘 이상을 한 문장에서 요청 | EXPLAIN → RECOMMEND → ELIGIBILITY 순서로 실행 |
| 멀티턴 | 추가 질문 후 원래 작업 재개 | 세션 State와 pending task 사용 |
| Guardrail | 타 지자체·가짜 정책·개인정보 추측·프롬프트 공격 방지 | 모델 호출 전 코드 차단 |
| 출처 링크 | 공식 페이지와 신청 페이지 표시 | 화면 문구는 유지하고 URL만 정확히 링크 처리 |

> 자격 판정은 안내용입니다. 실제 선정과 지급 여부는 최신 공고와 담당 부서 심사를 기준으로 합니다.

### 이번 1~7차 하드닝의 핵심

1. 진행 중인 카드에서도 긴 자연어는 GPT가 `CLARIFY_ANSWER / NEW_TASK / SWITCH_POLICY / CANCEL`로 다시 판정합니다.
2. 새 작업·정책 전환 시 중앙 `reset_task_context()`가 이전 focus, 후보, 카드, 검색어를 한 번에 정리합니다.
3. Intent·Profile 추출·자격 보조 검토는 OpenAI JSON 모드를 사용하고 허용값과 정책 ID를 코드가 다시 검증합니다.
4. 전체 32개 정책에 `recommendation_interests`를 명시해 GPT 장애 시에도 분야별 후보를 안정적으로 복구합니다.
5. 추가 자격 질문은 최대 3개씩, 전체 진행률과 남은 개수를 표시하며 최종 결과에서 답변을 다시 입력할 수 있습니다.
6. FastAPI는 세션별 잠금, GPT 작업 스레드, 동시 실행 상한, 세션 TTL·상한, 요청 timeout, `/healthz`를 사용합니다.
7. GitHub Actions가 Python 컴파일, Intent·State·32개 데이터 계약 회귀 테스트, Frontend JavaScript 문법 검사를 자동 실행합니다.


### Action 정규화와 복합 Workflow (최종 구조)

자연어와 버튼은 이제 서로 다른 분기에서 답을 만들지 않습니다.

```text
사용자 입력 또는 버튼
→ Action / Intent 분석
→ State Transition
→ task별 대상이 보존된 Workflow 생성
→ 정책 검색·추천·자격 Tool 실행
→ 정보가 부족하면 Workflow를 멈추고 카드 표시
→ 답변 입력 후 같은 지점부터 재개
→ 모든 task가 끝났을 때 결과 결합
```

공통 Action은 다음과 같습니다.

| Action | 의미 | 이전 State 처리 |
|---|---|---|
| `SEARCH_POLICY` | 새 정책 검색 | 새 대상이 있으면 현재 발화를 우선 |
| `FOLLOW_UP` | 직전 단일 정책의 기간·방법·서류 질문 | `current_policy_id` 유지 |
| `CHECK_ELIGIBILITY` | 특정 정책 자격 확인 | 명시적 대상 또는 명시적 지시어일 때만 이전 정책 사용 |
| `SHOW_ALTERNATIVES` | 지금 보던 정책·분야가 아닌 대안 | 직전 대상은 제외 조건으로만 사용 |
| `CHANGE_TOPIC` | 농업에서 취업처럼 분야 전환 | 기존 topic/policy를 새 주제로 교체 |
| `RESET` | 처음부터 다시 시작 | task 관련 State 전체 초기화, 저장 Profile은 기본 초기화 규칙 적용 |

Prompt A는 Intent와 Action을 따로 두 번 호출하지 않습니다. 한 번의 JSON 응답에서 `action`, `tasks`, `workflow`, 각 step의 `policy_mention/topic`을 함께 받으며, 코드는 허용된 값만 통과시킵니다. 버튼은 GPT를 호출하지 않고 같은 Action Schema를 직접 만들어 동일한 Handler로 보냅니다.

복합 요청의 안전 규칙:

- “월세 정책 설명하고 기본소득 자격 확인”처럼 task별 대상이 다르면 각 step의 정책명을 따로 보존합니다.
- 설명+자격에서 자격 질문이 필요하면 설명 결과를 내부에 보관하고, 자격 확인이 끝난 뒤 두 결과를 한 번에 보여줍니다.
- 추천+자격에서 자격 대상이 명시되지 않으면 추천 결과를 보여준 뒤 사용자가 정책을 선택하게 합니다. 추천 1위를 자격 대상으로 임의 선택하지 않습니다.
- 추가 Profile·정책별 질문이 여러 번 생겨도 `active_workflow.index`에서 멈추고 같은 step을 재실행합니다.
- “다른 정책 자격 확인”은 직전 정책을 재사용하지 않고 정책 선택 화면으로 이동합니다.
- “그 정책 신청 기간은?”은 새 검색이 아니라 직전 단일 정책의 `FOLLOW_UP`으로 처리합니다.

현재 자동 회귀 검사는 기존 Intent/State/Data 계약 16개와 Action/Workflow 7개, 총 23개를 실행하며 Python 컴파일과 Frontend JavaScript 문법도 함께 확인합니다.

---

## 2. 최종 파일 구조

```text
남양주시 청년 정책 Agent 모델 프로젝트/
├── .env                                  # OPENAI_API_KEY (외부 공유 금지)
├── server.py                             # FastAPI 실행 진입점과 API
├── agent.py                              # Intent, Clarify, EXPLAIN/RECOMMEND/ELIGIBILITY
├── state.py                              # 세션별 Profile·대화 상태 관리
├── data_loader.py                        # 정책 JSON 로딩과 Bundle 생성
├── vector_store.py                       # ChromaDB + OpenAI Embedding 검색
├── config.py                             # 경로·모델·Profile 설정
├── prompts.py                            # Intent·추천 의미 판정·자격 보조 검토 Prompt
├── requirements.txt                      # FastAPI 실행·테스트 패키지
│
├── static/
│   ├── index.html                        # 실제 웹 UI, CSS와 JavaScript 포함
│   └── jeong_yakyong.jpg                 # 정약용 캐릭터 이미지
│
├── policy_origin_extracted/json/         # 공식 정책 원문 32개
├── summary_documents_with_policy_id.json # 전체 정책 요약 32개
├── policy_eligibility_rules.json         # 구조화 자격·INFO_ONLY 규칙 32개
├── policy_origin.zip                     # 원문 백업본, 런타임 미사용
│
├── audit_policy_catalog.py               # ZIP·추출본·Summary·Rules·링크 감사
├── policy_full_coverage_matrix.md        # 32개 정책 감사표
├── FAIL_초기_진단.md                     # 수정 전 FAIL과 원인 보존
├── test_full_policy_catalog.py           # 32개 정책 720항목 전수검사
├── test_full_policy_catalog_result.md    # 전수검사 최종 결과
├── test_logic_regression.py              # 규칙·AI 병합·Intent·검색 안전 회귀 테스트 39개
├── test_acceptance.py                    # Agent 수용 테스트 22개
├── test_e2e_api.py                       # FastAPI E2E 반복 테스트
├── test_ui_playwright.py                 # 실제 클릭·UI Playwright 테스트 96개
└── README_코드흐름_정리.md               # 현재 문서
```

### 정리 원칙

- FastAPI 실행에 필요한 파일은 유지합니다.
- 정책 원문과 원본 ZIP은 근거 데이터와 복구용으로 보존합니다.
- 테스트 코드는 이후 수정 때 회귀 버그를 잡기 위해 유지합니다.
- 캐시, 과거 Streamlit UI, 중복 이미지, 일회성 Gemini 스크립트, 자동 생성 결과 파일은 제거합니다.

---

## 3. 기술 구성

| 영역 | 사용 기술 |
|---|---|
| Web API | FastAPI, Uvicorn |
| Frontend | 단일 `static/index.html`의 HTML/CSS/JavaScript |
| Chat model | OpenAI `gpt-4o-mini` |
| Embedding | OpenAI `text-embedding-3-small` |
| Vector search | ChromaDB in-memory collection |
| Session | FastAPI 프로세스 메모리 + 브라우저 `sessionStorage` session ID |
| UI test | Playwright |
| Logic test | unittest 및 프로젝트 수용 테스트 |

### 환경 변수

`.env`에는 다음 값이 필요합니다.

```dotenv
OPENAI_API_KEY=본인의_OpenAI_API_Key
# 선택값: 쉼표로 구분한 외부 Frontend origin만 허용
ALLOWED_ORIGINS=https://example.com
# 선택값: 기본 7200초, 최대 세션 1000개, 동시 Agent 실행 8개
SESSION_TTL_SECONDS=7200
MAX_SESSIONS=1000
MAX_CONCURRENT_AGENT_CALLS=8
```

같은 FastAPI가 HTML도 제공하는 현재 구성은 same-origin이므로 `ALLOWED_ORIGINS`를 비워두는 것이 기본입니다. API 키는 README, 테스트 결과, 화면 응답에 기록하지 않습니다.

---

## 4. 정책 데이터 관계

### 데이터 개수

| 데이터 | 개수 | 사용처 |
|---|---:|---|
| 공식 원문 JSON | 32개 | EXPLAIN과 Vector 검색 |
| Summary | 32개 | 정책 요약, 추천 표시 |
| Eligibility Rules | 32개 | 신청형 규칙 25개 + INFO_ONLY 7개 |
| 정보·시설 | 7개 | 설명·추천 가능, 자격 요청 시 모집·예약 확인 안내 |

### policy_id 체계

```text
신청형 정책 25개     → NYJ-YOUTH-xxx
정보·시설 안내 7개  → NYJ-EXPLAIN-xxx
```

### Policy Bundle

`data_loader.build_policy_bundles()`가 Summary와 Rules를 `policy_id`로 결합합니다.

```python
{
    "policy_id": "NYJ-YOUTH-010",
    "policy_name": "청년기본소득",
    "category": "청년기본소득",
    "summary": "...",
    "benefit": "...",
    "basic_condition": {...},
    "caution_condition": [...],
    "additional_questions": [...],
    "eligibility_mode": "FULL",  # 또는 INFO_ONLY
    "unverified_conditions": [...],
    "unknown_flag": False,
}
```

이 Bundle이 RECOMMEND와 ELIGIBILITY의 공통 판단 기준입니다.

### 8개 자격 조건 필드

Summary와 Rules의 모든 정책에는 아래 8개 조건 키가 항상 존재합니다.

| Summary 키 | Rules `basic_condition` 키 | 의미 |
|---|---|---|
| `age_condition` | `age` | 나이 |
| `residency_condition` | `residency` | 거주지 |
| `income_condition` | `income` | 소득 |
| `employment_condition` | `employment` | 취업·재직 상태 |
| `student_condition` | `student` | 학생·학적 상태 |
| `startup_condition` | `startup` | 창업 상태 |
| `housing_condition` | `housing` | 주택 보유·주거 상태 |
| `marriage_condition` | `marriage` | 혼인 상태 |

- 키를 생략하거나 빈 문자열로 두지 않습니다. 실제 조건이 없으면 반드시 `해당없음`으로 기록합니다.
- 신청형 25개는 Summary 조건과 Rules 조건이 완전히 같은 값을 사용합니다.
- 원문에 나이 조건이 없으면 청년 정책 공통값인 `만 19세~만 39세`, 거주지 조건은 이 남양주시 서비스의 공통값인 `남양주시`, 사업기간이 없으면 `2026년 중`을 사용합니다. 원문에 더 구체적인 값이 있으면 그 값을 우선합니다.
- `지역주도형 청년일자리사업`처럼 원문이 경기도 범위를 명시한 예외는 그 구체적인 값을 유지합니다.
- `startup_condition`은 정책 분야명이 아니라 신청자의 창업 상태만 기록합니다. 현재 허용값은 `해당없음`, `미창업`, `예비창업자,기창업자`입니다.
- 자격 조건 필드에는 `확인 필요`를 넣지 않습니다. 원문에 실제 조건이 있으면 조건 문구를 기록하고, 답변이 필요한 조건은 `additional_questions`에서 묻습니다.
- `application_period`의 `공지 확인 필요`는 자격 조건이 아니라 신청기간 안내이므로 유지할 수 있습니다.
- 정보·시설 7개는 8개 조건을 모두 `해당없음`으로 두고 `eligibility_mode: INFO_ONLY`로 구분합니다.

---

## 5. 프로그램 시작 흐름

```text
python server.py
    ↓
server.py가 .env 로드
    ↓
OpenAI client 설정
    ↓
원문 32개 + Summary 32개 + Rules 32개 로드
    ↓
Policy Bundle 32개 생성
    ↓
원문 32개를 OpenAI Embedding으로 변환
    ↓
ChromaDB in-memory collection 생성
    ↓
FastAPI가 0.0.0.0:8000에서 실행
    ↓
브라우저에서 http://localhost:8000 접속
```

서버가 시작될 때 API 키가 없으면 즉시 오류를 내고 중단합니다. 초기 VectorStore도 서버 시작 시 한 번 생성합니다.

---

## 6. FastAPI 엔드포인트

### `GET /`

- `static/index.html`을 UTF-8로 읽어 반환합니다.
- 캐시를 끄는 헤더를 넣어 코드 수정 후 오래된 화면이 남는 문제를 줄입니다.

### `GET /api/policies`

- `policies`: 설명 가능한 전체 32개
- `eligibility_policies`: 자격 메뉴에서 선택 가능한 신청형 25개
- `explain_only`: 하위 호환 필드명이며, 현재는 INFO_ONLY 정보·시설 7개

### `POST /api/chat`

요청 예시:

```json
{
  "session_id": "browser-session-id",
  "message": "청년기본소득 자격 확인해줘",
  "ui_event": "START_ELIGIBILITY",
  "profile": {
    "age": 24,
    "residency": "예"
  }
}
```

처리 순서:

1. 필수 `session_id` 형식과 길이를 검증합니다.
2. 같은 세션의 요청을 잠금으로 직렬화하고 TTL이 지난 세션을 정리합니다.
3. 전달된 Profile과 관심 분야를 정규화해 먼저 저장합니다.
4. UI 이벤트에 따라 `reset_task_context()`로 이전 Clarify와 임시 결과를 정리합니다.
5. 추가 질문 제출·재입력이면 해당 정책 답변만 저장하거나 초기화합니다.
6. 동기 OpenAI 작업을 FastAPI 이벤트 루프 밖의 작업 스레드에서 실행합니다.
7. 응답 텍스트와 화면 렌더링에 필요한 State만 반환합니다.

### `POST /api/reset`

- 해당 브라우저 세션의 서버 State를 초기값으로 바꿉니다.
- 다른 사용자의 세션에는 영향을 주지 않습니다.

### `GET /healthz`

- Render 상태 점검용 경량 endpoint입니다.
- 서버 상태, 정책 수, Vector 문서 수, 활성 세션 수를 반환합니다.
- OpenAI 응답 생성은 호출하지 않습니다.

---

## 7. State 구조

```python
{
    "profile": {
        "age": None,
        "residency": None,
        "employment": None,
        "student": None,
        "startup": None,
        "housing": None,
        "marriage": None,
    },
    "interest_query": None,
    "focus_policy_id": None,
    "selected_policy_id": None,
    "messages": [],
    "pending_tasks": [],
    "active_clarify": None,
    "policy_answers": {},
    "profile_status": "INCOMPLETE",
}
```

### 중요한 State 역할

| 필드 | 역할 |
|---|---|
| `focus_policy_id` | “이 정책”, “그 정책”이 가리키는 현재 정책 |
| `selected_policy_id` | 자격 확인을 진행 중인 정책 |
| `pending_tasks` | 추가 질문 뒤 재개할 EXPLAIN/RECOMMEND/ELIGIBILITY |
| `active_clarify` | 현재 사용자 답변을 기다리는 질문 종류 |
| `policy_answers` | `{policy_id: {question_id: answer}}` 형태의 정책별 답변 |
| `_partial_results` | 멀티쿼리 도중 이미 완료된 결과 |
| `_policy_candidates` | 정책명이 모호할 때 UI에 표시할 후보 |
| `_last_recommendation_mode` | `AI_HYBRID` / `RULE_FALLBACK` / `RULE_ONLY_HARD_FAIL` 추적 |
| `_last_eligibility_mode` | `COLLECTING_INPUT` / `AI_HYBRID` / `RULE_FALLBACK` / `RULE_ONLY_HARD_FAIL` 추적 |

### 세션 유지 범위

- 브라우저는 `sessionStorage`에 session ID를 저장합니다.
- 같은 탭에서 새로고침하면 session ID가 유지됩니다.
- 탭을 닫으면 브라우저 session ID는 사라집니다.
- 서버 State는 메모리 저장이므로 서버를 재시작하면 초기화됩니다.
- 기본 2시간 동안 사용하지 않은 세션은 제거되고, 기본 최대 1,000개까지만 보관합니다.
- 브라우저 요청은 60초 후 중단되어 무한 로딩을 막고, 같은 세션의 중복 클릭은 서버에서도 순서대로 처리합니다.

---

## 8. 한 턴의 전체 로직

`agent.handle_turn()`이 한 번의 메시지 또는 버튼 이벤트를 총괄합니다.

```text
사용자 메시지 / UI 클릭
    ↓
① Guardrail 검사
    ↓
② 만 나이 확인 또는 진행 중 Clarify 처리
    ↓
③ 명시적 자연어 패턴 / UI 이벤트 라우팅
    ↓
④ 애매한 문장과 진행 중 카드의 작업 전환 문장을 OpenAI Prompt A로 Intent 분석
    ↓
⑤ Clarify가 필요한지 State와 교차 검증
    ↓
⑥ EXPLAIN → RECOMMEND → ELIGIBILITY 순서로 실행
    ↓
⑦ 멀티쿼리 결과 결합 또는 후속 질문 표시
    ↓
⑧ State와 응답 반환
```

### 왜 명시 패턴을 먼저 처리하는가

`설명해줘 + 정책명`, `추천해줘`, `자격 확인해줘`처럼 의도가 명확한 문장은 모델 분류를 거치지 않습니다. 같은 질문이 실행할 때마다 다른 task로 분류되는 현상을 막기 위한 구조입니다. 코드 패턴으로 확정할 수 없는 자연어는 Prompt A가 EXPLAIN / RECOMMEND / ELIGIBILITY 중 의도를 판별합니다.

OpenAI Intent 분석은 다음처럼 애매한 자연어에만 사용합니다.

```text
“나한테 도움 되는 거 있어?”
“아까 말한 것 중 집 관련해서 더 볼 수 있어?”
```

---

## 9. Intent와 Clarify

### Task 종류

| Task | 의미 |
|---|---|
| `EXPLAIN` | 정책의 공식 내용 설명 |
| `RECOMMEND` | 사용자 조건과 관심 분야로 정책 추천 |
| `ELIGIBILITY` | 특정 정책의 자격 가능성 판정 |

### Clarify 종류

| 상태 | 발생 조건 | 화면 처리 |
|---|---|---|
| `CLARIFY_POLICY` | 설명·자격 대상 정책이 없음 또는 모호함 | 정책 선택 카드 |
| `CLARIFY_PREFERENCE` | 추천 관심 분야가 없음 | 관심 분야 카드 |
| `CLARIFY_PROFILE` | 추천 또는 자격 확인에 Profile이 부족함 | Profile 입력 카드 |
| `CLARIFY_ADDITIONAL` | 정책별 세부 조건이 부족함 | 최대 3개 질문 + 전체 진행률 카드 |

Clarify 도중 `추천해줘`, `설명해줘`, `자격 확인해줘` 같은 명확한 새 요청이 들어오면 이전 질문 상태를 정리하고 새 task로 전환합니다. 코드로 확정되지 않는 긴 문장은 GPT가 `turn_kind`와 `reuse_focus`를 JSON으로 반환합니다. `reuse_focus=true`는 “이 정책”, “그 정책”, “방금 정책”처럼 사용자가 직전 정책을 직접 가리킨 경우에만 허용합니다. `이 정책 말고 다른 정책`, `나 자격 다른 거 확인하고 싶어`처럼 기존 대상을 배제하는 문장은 `focus_policy_id`와 `selected_policy_id`를 지운 뒤 `CLARIFY_POLICY` 정책 선택 카드로 이동합니다. 단, `예`, `아니오`, 날짜, 구조화 답변은 현재 질문에 대한 답으로 처리합니다.

이 전환은 정규식 하나에만 의존하지 않습니다. 자주 발생하는 위험 표현은 코드가 먼저 안전하게 처리하고, 코드로 확정되지 않은 완전한 자연어 요청은 Prompt A가 최근 대화와 현재 focus를 함께 받아 다시 EXPLAIN / RECOMMEND / ELIGIBILITY로 분류합니다. GPT가 `말산업 ... 자격되는지`를 EXPLAIN으로 잘못 분류해도 자격 생활어와 공식 정책 별칭을 코드가 교차 검증해 ELIGIBILITY 흐름을 지킵니다.

### 버튼 입력과 자연어 입력은 어디서 만나는가

| 입력 방식 | 처음 처리 | 이후 공통 처리 |
|---|---|---|
| 하단 버튼·카드 | `ui_event`로 task와 선택값을 확정 | 같은 `run_explain` / `run_recommend` / `run_eligibility` 실행 |
| 명확한 자연어 | 코드 패턴과 별칭으로 task·정책·관심 분야 추출 | 같은 task 함수와 State 사용 |
| 애매한 자연어 | OpenAI Prompt A가 Intent와 State patch를 JSON으로 분석 | 검증 후 같은 task 함수 실행 |

따라서 버튼과 자연어는 입구만 다르고 정책 데이터, 자격 규칙, GPT 검증, 최종 포맷은 공유합니다. 버튼은 이미 구조화된 값이라 Intent GPT가 필요 없고, 자연어는 코드로 확정할 수 없는 경우에만 Intent GPT가 들어갑니다.

### 정책명과 관심 분야가 함께 보일 때의 우선순위

```text
1. 문장 안에 공식 정책명 하나가 완전히 들어 있음 → 그 단일 정책 우선
2. 공식 정책명 없이 관심 분야가 둘 이상 + 자격 요청 → 정책 선택 카드
3. 관심 분야가 하나 이상 + 추천 요청 → RECOMMEND
```

예를 들어 `농업인자녀 대학생 학자금 이자지원 자격 확인해줘`에는 농업·교육 단어가 함께 있지만 공식 정책명이 있으므로 해당 정책으로 바로 갑니다. 반대로 `기본 소득과 농업에 대해 자격확인해줘`는 정책명이 아니라 분야 두 개이므로 임의로 하나를 고르지 않고 `어떤 정책의 자격을 확인하고 싶으신가요?`와 관련 정책 선택 카드를 표시합니다.

---

## 10. EXPLAIN 상세 흐름

```text
정책 설명 요청
    ↓
정책명 별칭 확인
    ↓
정확 일치 / 부분 일치 / Vector 검색
    ↓
사용자가 실제로 입력한 핵심어가 후보의 공식 이름·분류·태그·요약에 직접 근거가 있음?
    ├─ 아니오 → 임의 정책을 대신 보여주지 않고 "직접 일치 정책 없음" 안내
    └─ 예
후보가 명확함? ── 아니오 → CLARIFY_POLICY
    ↓ 예
focus_policy_id 저장
    ↓
공식 원문 + Summary에서 설명 구성
    ↓
지원대상·지원내용·기간·방법·제출서류·출처 반환
```

### 설명 안정화 원칙

- 정확한 정책명과 자주 쓰는 별칭은 코드로 우선 매칭합니다.
- `정장 대여`, `면접 사진`, `취업 로드맵` 같은 생활 표현은 공식 정책 `일자리카페`로 연결합니다.
- 정책 설명 본문은 생성 모델이 새로 지어내지 않고 공식 원문에서 구성합니다.
- 출처가 있으면 공식 링크를 화면에 표시합니다.
- URL 뒤의 설명 괄호는 화면에 남기고 실제 링크 주소에는 넣지 않습니다.
- 후보가 애매하면 임의 정책을 선택하지 않고 사용자가 고르게 합니다.
- Vector 검색의 1위라는 이유만으로 답하지 않습니다. 사용자 원문의 핵심어 중 60% 이상이 공식 정책 근거에서 확인되어야 하며, GPT가 바꿔 쓴 검색어는 이 근거 판정에 사용하지 않습니다.
- `노트북 구입비`, `교통비`처럼 현재 데이터에 직접 근거가 없는 요청은 농업·입영 등 가까워 보이는 다른 정책으로 강제 연결하지 않습니다.

정보·시설 7개도 다른 정책과 같은 고정 ID·Summary·Rules를 갖습니다. EXPLAIN과 RECOMMEND에는 포함하지만 자격 선택 목록에서는 제외합니다. 사용자가 자연어로 자격을 직접 물어도 공개된 페이지가 개별 신청사업이 아니므로 `INFO_ONLY`가 PASS/FAIL을 만들지 않고 공실 모집·대관·예약 등 공식 확인 방법과 링크만 안내합니다. `남양주시 청년창업센터`처럼 이름에 `창업`이 들어간 정확한 정책명은 일반 창업 분야 요청보다 먼저 인식합니다.

---

## 11. RECOMMEND 상세 흐름

```text
추천 요청
    ↓
관심 분야 있음? ── 아니오 → CLARIFY_PREFERENCE
    ↓ 예
Profile 필요? ── 예 → CLARIFY_PROFILE
    ↓
32개 Policy Bundle의 명확한 조건을 규칙으로 비교
    ↓
명확한 FAIL 정책 제외
    ↓
나머지 전체 후보를 GPT에 전달
    ↓
GPT가 후보마다 의미 관련/무관을 독립 판정
    ↓
관련 후보만 의미 적합성 순위와 내부 근거 작성
    ↓
후보 ID·정책명 연결·점수·JSON 검증
    ↓
화면 추천 이유는 해당 ID의 공식 대상·혜택으로 구성
    ↓
PASS 우선 + UNKNOWN 보조 추천
    └─ GPT 오류/누락/형식 오류 → 코드 키워드 후보로 자동 복구
```

### 지원 관심 분야

```text
취업 / 주거 / 창업 / 교육 / 복지 / 참여·문화 / 기본소득 / 농업 / 전체
```

### 자연어 관심 분야 정규화

자연어 추천은 글자가 정확히 일치할 때만 동작하지 않습니다. 생활 표현을 아래 표준 분야로 바꾼 뒤 후보를 찾습니다.

| 사용자 표현 예 | 저장되는 분야 |
|---|---|
| `면접 컨설팅`, `취준`, `자소서`, `구직` | 취업 |
| `월세`, `보증금`, `집 구하기` | 주거 |
| `자격증비`, `시험비`, `응시료` | 교육 |
| `기본 소득`, `기본소득` | 기본소득 |
| `농어`, `농어촌`, `농사`, `영농`, `귀농` | 농업 |

`면접 컨설팅 받을 만한 정책 추천해줘`는 취업 추천으로 이동하고, 동사 없이 `면접 컨설팅`만 입력하면 공식 별칭을 통해 `취업성공 프로젝트` 설명으로 연결합니다.

관심 분야 후보는 정책 본문 전체를 단순 부분 문자열로 검색하지 않습니다. 공식 `대분류·정책명·소분류·태그`만 분야 근거로 사용합니다. 따라서 `미창업`, `취·창업 외`처럼 대상이나 혜택 문장에 우연히 포함된 단어 때문에 창업 정책으로 분류되는 일을 막습니다.

또한 `월세 말고 창업`, `헤어 말고 정장 대여`처럼 제외 표현이 붙은 단어와 `미창업`, `미취업` 같은 부정형 단어는 관심 분야나 정책 별칭으로 잡지 않습니다.

### 추천 원칙

- 코드는 32개 Policy Bundle을 준비하고, 명확한 FAIL만 GPT 후보에서 제외합니다.
- 나이, 주민등록, 취업, 학생, 주택, 혼인처럼 구조화된 8개 필드를 먼저 비교합니다. `경기도 거주`처럼 공통 Profile의 남양주시 예/아니오만으로 확정할 수 없는 실제 조건은 추가 질문으로 넘깁니다.
- GPT는 남은 모든 후보를 `is_relevant: true/false`로 의미 판정한 뒤 관련 후보만 정렬합니다. 따라서 코드 키워드가 놓친 관련 정책도 볼 수 있고, `미창업`처럼 단어만 맞는 후보는 제거할 수 있습니다.
- GPT는 코드가 정한 PASS / UNKNOWN 상태를 바꾸지 못합니다.
- GPT의 내부 순위 근거는 정책 ID 연결 검증에만 쓰고, 사용자가 보는 추천 이유는 해당 ID의 공식 대상·혜택으로 구성합니다.
- GPT는 전달받은 모든 후보 ID를 정확히 한 번씩 판정해야 합니다. ID 누락·중복·가짜 ID, 잘못된 JSON·점수·이유, 다른 정책명 혼입이 있으면 응답 전체를 버립니다.
- GPT 호출이 실패하면 서비스 오류를 내지 않고 기존 키워드 관련성 순위로 자동 복구합니다.
- 추가 증빙이 필요한 정책은 UNKNOWN으로 분리합니다.
- “조건 없이 탐색”은 현재 세션에 저장된 Profile과 과거 정책별 추가답변을 추천 필터에 사용하지 않습니다. 이전에 만 34세 Profile을 입력했더라도 기본소득을 조건 없이 보면 만 24세 조건 때문에 숨기지 않습니다.
- 조건 없이 복수 분야를 선택하면 `📍 [기본소득]`, `📍 [농업]`처럼 분야별로 나누고 각 분야 안에서 순위를 표시합니다.
- 기본소득은 일반적인 `지원금·급여`가 아니라 `청년기본소득·분기별 지역화폐` 같은 고유 표현으로만 매칭해 취업수당이 섞이지 않게 합니다.
- 자연어에서 추출한 `interest_query`를 API 응답으로 UI에 다시 전달해 Profile 카드와 `조건 없이 찾아보기`를 거쳐도 선택 분야가 `전체`로 덮어써지지 않게 합니다.
- 추천 결과는 신청 가능성을 안내할 뿐 최종 선정을 보장하지 않습니다.

### 맞춤 추천과 조건 없이 탐색의 차이

| 모드 | Profile 사용 | 명확한 FAIL 정책 | 화면 의미 |
|---|---|---|---|
| 맞춤 추천 | 사용 | 추천 목록에서 제외하고 이유 안내 | 현재 입력 조건에서의 신청 가능성 |
| 조건 없이 탐색 | 사용하지 않음 | 자격 때문에 제외하지 않음 | 관심 분야에 연결된 정책 개요 |

맞춤 추천에서 결과가 없을 때는 단순히 `관련 정책 없음`으로 끝내지 않습니다.

- 분야 후보 자체가 없음: 현재 보유 데이터에서 해당 분야로 연결된 정책이 없다고 안내합니다.
- 후보는 있지만 Profile이 명확히 불충족: `나이 조건`, `거주 조건` 등 제외 이유와 제외된 정책 수를 안내합니다.
- 자격과 무관하게 보고 싶음: `조건 없이 찾아보기`를 안내합니다.

추천 Profile이 필요할 때 말풍선에는 `맞춤 추천을 위해 몇 가지 여쭤볼게요! 😊`만 표시합니다. 실제 나이·거주·취업 등의 질문은 바로 아래 추천 조건 카드에서만 받으므로 같은 질문을 텍스트와 카드에 중복 표시하지 않습니다.

---

## 12. ELIGIBILITY 상세 흐름

```text
자격 확인 요청
    ↓
정책 특정? ── 아니오/복수 → CLARIFY_POLICY
    ↓ 예
INFO_ONLY 정보·시설? ── 예 → PASS/FAIL 없이 공식 모집·예약 링크 안내
    ↓ 아니오
해당 정책에 필요한 Profile 필드 계산
    ↓
필수 Profile 부족? ── 예 → CLARIFY_PROFILE
    ↓ 아니오
기본 조건 비교
    ↓
명확한 불충족 있음? ── 예 → FAIL
    ↓ 아니오
추가 질문 미응답? ── 예 → CLARIFY_ADDITIONAL
    ↓ 아니오
질문별 합격 방향 + OR 조건 판정
    ↓
규칙 FAIL? ── 예 → 즉시 FAIL, GPT 호출 안 함
    ↓ 아니오
정책 + Profile + 모든 추가답변 + 규칙 결과를 GPT에 1회 전달
    ↓
GPT JSON 검증 + 규칙 결과와 보수적으로 병합
    ↓
PASS / UNKNOWN
    └─ GPT 오류/형식 오류 → 규칙 결과로 자동 복구
```

### 결과 의미

| 상태 | 의미 |
|---|---|
| `PASS` | 입력 정보 기준으로 명확한 불충족이 없음 |
| `FAIL` | 하나 이상의 필수 조건이 명확하게 맞지 않음 |
| `UNKNOWN` | 실제 소득·증빙·생년월일·추가답변 등 아직 확보하지 못한 판정 정보가 있음 |

### 판정 안정화 규칙

- `만 24세`는 정확히 24세로 처리합니다.
- `만 19세 이상 39세 미만`은 19~38세로 처리합니다.
- 부정형 질문은 “아니오”가 합격 방향일 수 있으므로 question ID별 방향을 적용합니다.
- 청년기본소득의 경기도 거주기간은 `3년 계속` 또는 `합산 10년` 중 하나를 충족하면 됩니다.
- 생년월일처럼 최신 분기 공고와 대조해야 하는 값은 임의로 PASS 처리하지 않고 UNKNOWN으로 남깁니다.
- 원문에 조건이 없는 필드는 `해당없음`으로 확정합니다. 누락이나 빈 문자열을 UNKNOWN의 근거로 사용하지 않습니다.
- 프로젝트가 승인한 공통 기본값은 나이 `만 19세~만 39세`, 거주 `남양주시`, 사업기간 `2026년 중`입니다. 원문에 더 구체적인 값이 있는 정책만 해당 값을 우선합니다.
- 공통 Profile의 `남양주시 주민등록 아니오`는 남양주시 조건에서 FAIL입니다. 경기도 거주를 명시한 예외 정책은 경기도 거주 추가 질문으로 판정합니다.
- 공통 Profile 선택지로 답할 수 없는 `면접 예정`, `영농경력` 같은 조건은 취업/미취업 카드로 억지 비교하지 않고 정책별 추가 질문에서 확인합니다.
- 추가 질문 답변은 정책별로 격리해 다른 정책 판정에 섞이지 않게 합니다.
- 추가 질문은 GPT가 임의 생성하지 않습니다. `policy_eligibility_rules.json`의 공식 질문을 화면에서 먼저 받고, 완료된 답변 전체를 GPT 보조 검토에 전달합니다.
- 규칙 FAIL은 GPT를 호출하지 않고 즉시 확정하므로 PASS로 뒤집힐 수 없습니다.
- 여러 관심 분야만 말하고 자격을 요청하면 GPT나 코드가 임의 정책을 고르지 않습니다. 자격 판정은 반드시 공식 정책 하나를 선택한 뒤 시작합니다.
- 추가 질문 단계에서도 선택한 정책명과 해당 정책의 공식 링크를 항상 표시합니다.
- 한 카드에는 최대 3개만 표시하고 `전체 N개 중 X~Y번 · Z개 남음`으로 진행 상황을 보여줍니다.
- 최종 결과의 `추가 답변 다시 입력하기`는 해당 정책 답변만 지우고 같은 정책 질문을 처음부터 다시 엽니다.

### 규칙 결과와 GPT 결과의 최종 병합표

| 규칙 결과 | GPT 보조 결과 | 최종 결과 | 이유 |
|---|---|---|---|
| FAIL | 호출하지 않음 | FAIL | 명확한 나이·거주지·필수답변 위반 보호 |
| PASS | PASS | PASS | 두 판단이 일치 |
| PASS | FAIL 또는 UNKNOWN | UNKNOWN | 충돌을 임의 확정하지 않고 추가 확인 |
| UNKNOWN | PASS / FAIL / UNKNOWN | UNKNOWN | 빠진 정보나 공고 대조 필요성을 GPT가 덮어쓰지 못함 |
| PASS 또는 UNKNOWN | 오류·잘못된 JSON | 원래 규칙 결과 | 자동 fallback |

여기서 UNKNOWN은 단순히 모델이 판단하기 어렵다는 뜻만이 아닙니다. `잘 모르겠음`, 최신 공고표와 생년월일 대조, 소득·증빙 확인처럼 현재 입력만으로 확정하면 안 되는 경우를 뜻합니다.

---

## 13. 멀티쿼리와 멀티턴

### 멀티쿼리 예시

```text
“청년기본소득 설명해주고 나도 가능한지 확인해줘”
```

다음처럼 Profile 정보나 서로 다른 task가 한 문장에 섞여도 원래 요청을 버리지 않습니다.

```text
“청년기본소득 설명해주고 만 24세인 내가 신청 가능한지도 봐줘”
→ age=24 저장 → EXPLAIN 유지 → ELIGIBILITY Profile/추가 질문 계속

“청년월세 지원사업 설명해주고 조건 없이 면접 준비 정책도 추천해줘”
→ 청년월세 EXPLAIN + 취업 분야 조건 없는 RECOMMEND 결합
```

처리 순서:

1. EXPLAIN 결과를 먼저 생성합니다.
2. ELIGIBILITY에 정보가 부족하면 설명 결과를 `_partial_results`에 보존합니다.
3. Profile 또는 추가 질문을 받습니다.
4. 답변 후 ELIGIBILITY를 재개합니다.
5. 두 결과를 구분선과 제목으로 합쳐 표시합니다.

### 만 나이 자연어 처리

```text
사용자: “24살이야”
Agent: “만 나이 24세라는 뜻인가요?”
사용자: “예”
Agent: Profile에 age=24 저장 후 원래 작업 재개
```

한국식 나이와 만 나이를 혼동해 잘못 판정하는 것을 막기 위한 확인 절차입니다. 단, `만 24세인데 기본소득 자격도 봐줘`처럼 나이와 task가 한 문장에 있으면 나이만 저장하고 응답을 끝내지 않고 원래 task를 계속 처리합니다.

---

## 14. 엉뚱한 답변 방지 장치

모델을 호출하기 전에 다음 입력을 코드로 확인합니다.

| 입력 | 처리 |
|---|---|
| 타 지자체 정책 질문 | 남양주시 범위만 안내한다고 응답 |
| 프롬프트·시스템 지시 무시 요청 | 공식 정책 데이터 범위로 제한 |
| 개인정보를 추측해 달라는 요청 | 직접 입력하도록 안내 |
| 합격·선정·지급을 확정 또는 보장해 달라는 요청 | 안내형 판정임을 밝히고 공식 심사 확인 안내 |
| 지나치게 긴 반복 문자열 | 짧은 정책 질문을 요청 |
| 글자가 없는 특수문자 입력 | 의도를 확인할 수 없다고 안내 |
| focus 없이 “이 정책”이라고만 질문 | 임의 정책 선택 금지 |
| 데이터에 없는 정책 | 찾지 못했다고 안내 또는 후보 재확인 |

또한 추천·자격의 OpenAI 오류나 잘못된 JSON은 FastAPI 전체 요청을 500으로 만들지 않고 검증된 규칙 결과로 자동 복구합니다. 후보에 없는 정책 ID도 화면에 노출하지 않습니다.

---

## 15. Frontend 클릭 흐름

### 정책 알아보기

```text
정책 알아보기 클릭 → 전체 32개 목록 → 정책 선택 → /api/chat → EXPLAIN 표시
```

### 맞춤 추천받기

```text
추천 클릭 → 이전 추천 State 초기화 → 관심 분야 선택 → 한 줄 안내 + 추천 조건 카드 → 맞춤 추천 또는 조건 없이 탐색 → 결과 표시
```

### 자격 확인하기

```text
자격 클릭 → 신청형 25개 목록 → 정책 선택 → 필요한 Profile·추가 질문·판정

정보·시설 7개를 자연어로 직접 자격 문의 → INFO_ONLY 공식 모집·예약 안내(PASS/FAIL 없음)
```

### 중복 클릭 방지

- 요청 중에는 `isWaiting`으로 추가 전송을 막습니다.
- 제출한 카드와 선택 컨트롤을 비활성화합니다.
- 메뉴 전환 시 이전 Clarify와 임시 선택을 정리합니다.
- 정책 목록은 시작 시 미리 로드하고, 필요하면 완료될 때까지 기다립니다.

---

## 16. 테스트 구성

| 테스트 | 범위 | 마지막 결과 |
|---|---|---:|
| `audit_policy_catalog.py` | 원본 ZIP·추출본·Summary·Rules·ID·공식 링크 32개 정합성 | PASS, 오류 0 |
| `test_full_policy_catalog.py` | 32개 × 데이터·설명·자격·추천·버튼·자연어·링크·AI 의미필터 | 720/720 PASS |
| `test_acceptance.py` | 데이터·State·Agent 수용 조건 | 22 PASS |
| `test_e2e_api.py` | 정책 API + 하이브리드 AI 경로 + 8개 시나리오 × 3회 | 24/24, ALL PASS |
| `test_logic_regression.py` | 나이·질문 방향·OR 조건·Guardrail·AI 병합·관심분야·정책명·검색 안전성·8필드 스키마 | 39/39 PASS |
| `test_ui_playwright.py` | 클릭·자연어·멀티턴·모바일·하드 사용자 입력 | 96/96 PASS |

### 검증 범위

- EXPLAIN: 정확한 이름, 생활어 별칭, 오탈자, 의미 검색, 정보·시설 정책, 출처 링크, 관련 없는 Vector 후보 강제 선택 차단
- RECOMMEND: 단일·복수 관심 분야, Profile, 조건 없이 탐색, 저장된 불리한 Profile 무시, 기본소득·농업 분야 분리, `미창업`·`말고` 부정 표현 제외, 없는 AI 정책 ID 차단
- ELIGIBILITY: PASS/FAIL/UNKNOWN, 정확 나이, 미만, 부정형 질문, OR 조건, 자유입력 날짜, FAIL 불변, 충돌 시 UNKNOWN
- 자연어: 설명→자격 전환, 설명+추천·설명+자격 복합 요청, 문장 속 만 나이, `기본 소득·농어·면접 컨설팅` 생활어, Clarify 중 새 요청
- 안정성: 빠른 메뉴 클릭, 중복 제출, 초기화, 새로고침, XSS 이스케이프, HTTP 4xx/5xx
- 전체 정책 전수: 32개 공식명 자연어 설명·버튼 설명·공식 링크·추천 도달성·GPT 의미 판정, 신청형 25개 자연어/버튼 자격, 정보·시설 7개 INFO_ONLY와 자격 목록 제외를 각각 검사
- 데이터 정합성: `policy_origin.zip` 32개와 추출본 32개의 원문·정책명·출처를 비교하고 Summary 32개·Rules 32개의 ID와 조건 이관을 검사

### 테스트 실행

서버가 실행 중인 상태에서:

```powershell
python -m unittest -v test_logic_regression.py
python audit_policy_catalog.py
python test_full_policy_catalog.py
python test_acceptance.py
python test_e2e_api.py
python test_ui_playwright.py --runs 1 --hard-only
python test_ui_playwright.py --runs 1
```

`--hard-only`는 실사용 하드 시나리오 묶음만 빠르게 실행합니다. 최종 배포 전에는 옵션 없이 전체 96개를 실행합니다.

UI 테스트는 `test_ui_result.md`와 `screenshots/`를 자동 생성할 수 있습니다. 이 파일들은 테스트 증거용 산출물이므로 소스 정리 시 삭제해도 다시 만들어집니다.

---

## 17. 설치와 실행

### 최초 설치

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### FastAPI 실행

프로젝트 폴더에서:

```powershell
python server.py
```

브라우저 주소:

```text
http://localhost:8000
```

### 서버 종료

실행한 터미널에서 `Ctrl+C`를 누릅니다.

---

## 18. 이번 안정화에서 수정한 내역

### Agent 로직

- EXPLAIN·RECOMMEND·ELIGIBILITY 명시 요청의 Intent를 코드로 고정했습니다.
- `자격증`의 `자격`을 ELIGIBILITY로 오인하던 조건을 분리했습니다.
- 정책 설명을 원문·Summary 기반으로 구성해 출처 누락과 환각을 줄였습니다.
- 추천을 한 단계 더 확장해 `명확한 규칙 FAIL 제외 → 나머지 전체 후보의 GPT 관련/무관 의미 판정 → 관련 후보 순위 → 코드 fallback`으로 변경했습니다.
- GPT가 한 정책의 추천 이유에 다른 정책 내용을 섞는 경우를 차단하고, 화면 이유는 항상 해당 policy_id의 공식 Bundle에서 만들도록 고정했습니다.
- 자격 판정을 `공식 추가 질문 → 규칙 판정 → GPT 보조 검토 → 보수적 병합` 구조로 변경했습니다.
- 명확한 규칙 FAIL은 GPT 호출 없이 즉시 확정하고, 규칙 UNKNOWN은 GPT가 PASS로 승격하지 못하게 했습니다.
- 정확 나이, 범위, `미만`, 부정형 질문, OR 조건 판정을 보강했습니다.
- 타 지자체·프롬프트 공격·개인정보 추측·비정상 입력 Guardrail을 추가했습니다.
- 멀티쿼리 중 완료 결과 보존과 후속 task 재개를 보강했습니다.
- Clarify 중 새 요청 전환과 정책별 답변 격리를 보강했습니다.
- 공식 정책명 안에 여러 분야 단어가 포함되어도 복수 분야 요청으로 오인하지 않도록 정책명 완전 포함을 우선합니다.
- 복수 분야 자격 요청은 관련 정책 선택 카드로 보내고 임의 정책 판정을 차단합니다.
- 문장 안에 만 나이와 task가 함께 있으면 나이를 저장한 뒤 원래 설명·추천·자격 작업을 계속합니다.
- `면접 컨설팅` 같은 단독 키워드와 생활어 관심 분야 별칭을 보강했습니다.
- `정장 대여`·`면접 사진`·`취업 로드맵`을 `일자리카페`로 연결하는 정책 생활어 별칭을 추가했습니다.
- `참여 가능해?`·`신청 가능해?`·`지원받을 수 있어?`를 설명이 아니라 자격 확인으로 고정했습니다.
- 관심 분야 분류 근거를 공식 분류·정책명·태그로 제한해 `미창업`, `취·창업 외` 같은 우연한 본문 단어의 오탐을 차단했습니다.
- 32개 정책 모두에 나이·거주·소득·취업·학생·창업·주택·혼인 8개 조건을 고정하고, 조건이 없으면 `해당없음`으로 통일했습니다.
- 신청형 정책은 승인된 공통 기본값과 원문의 구체값을 적용하고, Summary와 Rules가 같은 조건값을 사용하도록 정규화했습니다.
- 정확한 공식 정책명이 포함된 설명 요청은 정책명에 일반적인 설명 키워드가 없어도 GPT Intent로 넘기지 않고 EXPLAIN으로 고정합니다.
- 정보·시설 7개를 Summary·Rules에 포함하고, 자격 요청에는 INFO_ONLY로 근거 없는 PASS/FAIL 대신 공식 모집·예약 안내를 제공합니다.
- `말고`·`제외`·`미창업`·`미취업` 등 부정 문맥을 별칭과 관심 분야 추출에서 제외합니다.
- EXPLAIN Vector 후보에는 사용자 원문 핵심어 직접 근거 검사를 추가해, 관련 없는 정책을 가장 가까운 답처럼 강제로 보여주지 않습니다.
- 합격·선정·지급 확정 요구는 정책 설명으로 흘러가지 않고 Guardrail에서 안내형 판정으로 제한합니다.

### FastAPI와 State

- `/api/chat`에서 Profile을 Agent 실행 전에 반영하도록 정리했습니다.
- 메뉴 전환과 새 추천 시작 시 이전 Clarify·후보·부분 결과를 초기화했습니다.
- 추가 질문을 구조화해 정책별로 저장하도록 보강했습니다.
- API 오류가 전체 웹 서비스 중단으로 이어지지 않도록 처리했습니다.

### Frontend

- 정책 목록을 앱 시작 시 미리 불러오도록 수정했습니다.
- 추가 질문을 최대 3개씩 표시하고 객관식·날짜·자유입력을 지원합니다.
- Profile과 서버 State를 동기화했습니다.
- 빠른 클릭, 중복 전송, 중복 카드 제출을 차단했습니다.
- 공식 링크는 계속 표시하면서 괄호 설명이 URL에 포함되지 않도록 수정했습니다.
- `만 나이가 24살` 같은 명시 표현은 즉시 저장하고, 나이 확인 중 정책 카드가 중복 표시되지 않도록 수정했습니다.
- 판정 결과의 `프로필 다시 설정하기`는 헤더 옆 모달이 아니라 채팅 프로필 카드를 다시 표시하며 기존 값을 미리 채웁니다.
- 추천 Profile 말풍선의 중복 질문을 제거하고 실제 입력은 추천 조건 카드에만 표시합니다.
- 조건 없이 탐색은 세션에 남은 이전 Profile·추가답변을 사용하지 않아 기본소득 같은 정책이 잘못 사라지지 않습니다.
- 맞춤 추천에서 후보가 사라지면 분야 데이터 부재와 Profile 명확 불충족을 나눠 이유를 표시합니다.
- 자격 확인 요청은 정책 설명을 반복하지 않고 필요한 Profile·추가 질문과 판정으로 바로 진행합니다. 설명을 함께 명시한 복합 요청만 EXPLAIN과 ELIGIBILITY를 함께 실행합니다.
- 기존 두물청 디자인과 정약용 캐릭터 형태는 유지했습니다.

### 테스트

- Windows cp949 환경에서 이모지 때문에 테스트가 중단되던 출력을 보강했습니다.
- Playwright 검증을 96개 항목으로 확장했고 PASS 96 / FAIL 0, Console 오류 0, HTTP 4xx·5xx 0을 확인했습니다.
- 예측하기 어려운 자연어·버튼 혼합 하드 시나리오에 검색 안전 4항목(창업 오탐, 정장 별칭, 없는 혜택, 부정 분야)을 추가했습니다.
- 규칙·GPT 병합·Intent·검색 안전·8필드 스키마 회귀 테스트 39개를 통과했습니다.
- 수용 테스트 22개와 API E2E 8개 시나리오 × 3회를 모두 통과했습니다.
- 첫 32개 전수검사 720항목에서 19건을 발견해 원인별로 수정했고, 같은 검사를 720/720 PASS로 마쳤습니다. 최초 FAIL은 `FAIL_초기_진단.md`에 보존합니다.

---

## 19. 현재 남아 있는 운영상 주의점

- FastAPI 세션은 메모리 저장이므로 서버 재시작 시 초기화됩니다.
- ChromaDB도 in-memory라 서버 시작 때 Embedding을 다시 생성합니다.
- 서버 시작 시 OpenAI Embedding이 필요하므로 API 또는 인터넷 연결이 완전히 없으면 Vector 검색 초기화가 실패할 수 있습니다.
- 서버가 이미 실행 중일 때 추천·자격 GPT 호출만 실패하면 규칙 결과로 복구됩니다. 애매한 자연어 Intent 분석은 모델 연결이 필요합니다.
- 정책 내용은 현재 포함된 JSON 기준이므로 공고가 바뀌면 원문·Summary·Rules를 함께 갱신해야 합니다.
- 실서비스 배포 전에는 CORS 제한, 세션 저장소, 요청 제한, 로그 정책과 보안 설정을 추가로 검토해야 합니다.

---

## 20. 핵심 한 줄 정리

```text
FastAPI가 세션과 정책 데이터를 준비하고,
agent.py가 명시 규칙 → 필요한 경우 OpenAI Intent → Clarify → EXPLAIN은 공식 문서 기반, RECOMMEND·ELIGIBILITY는 규칙+GPT 하이브리드 순서로 처리하며,
static/index.html이 버튼·카드·자연어 채팅과 공식 링크를 사용자에게 보여준다.
```
