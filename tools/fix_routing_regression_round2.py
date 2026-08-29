from pathlib import Path


def once(text, old, new, label):
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: count={text.count(old)}")
    return text.replace(old, new, 1)

p = Path('agent.py')
s = p.read_text(encoding='utf-8')
s = once(
    s,
    '''    if mentioned_topics and not exact_policy and (\n        _is_explicit_recommend_request(msg)\n        or re.search(r"(?:바꿔|바꾸|변경|전환)(?:줘|해줘|할래)?", msg)\n    ):\n''',
    '''    if (\n        mentioned_topics\n        and not exact_policy\n        # 설명+추천 / 자격+추천은 단일 추천 shortcut이 아니라 multi-task workflow가 처리한다.\n        and not re.search(r"설명|알려|내용|뭐야|자격|가능한지|되는지", msg)\n        and (\n            _is_explicit_recommend_request(msg)\n            or re.search(r"(?:바꿔|바꾸|변경|전환)(?:줘|해줘|할래)?", msg)\n        )\n    ):\n''',
    'restrict recommendation shortcut',
)
p.write_text(s, encoding='utf-8')

p = Path('test_final_routing_regression.py')
t = p.read_text(encoding='utf-8')
t = once(
    t,
    'self.assertLess(text.index("simple_menu_type = _simple_menu_type(message)"), text.index("should_probe_intent = ("))',
    'self.assertLess(text.index("simple_menu_type = ("), text.index("should_probe_intent = ("))',
    'bare menu test marker',
)
t = t.replace('text = open("server.py", encoding="utf-8").read()', 'from pathlib import Path\n        text = Path("server.py").read_text(encoding="utf-8")')
p.write_text(t, encoding='utf-8')
