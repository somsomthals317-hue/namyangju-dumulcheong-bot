"""Summary/Rules의 조건 스키마와 사용자 승인 기본값을 일관되게 맞춘다."""
from __future__ import annotations

import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SUMMARY_PATH = BASE_DIR / "summary_documents_with_policy_id.json"
RULES_PATH = BASE_DIR / "policy_eligibility_rules.json"

CONDITION_MAP = {
    "age_condition": "age",
    "residency_condition": "residency",
    "income_condition": "income",
    "employment_condition": "employment",
    "student_condition": "student",
    "startup_condition": "startup",
    "housing_condition": "housing",
    "marriage_condition": "marriage",
}

# 원문에 별도 값이 없을 때 적용하는 이 프로젝트의 승인된 공통값.
SUMMARY_OVERRIDES = {
    "NYJ-YOUTH-002": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
        "employment_condition": "미취업",
    },
    "NYJ-YOUTH-006": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-008": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
    },
    "NYJ-YOUTH-009": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-012": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-016": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
        "employment_condition": "현역병 또는 사회복무요원으로 입영",
        "business_period": "2026년 중",
    },
    "NYJ-YOUTH-017": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-018": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
    },
    "NYJ-YOUTH-019": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-023": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
    },
    "NYJ-YOUTH-024": {
        "age_condition": "만 19세~만 39세",
        "residency_condition": "남양주시",
    },
    "NYJ-YOUTH-025": {"residency_condition": "남양주시"},
    "NYJ-YOUTH-021": {"age_condition": "만 19세~만 39세"},
}

INFO_SUMMARY_OVERRIDES = {
    policy_id: {summary_key: "해당없음" for summary_key in CONDITION_MAP}
    for policy_id in (
        "NYJ-EXPLAIN-001",
        "NYJ-EXPLAIN-002",
        "NYJ-EXPLAIN-003",
        "NYJ-EXPLAIN-004",
        "NYJ-EXPLAIN-005",
        "NYJ-EXPLAIN-006",
        "NYJ-EXPLAIN-007",
    )
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    summaries = load(SUMMARY_PATH)
    rules = load(RULES_PATH)
    rules_by_id = {item["policy_id"]: item for item in rules}

    for summary in summaries:
        policy_id = summary["policy_id"]
        # 누락과 빈 문자열을 모두 명시적인 해당없음으로 통일한다.
        for key in CONDITION_MAP:
            if not str(summary.get(key) or "").strip():
                summary[key] = "해당없음"
        summary.update(SUMMARY_OVERRIDES.get(policy_id, {}))
        summary.update(INFO_SUMMARY_OVERRIDES.get(policy_id, {}))

        rule = rules_by_id[policy_id]
        basic = rule.setdefault("basic_condition", {})
        if policy_id.startswith("NYJ-YOUTH-"):
            # 신청형 25개는 Summary와 Rules가 같은 조건값을 사용한다.
            for summary_key, rule_key in CONDITION_MAP.items():
                basic[rule_key] = summary[summary_key]
        else:
            # 정보·시설은 개별 자격 판정 대상이 아니므로 키는 모두 두되
            # 값은 해당없음으로 고정하고 eligibility_mode로 분리한다.
            for rule_key in CONDITION_MAP.values():
                basic[rule_key] = "해당없음"

    save(SUMMARY_PATH, summaries)
    save(RULES_PATH, rules)
    print(json.dumps({
        "summaries": len(summaries),
        "rules": len(rules),
        "summary_condition_keys": len(CONDITION_MAP),
        "missing_values": 0,
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
