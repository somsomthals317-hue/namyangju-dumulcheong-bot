# 두물청 — 남양주시 청년정책 AI 상담 서비스

두물청은 남양주시 청년정책을 자연어로 탐색하고, 관심 분야에 맞는 정책을 추천받고, 특정 정책의 신청 자격을 확인할 수 있는 AI 상담 서비스입니다.

- **정책 설명**: 정책 대상·혜택·기간·신청 방법·제출 서류 안내
- **맞춤 추천**: 관심 분야와 사용자 프로필을 반영한 정책 추천
- **자격 확인**: 정책별 기본 조건과 추가 질문을 바탕으로 신청 가능성 안내
- **배포**: Render 기반 웹 서비스

> 자격 결과는 안내형 가능성 판정입니다. 실제 신청·선정·지급 여부는 최신 공식 공고와 담당 부서의 심사가 기준입니다.

---

## 1. 서비스 구조

```text
사용자 자연어 / UI 버튼
        ↓
FastAPI /api/chat
        ↓
Action + Task 해석
        ↓
State / Workflow 관리
        ↓
EXPLAIN / RECOMMEND / ELIGIBILITY
        ↓
정책 데이터 · Vector DB · 규칙 기반 검증 · LLM 보조 판단
        ↓
정책 설명 / 추천 카드 / 자격 결과 반환
```

두물청은 사용자의 문장을 바로 검색에 넣지 않고, 먼저 **대화의 관계(Action)** 와 **수행할 기능(Task)** 을 분리합니다.

### Action

| Action | 의미 |
|---|---|
| `NORMAL` | 이전 대화를 특별히 참조하지 않는 새 요청 |
| `FOLLOW_UP` | 직전 정책·추천 결과를 이어서 질문 |
| `SHOW_ALTERNATIVES` | 직전 대상을 제외하고 다른 정책 요청 |
| `CHANGE_TOPIC` | 추천 관심 분야 변경 |
| `RESET` | 상담 상태 초기화 |
| `CLARIFY` | 요청 의미를 신뢰성 있게 판단하기 어려운 경우 |

### Task

| Task | 역할 | 예시 |
|---|---|---|
| `EXPLAIN` | 정책 내용 설명 | “청년월세 알려줘” |
| `RECOMMEND` | 정책 후보 추천 | “취업 정책 추천해줘” |
| `ELIGIBILITY` | 특정 정책 신청 가능성 확인 | “말산업 인턴 나도 가능해?” |

Action과 Task는 독립적으로 조합됩니다. 예를 들어 `FOLLOW_UP + ELIGIBILITY`는 “이 정책 나도 돼?”를, `SHOW_ALTERNATIVES + RECOMMEND`는 “이거 말고 다른 정책 없어?”를 의미합니다.

---

## 2. 자연어 Intent와 Workflow

자연어 입력은 LLM 기반 Intent 분석과 코드 검증을 함께 사용합니다.

- **Prompt A**: 자연어에서 Action, Task, 정책명, 관심 분야, Workflow를 구조화
- **코드 검증**: 허용된 Action/Task Schema와 실제 정책 ID, State를 검증
- **복합 요청**: 하나의 문장에 여러 작업이 있으면 작업별 Atomic Step으로 분리
- **UI 버튼**: 이미 구조화된 Action을 전달하고 자연어와 동일한 실행기로 진입

예를 들어 아래 요청은 두 단계 Workflow로 처리됩니다.

```text
“청년기본소득 설명하고 자격도 확인해줘”

1. NORMAL + EXPLAIN
2. FOLLOW_UP + ELIGIBILITY
```

정책명이나 관심 분야가 새로 명시된 경우에는 과거 대화 Context보다 현재 발화를 우선합니다.

### 자연어와 하단 메뉴 UI 동기화

정책명·관심 분야가 없는 **일반 메뉴 이동 발화**는 Prompt A가 의미를 판정한 뒤 하단 버튼과 같은 UI로 수렴합니다.

```text
“정책 알아보자”      → [정책 알아보기]와 같은 정책 선택 카드
“맞춤 추천해보자”    → [맞춤 추천받기]와 같은 새 추천 Flow
“자격조회하자”       → [자격 확인하기]와 같은 정책 선택 카드
```

반대로 `청년월세 정책 알아보자`, `청년꽃간 자격조회하자`처럼 **구체적인 정책명이 포함된 발화는 메뉴 이동으로 바꾸지 않고 해당 정책의 EXPLAIN/ELIGIBILITY 요청으로 처리**합니다.

---

## 3. 정책 설명 — RAG

정책 설명은 남양주시 공식 정책 데이터를 기반으로 합니다.

```text
사용자 질의
  → 정책명 / alias 확인
  → Vector DB 후보 검색
  → 정책 ID 검증
  → 공식 원문 + 구조화 Summary 기반 응답
```

임베딩 검색 결과를 그대로 답변으로 사용하지 않고, 공식 정책명·정책 ID·원문 근거를 다시 확인합니다. 직접적인 근거가 부족한 경우 유사한 정책을 임의로 설명하지 않습니다.

현재 데이터 범위:

- 정책 설명 대상: **32개**
- 신청 자격 확인 가능 정책: **25개**
- 정보·시설 안내 전용 정책: **7개**

---

## 4. 맞춤 추천

추천은 관심 분야와 세션의 공통 Profile을 사용합니다.

지원 분야:

`취업` · `주거` · `창업` · `교육` · `복지` · `참여·문화` · `기본소득` · `농업` · `전체`

### Profile State 원칙

Profile은 세션의 공통 상태로 누적·수정됩니다.

- 자연어로 `만 25세`, `남양주시 거주`, `미취업` 등을 말하면 해당 값이 Profile에 누적됩니다.
- Profile 자체는 추천 Flow를 새로 시작해도 삭제하지 않습니다.
- 저장된 Profile 값은 추천 조건 카드에 **prefill**되어 사용자가 확인·수정할 수 있습니다.

### 새 추천 Flow

하단 `[맞춤 추천받기]`는 항상 새 추천 Flow를 시작합니다.

```text
[맞춤 추천받기]
  → 이전 추천의 관심분야 / pending task / clarify 정리
  → 공통 Profile은 유지
  → 관심 분야 카드
  → 저장된 Profile이 prefill된 추천 조건 카드
  → 추천 실행
```

자연어의 새 `NORMAL + RECOMMEND` 요청도 관심 분야가 이미 명시된 경우에는 관심 분야 선택 단계를 생략할 수 있지만, **일반 맞춤 추천은 최종 결과 전에 추천 조건 카드를 보여주어 현재 Profile을 다시 확인할 수 있게 합니다.**

`SHOW_ALTERNATIVES`, `CHANGE_TOPIC`, `FOLLOW_UP` 같은 자연어 후속 추천은 기존 Profile을 유지합니다. 예를 들어 `이거 말고 다른 정책 추천해줘`는 직전 결과를 제외하고, `주거로 바꿔줘`는 관심 분야만 주거로 변경합니다.

### 추천 조건 카드

- `[이 조건으로 추천받기]` → 현재 카드의 Profile을 저장·사용해 맞춤 추천
- `[조건 없이 찾아보기]` → Profile은 저장해 둔 채 **이번 추천에서만 Profile을 무시**하고 정책 자체를 탐색

`조건 없이`는 Action이 아니라 **이번 추천에서 Profile을 사용할지 여부를 나타내는 one-turn 실행 옵션**입니다. `SHOW_ALTERNATIVES`나 `CHANGE_TOPIC`이라는 이유만으로 자동 활성화하지 않습니다.

자연어에서도 `조건 없이 취업 정책 보여줘`, `프로필 상관없이 추천해줘`처럼 명시한 경우에만 Profile 카드를 생략하고 조건 없는 탐색으로 바로 진행합니다.

### 추천 처리

1. 공식 정책 Bundle에서 관심 분야 후보 생성
2. 일반 추천이면 사용자 Profile로 명확한 불충족 정책 제외
3. LLM이 후보의 의미 적합성을 보조 판정
4. 정책 ID와 공식 데이터를 다시 검증
5. 추천 이유와 함께 정책 카드 반환

LLM에 Profile을 전달할 때 State 값 자체는 유지하되, 문맥이 모호한 `residency=예/아니오`만 각각 `남양주시 거주/남양주시 비거주`로 의미화해 전달합니다.

---

## 5. 자격 확인

자격 확인은 LLM 단독 판단이 아니라 **구조화된 정책 규칙을 우선**합니다.

```text
정책 선택
  → 해당 정책에 필요한 Profile 입력
  → 기본 조건 비교
  → 정책별 추가 질문
  → 규칙 기반 PASS / FAIL / UNKNOWN
  → 필요한 경우 LLM 보조 검토
```

### 사용자 Profile

- 만 나이
- 남양주시 거주 여부
- 취업 상태
- 학생 여부
- 창업 상태
- 주택 보유 여부
- 혼인 여부

정책마다 실제 판정에 필요한 필드만 요청합니다. 기본 조건에서 명확한 불충족이 확인되면 규칙 결과를 우선하며, LLM이 이를 뒤집지 않습니다.

정책별 Additional Question은 질문 문구·선택지·question_id가 함께 전달되므로 `예/아니오` 값의 문맥이 유지됩니다.

최종 상태는 다음 세 가지입니다.

- `PASS`: 입력 정보 기준 신청 가능성이 높음
- `FAIL`: 명확한 불충족 조건 존재
- `UNKNOWN`: 최신 공고·증빙·추가 확인이 필요한 조건 존재

---

## 6. 주요 기술

| 영역 | 기술 |
|---|---|
| Backend | Python, FastAPI |
| Frontend | HTML, CSS, JavaScript |
| LLM | OpenAI API |
| Retrieval | Vector DB, Embedding 기반 정책 검색 |
| Data | 남양주시 청년정책 공식 자료, 구조화 Summary / Eligibility Rule |
| State | 세션 기반 Profile·정책·Workflow 상태 관리 |
| Test | pytest 기반 Intent·Action·Workflow·정책 회귀 테스트 |
| Deploy | GitHub + Render Auto-Deploy |

---

## 7. 주요 파일

```text
.
├─ server.py                 # FastAPI API 및 세션 처리
├─ agent.py                  # Action/Task, Workflow, RAG·추천·자격 실행
├─ sitecustomize.py          # 추천 Profile/UI 계약 및 GPT 전달값 호환 보정
├─ prompts.py                # Intent·추천·자격 검토 Prompt
├─ state.py                  # Profile 및 대화 State 관리
├─ data_loader.py            # 정책 데이터 로딩 및 Bundle 구성
├─ vector_store.py           # 정책 Embedding / 검색
├─ static/
│  ├─ index.html             # 챗봇 UI
│  └─ navigation_sync.js     # 자연어 State → 기존 버튼 UI 동기화
├─ test_*.py                 # 회귀 및 Workflow 테스트
└─ requirements.txt
```

---

## 8. 로컬 실행

### 환경 변수

`.env` 파일에 OpenAI API Key를 설정합니다.

```env
OPENAI_API_KEY=your_api_key
```

### 실행

```bash
pip install -r requirements.txt
python server.py
```

기본 주소:

```text
http://localhost:8000
```

---

## 9. 테스트 및 배포

주요 회귀 테스트는 자연어 Intent, Action/Task 전환, 복합 Workflow, 추천 Profile 카드 State, 조건 없는 탐색, 자격 확인 흐름과 정책 데이터 정합성을 검증합니다.

```bash
pytest
```

`main` 브랜치에 병합되면 Render의 Auto-Deploy를 통해 서비스에 반영됩니다.

---

## 10. 설계 원칙

두물청은 다음 원칙을 기준으로 구현했습니다.

- **자연어 의미 판단과 코드 검증을 분리**한다.
- **LLM이 정책 ID나 자격 결과를 임의로 확정하지 않도록** 공식 데이터와 규칙으로 검증한다.
- 버튼과 자연어를 가능한 한 **동일한 Action/Task 및 UI 흐름**으로 처리한다.
- 이전 대화 Context는 필요할 때만 재사용하고, **현재 사용자가 명시한 정책·분야를 우선**한다.
- Profile은 세션 공통 상태로 유지하되, `조건 없이`는 Profile 삭제가 아닌 **한 번의 추천 실행 옵션**으로 처리한다.
- 정보가 부족한 경우 추측하지 않고 Profile 또는 추가 질문을 통해 확인한다.

---

## Status

현재 버전은 남양주시 청년정책을 대상으로 **정책 설명 → 맞춤 추천 → 자격 확인**까지 하나의 대화 흐름으로 연결한 웹 기반 AI 상담 프로토타입입니다.
