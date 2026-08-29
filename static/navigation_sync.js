// 자연어로 요청한 UI 전환을 기존 버튼 UX와 동일한 화면 동작으로 수렴시킨다.
// 서버의 Prompt A/State가 의미를 확인한 뒤에만 기존 버튼 함수를 호출한다.
(() => {
    const originalRender = window.renderResponseWithCard;
    if (typeof originalRender !== 'function') return;

    function hasSpecificPolicyCandidates(st) {
        return Array.isArray(st.policy_candidates) && st.policy_candidates.length > 0;
    }

    function runQuickAction(fn, fallbackMessage) {
        if (typeof deactivateAllCards === 'function') deactivateAllCards();
        Promise.resolve(fn()).catch(() => addBotMessage(fallbackMessage));
    }

    // 기존 함수의 hard-coded explore_without_profile=true 버그를 우회한다.
    // 검증/표시 UI는 그대로 재사용하고, 최종 전송만 이미 올바른 공통 helper로 보낸다.
    window.submitRecommendProfile = async function(btn) {
        const card = btn.closest('.chat-card');
        const profile = {};
        if (pcSelections.age) profile.age = parseInt(pcSelections.age);
        ['residency','employment','student','startup','housing','marriage'].forEach(f => {
            if (pcSelections[f]) profile[f] = pcSelections[f];
        });

        const required = ['age','residency','employment','student','startup','housing','marriage'];
        const missing = required.filter(f => !profile[f]);
        if (missing.length > 0) {
            const labels = {
                age:'만 나이', residency:'남양주시 거주', employment:'고용 상태',
                student:'학생 여부', startup:'창업 여부', housing:'주택 보유', marriage:'결혼 여부'
            };
            showCardError(card, '다음 항목을 선택해주세요: ' + missing.map(f => labels[f]).join(', '));
            return;
        }

        card.querySelectorAll('button, input').forEach(el => el.disabled = true);
        card.style.opacity = '0.6';
        addUserMessage('이 조건으로 추천받기');
        await postProfileAndRecommend(profile, false);
    };

    window.renderResponseWithCard = function(data) {
        const command = data && data.ui_command;
        const st = (data && data.state) || {};
        if (st.profile) window.serverProfile = st.profile;

        if (command && command.type === 'RESET_PROFILE') {
            if (typeof deactivateAllCards === 'function') deactivateAllCards();
            if (command.policy_id) {
                lastEligibilityPolicyId = command.policy_id;
                profileResumePolicyId = command.policy_id;
            }
            if (Array.isArray(command.fields)) {
                lastEligibilityFields = [...command.fields];
                lastMissingFields = [...command.fields];
            }
            if (data.response) addBotMessage(data.response);
            showProfileCard(
                Array.isArray(command.fields) && command.fields.length > 0
                    ? command.fields
                    : undefined
            );
            return;
        }

        if (command && command.type === 'START_ELIGIBILITY') {
            runQuickAction(
                startEligibility,
                '자격 확인 정책 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        if (command && command.type === 'START_EXPLAIN') {
            runQuickAction(
                startExplain,
                '정책 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        if (command && command.type === 'START_RECOMMEND') {
            runQuickAction(
                startRecommend,
                '추천 시작 화면을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        if (command && command.type === 'RESET_CHAT') {
            const container = document.getElementById('chatMessages');
            if (container) container.innerHTML = '';
            pendingInterest = '';
            lastMissingFields = null;
            lastEligibilityPolicyId = null;
            lastEligibilityFields = [];
            profileResumePolicyId = null;
            window.serverProfile = st.profile || {};
            addBotMessage(data.response || '대화가 초기화되었어요. 아래 버튼을 눌러보세요!');
            return;
        }

        // 자연어 단일 메뉴 요청은 Prompt A가 만든 State를 보고 하단 버튼 UX로 수렴한다.
        // 정책명이 명확하거나 후보가 이미 좁혀진 경우에는 절대 메뉴 이동으로 바꾸지 않는다.
        const pending = Array.isArray(st.pending_tasks) ? st.pending_tasks : [];
        const noTopic = !(typeof st.interest_query === 'string' && st.interest_query.trim());

        if (
            st.active_clarify === 'CLARIFY_POLICY'
            && pending.length === 1
            && pending[0] === 'EXPLAIN'
            && noTopic
            && !hasSpecificPolicyCandidates(st)
        ) {
            runQuickAction(
                startExplain,
                '정책 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        if (
            st.active_clarify === 'CLARIFY_POLICY'
            && pending.length === 1
            && pending[0] === 'ELIGIBILITY'
            && noTopic
            && !hasSpecificPolicyCandidates(st)
        ) {
            runQuickAction(
                startEligibility,
                '자격 확인 정책 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        if (
            st.active_clarify === 'CLARIFY_PREFERENCE'
            && pending.length === 1
            && pending[0] === 'RECOMMEND'
            && noTopic
        ) {
            runQuickAction(
                startRecommend,
                '추천 시작 화면을 불러오지 못했어요. 잠시 후 다시 시도해주세요.'
            );
            return;
        }

        return originalRender(data);
    };
})();
