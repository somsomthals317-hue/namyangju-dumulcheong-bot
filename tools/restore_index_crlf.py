from pathlib import Path
import subprocess

raw = subprocess.check_output(['git','show','origin/main:static/index.html'])
text = raw.decode('utf-8')

def crlf(s):
    return s.replace('\n','\r\n')

def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected 1, got {count}')
    return text.replace(old, new, 1)

old_css = crlf('''        .chat-card-btn:disabled { opacity:0.5; cursor:not-allowed; }\n\n        /* 관심분야 버튼 그리드 */\n''')
new_css = crlf('''        .chat-card-btn:disabled { opacity:0.5; cursor:not-allowed; }\n        .aq-action-row {\n            display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr);\n            gap:8px; margin-bottom:12px;\n        }\n        .aq-action-btn {\n            width:100%; min-width:0; margin:0; padding:10px 4px;\n            background:#f8f8f8; color:#555; border:1px solid #ddd;\n            border-radius:10px; font-size:11px; font-weight:600; line-height:1.2;\n            cursor:pointer; white-space:nowrap; word-break:keep-all;\n        }\n        .aq-action-btn:hover { border-color:#1e8c6e; background:#f0faf7; }\n\n        /* 관심분야 버튼 그리드 */\n''')
text = replace_once(text, old_css, new_css, 'css')

old_markup = crlf('''        <div style="display:flex;gap:8px;margin-bottom:12px;">\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="resetAndShowFullCard('${aq.policy_id || ''}', this)">프로필 다시 설정하기</button>\n            <button type="button" class="chat-card-btn" style="background:#f8f8f8;color:#555;border:1px solid #ddd;flex:1;margin:0;" onclick="startEligibility()">다른 정책 선택</button>\n        </div>\n''')
new_markup = crlf('''        <div class="aq-action-row">\n            <button type="button" class="aq-action-btn" onclick="resetAndShowFullCard('${aq.policy_id || ''}', this)">프로필 다시 설정하기</button>\n            <button type="button" class="aq-action-btn" onclick="startEligibility()">다른 정책 선택</button>\n        </div>\n''')
text = replace_once(text, old_markup, new_markup, 'markup')
Path('static/index.html').write_bytes(text.encode('utf-8'))
print('restored main CRLF with minimal UI changes')
