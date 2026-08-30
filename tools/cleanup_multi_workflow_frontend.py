from pathlib import Path
import subprocess


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


def replace_in_block(text, start, end, old, new, label):
    start_i = text.index(start)
    end_i = text.index(end, start_i)
    block = text[start_i:end_i]
    new_block = replace_once(block, old, new, label)
    return text[:start_i] + new_block + text[end_i:]


# Restore exact main blob (including CRLF), then reapply only intended functional edits.
raw = subprocess.check_output(['git', 'show', 'origin/main:static/index.html'])
h = raw.decode('utf-8')
crlf = '\r\n' if '\r\n' in h else '\n'

def nl(s):
    return s.replace('\n', crlf)

h = replace_once(
    h,
    nl('let pcSelections = {};\n\nfunction makeChipRow'),
    nl('let pcSelections = {};\nlet resumeMultiWorkflowOnRecommendProfile = false;\n\nfunction makeChipRow'),
    'recommend rerun state flag',
)

submit_start = 'async function submitRecommendProfile(btn) {'
submit_end = 'function reopenRecommendProfile(btn) {'
h = replace_in_block(
    h,
    submit_start,
    submit_end,
    nl("                    explore_without_profile: false,\n                    confidence: 'high',\n"),
    nl("                    explore_without_profile: false,\n                    resume_multi_workflow: resumeMultiWorkflowOnRecommendProfile,\n                    confidence: 'high',\n"),
    'recommend submit resume flag',
)
h = replace_in_block(
    h,
    submit_start,
    submit_end,
    nl("        if (st.active_clarify === 'CLARIFY_PREFERENCE') {\n            addBotMessage(data.response);\n            showInterestCardsInChat();\n        } else {\n            addBotMessage(data.response);\n        }\n"),
    nl("        // 멀티쿼리는 추천 제출 뒤 ELIGIBILITY 등 다음 Clarify로 이어질 수 있으므로\n        // 말풍선만 그리지 말고 공통 State→카드 렌더러를 반드시 사용한다.\n        renderResponseWithCard(data);\n        resumeMultiWorkflowOnRecommendProfile = false;\n"),
    'render next workflow card after recommendation submit',
)

h = replace_once(
    h,
    nl("function reopenRecommendProfile(btn) {\n    if (btn) btn.disabled = true;\n    deactivateAllCards();\n"),
    nl("function reopenRecommendProfile(btn) {\n    if (btn) btn.disabled = true;\n    // 결과 카드에서 다시 설정한 경우에만 완료된 멀티쿼리 Atomic 계약을 재사용한다.\n    resumeMultiWorkflowOnRecommendProfile = true;\n    deactivateAllCards();\n"),
    'mark result profile rerun',
)

explore_start = 'async function exploreWithoutProfile(btn) {'
explore_end = 'function onInterestSelected(btn, interest) {'
h = replace_in_block(
    h,
    explore_start,
    explore_end,
    nl('        if (data.state && data.state.profile) serverProfile = data.state.profile;\n        addBotMessage(data.response);\n'),
    nl('        if (data.state && data.state.profile) serverProfile = data.state.profile;\n        renderResponseWithCard(data);\n'),
    'render next workflow card after conditionless submit',
)

Path('static/index.html').write_bytes(h.encode('utf-8'))
print('minimal frontend cleanup applied')
