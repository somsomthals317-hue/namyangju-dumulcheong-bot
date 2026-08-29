// 자연어로 요청한 UI 전환을 기존 버튼 UX와 동일한 화면 동작으로 수렴시킨다.
// 서버의 Prompt A가 의미를 확인한 경우에만 ui_command가 내려온다.
(() => {
    const originalRender = window.renderResponseWithCard;
    if (typeof originalRender !== 'function') return;

    window.renderResponseWithCard = function(data) {
        const command = data && data.ui_command;
        if (!command || !command.type) {
            return originalRender(data);
        }

        const st = data.state || {};
        if (st.profile) window.serverProfile = st.profile;

        if (command.type === 'RESET_PROFILE') {
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

        if (command.type === 'START_ELIGIBILITY') {
            if (typeof deactivateAllCards === 'function') deactivateAllCards();
            // 버튼 [다른 자격 조회하기]와 동일하게 새 정책 선택 카드부터 시작한다.
            Promise.resolve(startEligibility()).catch(() => {
                addBotMessage('자격 확인 정책 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.');
            });
            return;
        }

        if (command.type === 'RESET_CHAT') {
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

        return originalRender(data);
    };
})();
