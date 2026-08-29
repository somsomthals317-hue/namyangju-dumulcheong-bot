from pathlib import Path

p = Path('README.md')
s = p.read_text(encoding='utf-8')
s = s.replace(
    '정책명·관심 분야가 없는 **일반 메뉴 이동 발화**는 Prompt A가 의미를 판정한 뒤 하단 버튼과 같은 UI로 수렴합니다.\n',
    '정책명·관심 분야가 없는 **단순 메뉴 명령(Bare Menu Command)** 은 Prompt A보다 먼저 코드에서 확정해 하단 버튼과 같은 UI로 수렴합니다. 이 계층은 문장 전체 exact-match만 사용하므로 `자격조회`와 `자격조회하자`는 항상 같은 결과가 나며, 정책명이 붙은 구체 요청은 가로채지 않습니다.\n'
)
s = s.replace(
    '"자격조회하자"       → [자격 확인하기]와 같은 정책 선택 카드\n',
    '"자격조회" / "자격조회하자" → [자격 확인하기]와 같은 정책 선택 카드\n'
)
s = s.replace(
    '1. 공식 정책 Bundle에서 관심 분야 후보 생성\n2. 일반 추천이면 사용자 Profile로 명확한 불충족 정책 제외\n3. LLM이 후보의 의미 적합성을 보조 판정\n4. 정책 ID와 공식 데이터를 다시 검증\n5. 추천 이유와 함께 정책 카드 반환\n',
    '1. 공식 정책 Bundle에서 관심 분야 후보 생성\n2. 선택한 관심 분야와 공식 `recommendation_interests`가 맞는 정책만 **hard gate**로 통과\n3. 일반 추천이면 사용자 Profile로 명확한 불충족 정책 제외\n4. LLM은 hard gate를 통과한 후보 안에서만 의미 적합성과 순위를 보조 판정\n5. 정책 ID와 공식 데이터를 다시 검증\n6. 추천 이유와 함께 정책 카드 반환\n'
)
s = s.replace(
    '주요 회귀 테스트는 자연어 Intent, Action/Task 전환, 복합 Workflow, 추천 Profile 카드 State, 조건 없는 탐색, 자격 확인 흐름과 정책 데이터 정합성을 검증합니다.\n',
    '주요 회귀 테스트는 자연어 Intent, Action/Task 전환, 복합 Workflow, 추천 Profile 카드 State, 조건 없는 탐색, 자격 확인 흐름과 정책 데이터 정합성을 검증합니다. 추가로 최종 라우팅 매트릭스에서 bare 메뉴 동의어, 진행 중 자격→새 추천 전환, 25개 자격 버튼 전수, 추천 분야 hard gate, 멀티 관심분야, 모호한 Profile 답변, 설명+자격 복합 요청을 고정 검증합니다.\n'
)
s = s.replace(
    '- **자연어 의미 판단과 코드 검증을 분리**한다.\n',
    '- **자연어 의미 판단과 코드 검증을 분리**한다. 단, 단순 메뉴 명령은 의미 추론 대상이 아니라 UI 명령으로 canonicalize한다.\n'
)
p.write_text(s, encoding='utf-8')
