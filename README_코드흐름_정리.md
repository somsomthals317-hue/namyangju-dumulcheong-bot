# 두물청 — 남양주시 청년정책 AI 상담 서비스

두물청은 남양주시 청년정책을 자연어와 버튼으로 검색하고, 관심 분야에 맞는 정책을 추천받고, 특정 정책의 신청 자격을 확인하는 FastAPI 기반 웹 서비스입니다.

이 문서는 수정 이력이 아니라 현재 운영 코드가 어떤 원리로 입력을 해석하고, 상태를 바꾸고, 정책 근거를 찾고, 답변을 만드는지 설명하는 최종 설계 문서입니다.

- 웹 서버: FastAPI
- 화면: HTML/CSS/JavaScript
- 정책 설명 범위: 32개
- 신청 자격 판정 가능 정책: 25개
- 정보·시설 안내 전용 정책: 7개
- 기본 실행 주소: `http://localhost:8000`

> 자격 결과는 안내용 가능성 판정입니다. 실제 선정·지급 여부는 최신 공식 공고와 담당 부서 심사가 기준입니다.

---

## 1. 서비스가 제공하는 세 가지 작업

| 작업 | 사용자 예시 | 결과 |
|---|---|---|
| EXPLAIN | “청년기본소득 설명해줘” | 대상, 혜택, 기간, 신청 방법, 서류, 공식 링크 |
| RECOMMEND | “창업 정책 추천해줘” | 관심 분야 및 프로필에 맞는 정책 목록과 추천 이유 |
| ELIGIBILITY | “말산업 인턴 내가 신청할 수 있어?” | 규칙·추가 답변·AI 보조 검토를 합친 PASS/FAIL/UNKNOWN |

한 문장에 두 가지 이상이 들어오면 하나를 버리지 않고 Workflow로 처리합니다.

예:

- “청년기본소득 설명하고 자격도 확인해줘”
- “농업 정책 추천해주고 그중 신청 가능한 것도 보고 싶어”
- “월세 정책은 설명해주고 기본소득은 자격 확인해줘”

---

## 2. 전체 처리 구조

```text
브라우저의 자연어 또는 버튼
        ↓
POST /api/chat
        ↓
의미 단위별 Action + Task 정규화
        ↓
각 단위가 참조할 policy/topic 선고정
        ↓
단일 또는 복합 Workflow 생성
        ↓
Action에 따른 State Transition
        ↓
EXPLAIN / RECOMMEND / ELIGIBILITY 실행
        ↓
정보가 부족하면 Clarify 카드에서 일시 정지
        ↓
사용자 답변 저장 후 같은 Workflow 지점부터 재개
        ↓
공식 링크를 포함한 최종 답변 반환
```

핵심은 사용자의 문장을 바로 RAG 검색에 넣지 않는 것입니다. Action은 이전 대화와의 관계만, Task는 실제 수행 기능만 표현합니다. 멀티쿼리에서는 모든 단위의 대상을 먼저 고정한 뒤 State를 바꾸므로, 첫 번째 step의 상태 변경이 뒤 step의 정책 대상을 지우지 않습니다.

### GPT가 개입하는 위치

GPT는 자유롭게 정책을 고르는 단일 엔진이 아니라, 코드가 정한 범위 안에서
의미를 보조하는 세 지점에만 개입합니다.

| 위치 | GPT 역할 | 코드 안전장치 |
|---|---|---|
| Prompt A | 규칙만으로 애매한 자연어의 Action·Task·대상·Workflow를 JSON으로 구조화 | 버튼과 명확한 정책명·후속 표현은 GPT를 건너뜀. 복합 발화는 문장에 실제 존재하는 Task 동사와 다시 교차검증하고, 허용 Schema 재검증 실패 시 `CLARIFY` |
| Prompt C | 코드가 만든 추천 후보들의 의미 적합성 판정과 순위화 | 공식 후보 ID만 허용, 명확한 자격 FAIL 사전 제외, 불완전 JSON은 규칙 순위로 복구 |
| Prompt D | 코드 규칙 결과와 실제 입력을 바탕으로 자격 보조 검토 | 코드 FAIL은 GPT 미호출, PASS/UNKNOWN 충돌은 보수적으로 UNKNOWN |

EXPLAIN 본문과 멀티쿼리 최종 결합은 GPT 자유 생성에 맡기지 않습니다.
공식 원문·Summary를 정해진 형식으로 출력하고, 여러 결과도 코드가 Task별
제목 아래 결합합니다. 따라서 GPT 장애가 설명 내용이나 이미 계산된 결과를
다른 정책으로 바꾸지 않습니다.

---

## 3. Action과 Task의 분리

```text
Action = 현재 발화와 이전 대화의 관계
Task   = 사용자가 현재 원하는 실제 결과
Atomic Intent Unit = Action 1개 + Task 1개 + 대상
```

Action과 Task는 1:1 관계가 아닙니다. `FOLLOW_UP`과 `SHOW_ALTERNATIVES`에는 세 Task가 모두 올 수 있고, `CHANGE_TOPIC`만 추천 관심 분야 변경 전용이라 `RECOMMEND`와 연결됩니다.

### Action Schema

| Action | 의미 | 대표 문장 |
|---|---|---|
| `NORMAL` | 이전 정책·결과를 특별히 참조하지 않는 독립 요청 | “청년월세 알려줘” |
| `FOLLOW_UP` | 직전 정책 또는 추천 결과를 명시적으로 이어서 참조 | “나도 돼?”, “이거랑 비슷한 정책 추천해줘” |
| `SHOW_ALTERNATIVES` | 직전 대상을 거부·제외하고 다른 대상을 요청 | “그거 말고 다른 거”, “그거 말고 청년기본소득 설명해줘” |
| `CHANGE_TOPIC` | 추천 관심 분야를 새 분야로 교체 | “농업 말고 취업 정책” |
| `RESET` | 대화 작업 상태 초기화 | “처음부터 다시 할래” |
| `CLARIFY` | Action·Task·요청 의미를 신뢰성 있게 해석하지 못함 | 의미 없는 문장, 충돌 Schema, 파싱 실패 |

`RUN_WORKFLOW`는 `NORMAL`, `FOLLOW_UP` 같은 **원자 Action이 아닙니다**. 두 개
이상의 Task가 감지됐다는 실행 오케스트레이션 이름이며, 실제 저장 시에는
`active_workflow.steps` 안에 `Action 1개 + Task 1개 + 대상` 형태로 나뉩니다.
예를 들어 설명+추천은 `RUN_WORKFLOW` 한 단계로 뭉개지 않고
`NORMAL+EXPLAIN`, `NORMAL+RECOMMEND` 두 step으로 실행됩니다. 과거 화면에서
보낸 `RUN_WORKFLOW` payload는 하위 호환 alias로만 받습니다.

### Task Schema

| Task | 판단 기준 | 대표 문장 |
|---|---|---|
| `EXPLAIN` | 특정 정책의 내용·대상·혜택·기간·방법·서류 설명 | “설명해줘”, “신청 기간은?” |
| `RECOMMEND` | 정책 후보·선택지·맞춤 추천 제공 | “추천해줘”, “다른 정책 없어?” |
| `ELIGIBILITY` | 특정 정책의 실제 신청 가능 여부 확인 | “나도 돼?”, “자격 확인해줘” |

대표 조합:

| Action | 가능한 Task | 실제 처리 |
|---|---|---|
| `NORMAL` | 세 Task 모두 | 새 정책 설명, 새 추천, 새 정책 자격 확인 |
| `FOLLOW_UP` | 세 Task 모두 | 직전 정책 상세 설명, 같은 정책 자격 확인, 비슷한 정책 추천 |
| `SHOW_ALTERNATIVES` | 세 Task 모두 | 다른 정책 카드, 새로 지정한 정책 설명·자격 |
| `CHANGE_TOPIC` | `RECOMMEND`만 | 기존 분야 제거 후 새 분야 정책 카드 |
| `RESET` | 없음 | 작업 상태 초기화 |
| `CLARIFY` | 없음 | 정책 실행 중단 후 기능 선택 안내 |

### 자연어 처리 원리

1. 코드가 확실하게 판단할 수 있는 전환·후속 표현을 먼저 확인합니다.
2. 확실하지 않거나 여러 작업·대상이 섞인 문장은 Prompt A가 JSON으로 분석합니다.
3. 복합 문장은 코드가 실제 작업 동사와 순서를 다시 추출하여 GPT의 `tasks`·`workflow`와 대조합니다.
4. GPT가 설명+추천 중 하나를 누락해도 코드가 누락 Task와 해당 정책/분야 대상을 복구합니다. 현재 문장에 명시된 공식 정책과 관심 분야는 과거 Context보다 우선합니다.
5. 완성된 Action, Task, 관심 분야, 정책명, Workflow step을 허용값 기준으로 다시 검증합니다.
6. 검증된 Atomic Action만 State Transition에 전달합니다.
7. Task는 분명하지만 정책·분야 정보만 부족하면 정책/관심분야 Clarify 카드로 이동합니다.
8. Action·Task 자체를 이해하지 못했거나 Schema가 충돌하면 `CLARIFY`로 실행을 중단합니다.

Prompt A를 Intent용과 Action용으로 두 번 호출하지 않습니다. 한 번의 구조화 응답에서 다음 정보를 함께 받습니다.

```json
{
  "action": "CHANGE_TOPIC",
  "tasks": ["RECOMMEND"],
  "topic": "취업",
  "exclude_topics": ["농업"],
  "explore_without_profile": false,
  "policy_mention": null,
  "follow_up_field": null,
  "workflow": [
    {
      "action": "CHANGE_TOPIC",
      "task": "RECOMMEND",
      "policy_mention": null,
      "topic": "취업",
      "exclude_policy_mentions": [],
      "explore_without_profile": false
    }
  ]
}
```

### 버튼 처리 원리

버튼은 자연어를 흉내 낸 문장을 만들어 별도 분기로 보내지 않습니다. JavaScript의 `dispatchAction()`이 동일한 Action Schema를 만들어 `/api/chat`으로 전달합니다.

```text
“다른 정책 없어?” 자연어 ─┐
                            ├→ SHOW_ALTERNATIVES + RECOMMEND → 공통 Handler
[다른 정책 보기] 버튼 ──────┘
```

따라서 자연어와 버튼의 차이는 Action을 만드는 주체뿐입니다.

- 자연어: 코드 규칙 또는 Prompt A가 Action 생성
- 버튼: Frontend가 Action 직접 생성
- 이후 State Transition, 검색, 답변 생성 과정은 동일

`조건 없이 찾아보기`도 별도 예외 분기로 처리하지 않습니다. 버튼은
`NORMAL + RECOMMEND + explore_without_profile: true`를 전송하고, 자연어의
“조건 없이”, “프로필 없이”, “자격 판정 없이”도 같은 필드로 정규화됩니다.
State Transition이 일어나도 이 필드가 Workflow step에 보존되므로 과거
Profile 때문에 정책이 사라지거나 Profile 카드가 다시 열리지 않습니다.

---

## 4. State와 주제 전환 원리

세션마다 다음 상태를 보관합니다.

| State | 역할 |
|---|---|
| `profile` | 나이, 거주, 취업, 학생, 창업, 주택, 혼인 정보 |
| `current_topic` | 현재 보고 있는 관심 분야 |
| `current_policy_id` | 후속 질문이 참조할 수 있는 단일 정책 |
| `focus_policy_id` | 현재 설명·자격 처리 대상 정책 |
| `last_result_policy_ids` | 직전 추천 목록의 정책 ID |
| `last_recommendation_topic` | 정책 설명 화면으로 이동해도 보존되는 마지막 추천 분야 |
| `last_recommendation_policy_ids` | 설명 후 “다른 거” 전환에 쓰는 마지막 추천 묶음 |
| `active_clarify` | 현재 기다리는 입력 종류 |
| `active_workflow` | 복합 요청의 step, 현재 위치, 완료 결과 |
| `policy_answers` | 정책별 추가 질문 답변 |
| `last_action` | 직전에 처리한 Action |
| `last_task`, `last_tasks` | 직전 단일·복합 실행 Task |
| `last_intent_failure` | 자연어 해석 실패 원인 코드 |

Action별 State 원칙은 다음과 같습니다.

- `NORMAL`: Profile만 유지하고, 현재 발화의 새 정책·분야가 과거 policy/topic보다 우선합니다.
- `FOLLOW_UP`: 명시적으로 참조한 직전 policy 또는 추천 결과만 유지합니다. Task는 현재 문장에서 다시 판단합니다.
- `SHOW_ALTERNATIVES`: 직전 정책·분야를 제외 조건으로 바꾸고, 새 정책명이 있으면 새 대상을 최우선으로 설정합니다. “다른 분야”는 마지막 추천 분야 전체를 제외하고 `전체`에서 새 카드를 찾습니다.
- `CHANGE_TOPIC`: 이전 topic과 policy를 지우고 새 추천 분야로 교체합니다. `RECOMMEND` 이외의 Task와 결합되면 실행하지 않습니다.
- `RESET`: 진행 중 카드, 후보, Workflow, 현재 정책·분야를 초기화합니다.
- `CLARIFY`: 현재 policy/topic은 보존하지만 stale 카드와 Workflow는 닫고 안전 메뉴를 표시합니다.

“다른 정책 자격을 확인하고 싶어”처럼 새 정책명이 없는 자격 전환은 직전 정책을 다시 판정하지 않습니다. `CLARIFY_POLICY`로 이동하여 사용자가 정책을 선택하게 합니다.

---

## 5. 정책 데이터가 결합되는 원리

### 데이터 계층

| 데이터 | 역할 |
|---|---|
| `policy_origin_extracted/json/` | 공식 정책 원문 32개 |
| `summary_documents_with_policy_id.json` | 설명·추천에 쓰는 구조화 요약 |
| `policy_eligibility_rules.json` | 자격 판정 규칙과 추가 질문 |
| `policy_origin.zip` | 원문 복구·감사용 백업 |
| Policy Bundle | Summary와 Rules를 `policy_id`로 결합한 런타임 객체 |

`data_loader.build_policy_bundles()`가 같은 `policy_id`를 가진 Summary와 Rules를 결합합니다.

```python
{
    "policy_id": "NYJ-YOUTH-010",
    "policy_name": "청년기본소득",
    "summary": "...",
    "main_target": "...",
    "benefit": "...",
    "application_period": "...",
    "recommendation_interests": ["기본소득"],
    "basic_condition": {
        "age": "만 24세",
        "residency": "남양주시",
        "income": "해당없음",
        "employment": "해당없음",
        "student": "해당없음",
        "startup": "해당없음",
        "housing": "해당없음",
        "marriage": "해당없음"
    },
    "additional_questions": [],
    "eligibility_mode": "FULL",
    "source": "공식 URL"
}
```

### 조건 필드 원칙

모든 정책의 조건 객체는 다음 8개 키를 동일하게 가집니다.

- age
- residency
- income
- employment
- student
- startup
- housing
- marriage

실제 조건이 없으면 키를 생략하지 않고 `해당없음`을 사용합니다. 사용자 답변이 필요한 복잡한 조건은 `additional_questions`에 둡니다. 이 구조 덕분에 코드와 GPT가 누락된 필드를 서로 다르게 해석하는 문제를 줄입니다.

정보·시설 안내 7개는 `eligibility_mode: INFO_ONLY`입니다. 설명과 추천에는 등장할 수 있지만 PASS/FAIL 신청 자격을 임의로 판정하지 않습니다.

---

## 6. EXPLAIN 원리

```text
정책명·별칭 확인
→ 정확한 policy_id가 있으면 바로 원문 조회
→ 불명확하면 Vector 검색
→ 정책명·요약·혜택에 실제 질의 근거가 있는지 재검증
→ 공식 원문과 Summary로 설명 포맷 생성
→ 공식 정책 링크 추가
```

EXPLAIN은 유사도 1위라는 이유만으로 정책을 선택하지 않습니다. 검색 후보의 정책명과 공식 요약에 사용자의 핵심어가 실제로 존재하는지 다시 확인합니다.

예를 들어 “정장 대여”가 입영지원금과 임베딩상 가깝더라도 공식 이름·혜택에 직접 근거가 없으면 설명하지 않습니다.

최종 설명 항목:

- 지원대상
- 지원내용
- 신청기간
- 신청방법
- 제출서류
- 공식 정책 페이지

직전 단일 정책의 “신청 기간”, “신청 방법”, “필요 서류” 질문은 새 RAG 검색을 하지 않고 현재 Policy Bundle의 해당 필드를 직접 답합니다.

---

## 7. RECOMMEND 원리

```text
자연어 관심 분야 정규화
→ 전체 Bundle에서 분야 관련 후보 계산
→ 코드로 명확한 자격 FAIL 후보 제외
→ GPT가 남은 후보의 의미 관련성 판단·순위화
→ 정책 ID와 공식 근거를 코드가 재검증
→ PASS와 UNKNOWN을 구분해 표시
→ GPT 오류 시 규칙 후보로 복구
```

### 관심 분야

허용 분야는 다음과 같습니다.

`취업, 주거, 창업, 교육, 복지, 참여·문화, 기본소득, 농업, 전체`

생활어와 동의어는 표준 분야로 바꿉니다.

- 구직, 면접, 정장 → 취업
- 월세, 보증금 → 주거
- 영농, 농어, 말산업 → 농업
- 기본 소득 → 기본소득

창업 추천은 단순히 본문에 “창업”이라는 글자가 있다고 포함하지 않습니다. `미창업`, `비창업`, `창업 외`처럼 부정·제외 조건에만 등장한 정책은 창업 분야로 보지 않습니다.

### 맞춤 추천과 조건 없는 탐색

- 맞춤 추천: 저장 Profile로 명확한 FAIL을 제외합니다.
- 조건 없이 탐색: 개인 자격을 판정하지 않고 분야 관련 정책 개요를 보여줍니다.
- 두 모드 모두 GPT가 후보의 의미 적합성을 검토할 수 있지만 공식 후보 밖의 정책 ID를 만들 수 없습니다.
- 조건 없이 탐색에서는 Profile 값이 `None`인 항목을 FAIL로 해석하지 않습니다. 실제로 입력된 값이 공식 조건과 충돌할 때만 맞춤 추천에서 명확한 FAIL로 제외합니다.
- 정보·시설 안내 7개는 추천 카드에는 등장할 수 있지만 자격 확인 버튼은 표시하지 않습니다. 자연어로 자격을 물으면 PASS/FAIL 대신 모집공고·예약 페이지 확인 안내를 제공합니다.

`SHOW_ALTERNATIVES`에서는 이전 정책 ID나 분야를 제외한 후 다시 추천합니다. 의미 재정렬에는 “이거 말고”라는 지시문 대신 전환 후 관심 분야를 전달하고, GPT가 일반어 `전체`에서 후보를 하나도 선택하지 않아도 검증된 비-FAIL 정책에서 제외 조건을 적용해 다른 분야 카드를 복구합니다. GPT가 제외 대상을 다시 상위에 올려도 코드가 최종 제외 조건을 한 번 더 적용합니다.

---

## 8. ELIGIBILITY 원리

```text
정책 대상 확정
→ INFO_ONLY 여부 확인
→ 필요한 Profile 필드만 수집
→ basic_condition 코드 판정
→ 명확한 FAIL이면 즉시 종료
→ 정책별 additional_questions 수집
→ 규칙 결과 생성
→ GPT 보조 검토
→ 보수적 병합
→ PASS / FAIL / UNKNOWN과 공식 링크 표시
```

### 코드가 먼저 판정하는 이유

나이 범위, 남양주시 거주, 미취업, 대학생, 무주택, 미혼, 창업 상태처럼 구조화된 조건은 코드가 비교합니다. 명확한 불충족은 GPT가 뒤집을 수 없습니다.

### 추가 질문

소득 구간, 영농 경력, 부모와 별도 거주, 모집공고상 세부 요건처럼 공통 Profile만으로 알 수 없는 조건은 정책별 질문으로 받습니다.

- 한 화면에 최대 3개
- 질문 ID 기준으로 정책별 저장
- 답변 후 같은 ELIGIBILITY step 재실행
- 최종 결과에서 추가 답변 재입력 가능

### GPT 보조 판정

GPT는 다음 자료만 받습니다.

- 현재 Policy Bundle
- 입력된 Profile
- 저장된 추가 질문 답변
- 코드 규칙 결과

새 조건이나 사용자 정보를 추측할 수 없습니다.

### 최종 병합 원칙

| 코드 결과 | GPT 결과 | 최종 결과 |
|---|---|---|
| FAIL | 호출하지 않음 | FAIL |
| PASS | PASS | PASS |
| PASS | UNKNOWN | UNKNOWN |
| PASS | FAIL | UNKNOWN |
| UNKNOWN | PASS | UNKNOWN |
| UNKNOWN | UNKNOWN | UNKNOWN |
| UNKNOWN | FAIL | UNKNOWN |

코드 FAIL은 확정적으로 유지합니다. 코드와 GPT 중 하나라도 불확실하거나 서로 충돌하면 안전하게 UNKNOWN으로 둡니다.

---

## 9. 멀티쿼리와 Workflow 원리

`active_workflow`는 다음 정보를 가집니다.

```python
{
    "steps": [
        {"action": "NORMAL", "task": "EXPLAIN", "policy_mention": "청년월세", "topic": None},
        {"action": "NORMAL", "task": "ELIGIBILITY", "policy_mention": "청년기본소득", "topic": None}
    ],
    "index": 0,
    "results": {},
    "original_query": "월세는 설명하고 기본소득은 자격 확인해줘"
}
```

### Resolve → Transition → Execute → Pause → Resume

1. Prompt A가 문장을 의미 단위로 나눕니다.
2. 코드가 문장의 설명·추천·자격 동사와 순서를 독립적으로 추출하여 Prompt A 결과의 누락 여부를 검사합니다.
3. GPT step과 코드 step을 Task별로 병합합니다. 현재 문장에 명시된 공식 정책명과 관심 분야는 이전 State에서 가져온 대상보다 우선합니다.
4. 각 step의 `policy_id`, `topic`, 제외 정책을 현재 State가 바뀌기 전에 resolve합니다.
5. 완성된 Workflow를 저장합니다.
6. 현재 step의 Action으로 State를 유지·제외·교체합니다.
7. Task를 실행합니다.
8. 정보가 부족하면 현재 `index`에서 멈춥니다.
9. 카드 답변을 저장한 뒤 같은 step부터 재개합니다. 완료된 앞 step은 다시 실행하지 않습니다.

예를 들어 “이 정책 설명해주고 다른 건 없어?”는 다음 두 단위입니다.

```text
FOLLOW_UP + EXPLAIN           (직전 정책 ID를 먼저 고정)
SHOW_ALTERNATIVES + RECOMMEND (직전 정책 ID를 제외 대상으로 먼저 고정)
```

두 번째 Action이 policy State를 초기화하더라도 첫 번째 설명 대상과 제외 ID는 이미 Workflow에 저장되어 사라지지 않습니다.

### 설명 + 추천

예: “안녕 나 입영지원금 설명해주고, 복지 분야에서 정책 하나 추천해줘”

1. Prompt A가 EXPLAIN의 정책 대상과 RECOMMEND의 관심 분야를 서로 다른 Workflow step으로 분리합니다.
2. GPT가 추천 Task만 반환하더라도 코드 완전성 검사가 문장 속 “설명”을 찾아 `EXPLAIN(입영지원금)`을 복구합니다.
3. EXPLAIN은 지정된 정책 원문과 Summary를 사용해 설명 결과를 먼저 만들고 내부 `results`에 저장합니다.
4. RECOMMEND는 설명 정책을 추천 분야로 재사용하지 않고, 자기 step의 `topic=복지`와 Profile을 사용합니다. GPT 의미 정렬에도 전체 복합문장이 아닌 `복지`만 전달하여 앞 step의 `농업`이 추천 결과로 역류하지 않게 합니다.
5. 추천에 관심 분야나 Profile이 부족하면 Workflow를 RECOMMEND 위치에서 멈추고 필요한 카드만 표시합니다. 화면에는 설명이 준비됐으며 추천 입력 뒤 두 결과를 묶겠다는 안내를 표시합니다.
6. 입력이 완료되면 같은 RECOMMEND step부터 재개하고, 마지막에 `[정책 설명]`과 `[맞춤 추천 결과]`를 사용자 요청 순서로 함께 반환합니다.
7. 각 블록 앞에는 “먼저 입영지원금 지원에 대해 설명해드릴게요”, “이어서 복지 분야에서 정책을 추천해드릴게요”처럼 실제 대상을 확인하는 문장을 표시합니다.

```text
EXPLAIN(입영지원금 지원)
→ 설명 결과 임시 저장
→ RECOMMEND(복지)
→ 필요하면 관심 분야/Profile 질문 후 재개
→ 입영지원금 설명 + 복지 맞춤 추천 최종 결합
```

“농업 정책 설명해주고 복지 분야에서 추천해줘”처럼 첫 대상이 정확한 정책명이
아니라 분야라면 토큰이 우연히 일치하는 한 정책을 고르지 않습니다. 농업 관련
정책 선택 카드와 “어떤 정책을 알고 싶으신가요?”를 먼저 표시하고, 복지 추천
step은 `active_workflow`에 그대로 보존합니다. 사용자가 정책을 선택하면 설명을
내부 `results`에 저장한 뒤 “정책 설명은 준비해두었어요”라고 알리고 Profile
카드를 표시합니다. 입력이 끝나야 정책 설명과 복지 추천 카드를 한 답변으로
합쳐 반환합니다.

### 설명 + 자격

1. 설명 결과를 내부 `results`에 저장합니다.
2. 자격에 Profile이나 추가 답변이 필요하면 Workflow를 멈춥니다.
3. 이때 저장된 설명을 먼저 노출하지 않습니다.
4. 자격 질문이 끝나면 같은 ELIGIBILITY step을 재실행합니다.
5. 마지막에 정책 설명과 자격 결과를 한 번에 결합합니다.

### 추천 + 자격

1. 추천 결과와 정책 ID 목록을 저장합니다.
2. 자격 대상이 문장에 명시되어 있으면 해당 정책으로 진행합니다.
3. 대상이 없으면 추천 1위를 임의로 사용하지 않습니다.
4. 추천 후보를 보여주고 `CLARIFY_POLICY` 선택 카드를 표시합니다.
5. 사용자가 선택한 정책으로 자격 확인을 이어갑니다.

### 서로 다른 정책 대상

각 step에 `policy_mention`을 따로 저장하므로 “월세는 설명, 기본소득은 자격” 요청에서 두 정책이 섞이지 않습니다.

---

## 10. Clarify와 카드 원리

`CLARIFY` Action과 아래 정보수집 Clarify는 서로 다릅니다.

- `CLARIFY` Action: 자연어 의미, Task 또는 Action+Task 조합을 신뢰성 있게 판단하지 못한 실패 경로입니다. 정책 실행과 RAG를 중단합니다.
- `CLARIFY_*` State: Task는 확정됐지만 정책명·관심 분야·Profile·추가 조건이 부족하여 사용자 입력을 기다리는 정상 경로입니다.

해석 실패 응답:

> 말씀하신 내용을 정책 요청으로 정확히 이해하지 못했어요. 정책명과 궁금한 내용(설명·추천·자격)을 함께 적거나, 아래에서 원하는 기능을 선택해주세요. 무엇을 도와드릴까요?

이 응답에는 정책 알아보기, 맞춤 추천받기, 자격 확인하기 버튼이 함께 표시됩니다. 실패 입력으로 현재 policy/topic을 바꾸지 않지만, 오래된 카드와 Workflow는 닫아 다음 입력이 과거 질문의 답으로 잘못 저장되지 않게 합니다.

| Clarify | 발생 조건 | 화면 |
|---|---|---|
| `CLARIFY_POLICY` | 설명·자격 정책이 불명확 | 정책 선택 카드 |
| `CLARIFY_PREFERENCE` | 추천 분야가 없음 | 관심 분야 카드 |
| `CLARIFY_PROFILE` | 추천·자격에 필요한 Profile 부족 | Profile 카드 |
| `CLARIFY_ADDITIONAL` | 정책별 세부 조건 미확인 | 추가 질문 카드 |

카드가 떠 있어도 사용자가 “이거 말고 다른 정책”처럼 새 작업을 말하면 카드 답변으로 저장하지 않습니다. 현재 발화를 다시 Action 분석하여 기존 Workflow를 취소하거나 새 주제로 전환합니다.

---

## 11. Frontend와 FastAPI 연결

### 주요 엔드포인트

| Method | Path | 역할 |
|---|---|---|
| GET | `/` | 상담 화면 |
| GET | `/landing` | 서비스 소개 화면 |
| GET | `/chat` | 상담 화면 |
| GET | `/api/policies` | 전체 정책과 자격 가능 정책 목록 |
| POST | `/api/chat` | 자연어·버튼 Action·Profile·추가 답변 처리 |
| POST | `/api/reset` | 세션 초기화 |
| GET | `/healthz` | 서버 상태 확인 |

`POST /api/chat`의 버튼 요청 예:

```json
{
  "session_id": "browser-session-id",
  "message": "청년기본소득 자격 확인해줘",
  "action": {
    "action": "NORMAL",
    "tasks": ["ELIGIBILITY"],
    "policy_id": "NYJ-YOUTH-010",
    "policy_mention": "청년기본소득",
    "use_previous_context": false,
    "confidence": "high"
  }
}
```

FastAPI는 세션별 잠금으로 같은 사용자의 중복 요청을 직렬화하고, 동기 AI 작업은 Thread Pool에서 실행합니다. 세션에는 TTL과 최대 개수 제한이 있습니다.

---

## 12. 최종 검증 범위와 결과

최종 검증은 외부 API 응답의 우연한 문장 차이가 아니라
`Action → Task → State → 정책 ID → 사용자 응답` 계약을 검사합니다. GPT가
응답하지 않는 경우의 규칙 복구도 포함하며, GPT JSON 계약과 보수적 병합은
별도 단위 테스트로 검증합니다.

### 32개 정책 전수 매트릭스

| 검증 묶음 | 시나리오 수 | 확인 내용 | 결과 |
|---|---:|---|---|
| 자연어 정책 설명 | 32 | 정확한 policy_id, 정책명, 공식 링크 | PASS |
| 설명 버튼 | 32 | `NORMAL + EXPLAIN`, 자연어와 같은 실행기 | PASS |
| 자연어 자격 확인 | 25 | 신청형 정책 선택, 설명 혼입 방지, 링크 | PASS |
| 자격 확인 버튼 | 25 | `NORMAL + ELIGIBILITY`, 정확한 정책 유지 | PASS |
| 정보·시설 자격 질문 | 7 | PASS/FAIL 금지, INFO_ONLY 안내 | PASS |
| 직전 정책 기반 자연어 추천 | 32 | `FOLLOW_UP + RECOMMEND`, 공식 관심 분야 seed | PASS |
| 추천 분야·조건 없는 버튼 | 9 | 표준 분야, 탐색 모드, 정책 카드 생성 | PASS |
| 추천 카드 자격 버튼 계약 | 32 | 신청형 25개만 버튼 허용, INFO_ONLY 7개 차단 | PASS |
| 주제 전환·대안·후속 표현 | 6 | Action과 현재 발화 우선순위 | PASS |
| 해석 불가 입력 | 5 | CLARIFY 안전 메뉴와 stale 카드 종료 | PASS |
| 3단계 멀티쿼리 | 1 | 대상 선결정, 제외 정책, Task 순서 | PASS |
| 카탈로그 분할 | 1 | 설명 32 / 자격 25 / INFO_ONLY 7 | PASS |
| 연속 대화·복합 Task·탐색 모드 복구 | 12 | GPT Task 누락 복구, 분야형 설명 정책 선택 → Profile/조건 없이 버튼 → 합본 응답, 추천 후 다른 분야 전환, 역순 Task, 대안 목록 전체 제외 | PASS |

세부 정책·대화 시나리오는 **219/219 PASS**, 전체 회귀 테스트는
**58/58 PASS**입니다.

### 데이터·링크 감사

`audit_policy_catalog.py`가 원본과 운영 데이터를 전수 비교합니다.

| 감사 대상 | 결과 |
|---|---|
| `policy_origin.zip` 원본 | 32개 |
| 추출 원문 | 32개 |
| Summary | 32개 |
| Eligibility Rules | 32개 |
| 정책별 추가 질문 | 56개 |
| 구조·ID·조건·공식 링크 오류 | 0건 |
| 원문 수동 재확인 경고 | 0건 |

감사 결과는 `policy_full_coverage_matrix.md`와
`policy_data_audit.json`에 정책별로 기록됩니다.

---

## 13. 파일 역할

```text
agent.py
  Action 분석, State Transition, Workflow,
  EXPLAIN / RECOMMEND / ELIGIBILITY 실행

prompts.py
  Prompt A Intent·Action JSON,
  추천 의미 판정, 자격 보조 판정 Prompt

state.py
  세션 기본값, Profile, 정책별 답변,
  task Context 초기화

data_loader.py
  원문·Summary·Rules 로드,
  policy_id 검증과 Bundle 생성

vector_store.py
  OpenAI Embedding과 ChromaDB 검색

server.py
  FastAPI 엔드포인트, 세션 잠금,
  Action·Profile·추가 답변 전달

static/index.html
  화면, 카드, 공통 dispatchAction,
  API 응답 렌더링

test_action_workflow.py
  자연어·버튼·멀티쿼리 대표 10개 테스트

test_action_task_backbone.py
  Action/Task 독립, 해석 실패 CLARIFY,
  대상 선고정과 새 정책 우선 회귀 테스트

test_policy_agent_matrix.py
  32개 정책 설명·추천·자격 자연어/버튼 전수 매트릭스,
  INFO_ONLY·전환·멀티쿼리·오입력 검증

test_conversation_workflows.py
  GPT Task 누락 재현과 코드 복구,
  설명+추천·추천+자격 순서, 동일 문장 FOLLOW_UP,
  "이거 말고 다른 거" 결과 묶음 제외 검증

audit_policy_catalog.py
  원본 ZIP·추출본·Summary·Rules·공식 링크 전수 감사
```

---

## 14. 설치와 실행

### 환경 변수

`.env`:

```dotenv
OPENAI_API_KEY=본인의_OpenAI_API_Key
SESSION_TTL_SECONDS=7200
MAX_SESSIONS=1000
MAX_CONCURRENT_AGENT_CALLS=8
```

API 키는 GitHub에 올리지 않습니다.

### 설치

```bash
python -m pip install -r requirements.txt
```

### 실행

```bash
python server.py
```

브라우저:

- 상담 서비스: `http://localhost:8000/`
- 소개 페이지: `http://localhost:8000/landing`
- 상태 확인: `http://localhost:8000/healthz`

### 핵심 테스트

```bash
python -m unittest test_action_workflow.py -v
```

전체 CI 대상:

```bash
python -m unittest test_intent_regression.py test_hardening.py test_action_workflow.py test_action_task_backbone.py test_policy_agent_matrix.py test_conversation_workflows.py -v
```

정책 데이터 감사:

```bash
python audit_policy_catalog.py
```

---

## 15. 운영 시 알아둘 한계

- Render 인스턴스가 재시작되면 메모리 세션과 ChromaDB는 다시 생성됩니다.
- 실제 신청기간과 모집 상태는 바뀔 수 있으므로 공식 링크를 최종 기준으로 봐야 합니다.
- GPT 장애 시 추천은 규칙 후보로 복구하지만 의미 순위의 세밀함은 낮아질 수 있습니다.
- 사용자가 제공하지 않은 개인정보나 소득·경력 조건은 추측하지 않고 UNKNOWN 또는 추가 질문으로 처리합니다.
- 정책 데이터가 변경되면 원문, Summary, Rules의 `policy_id`와 조건을 함께 갱신해야 합니다.

---

## 16. 핵심 설계 원칙

두물청은 GPT가 모든 것을 자유롭게 결정하는 챗봇이 아닙니다.

```text
코드가 입력과 상태를 통제하고
정책 원문과 규칙이 사실의 범위를 정하며
GPT는 자연어 의미 분석과 후보 검토를 보조하고
불확실하거나 충돌하는 결과는 사용자 선택 또는 UNKNOWN으로 남긴다.
```

이 원칙이 자연어의 유연성과 정책 안내의 안정성을 함께 유지하는 기준입니다.
