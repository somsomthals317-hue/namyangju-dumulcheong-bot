"""32개 정책의 원본 ZIP·추출본·요약·자격 규칙·링크 정합성 감사."""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SUMMARY_PATH = BASE_DIR / "summary_documents_with_policy_id.json"
RULES_PATH = BASE_DIR / "policy_eligibility_rules.json"
ORIGIN_DIR = BASE_DIR / "policy_origin_extracted" / "json"
DEFAULT_ZIP = BASE_DIR / "policy_origin.zip"
REPORT_PATH = BASE_DIR / "policy_full_coverage_matrix.md"
JSON_REPORT_PATH = BASE_DIR / "policy_data_audit.json"

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

ALLOWED_STARTUP_CONDITIONS = {
    "해당없음", "미창업", "예비창업자", "기창업자", "예비창업자,기창업자",
}
EXPECTED_STARTUP_EXCEPTIONS = {
    "NYJ-YOUTH-001": "미창업",
    "NYJ-YOUTH-021": "예비창업자,기창업자",
}
DEFAULT_AGE_POLICY_IDS = {
    "NYJ-YOUTH-002", "NYJ-YOUTH-008", "NYJ-YOUTH-016",
    "NYJ-YOUTH-018", "NYJ-YOUTH-023", "NYJ-YOUTH-024",
}
SUMMARY_CONDITION_KEYS = set(CONDITION_MAP)
RULE_CONDITION_KEYS = set(CONDITION_MAP.values())


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalized_doc(doc):
    return {
        "category": str(doc.get("category") or "").strip(),
        "policy_name": str(doc.get("policy_name") or "").strip(),
        "source": str(doc.get("source") or "").strip(),
        "content": re.sub(r"\s+", " ", str(doc.get("content") or "")).strip(),
    }


def load_extracted_origins():
    docs = []
    for path in sorted(ORIGIN_DIR.rglob("*.json")):
        docs.append(load_json(path))
    return docs


def load_zip_origins(path: Path):
    docs = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".json"):
                docs.append(json.loads(archive.read(name).decode("utf-8")))
    return docs


def duplicate_values(items, key):
    seen, duplicates = set(), set()
    for item in items:
        value = item.get(key)
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(str(value) for value in duplicates)


def evidence_tokens(value):
    ignored = {
        "해당없음", "포함", "이상", "이하", "미만",
        "청년", "거주", "소득", "취업", "미취업", "대학생", "미혼", "무주택",
    }
    return [
        token for token in re.findall(r"\d+|[가-힣]{2,}", str(value or ""))
        if token not in ignored
    ]


def audit(zip_path: Path):
    summaries = load_json(SUMMARY_PATH)
    rules = load_json(RULES_PATH)
    origins = load_extracted_origins()
    zip_origins = load_zip_origins(zip_path)

    errors, warnings = [], []
    for label, docs in (
        ("추출 원본", origins), ("ZIP 원본", zip_origins),
        ("Summary", summaries), ("Eligibility", rules),
    ):
        if len(docs) != 32:
            errors.append(f"{label} 개수: 예상 32, 실제 {len(docs)}")
        for key in ("policy_name",):
            duplicates = duplicate_values(docs, key)
            if duplicates:
                errors.append(f"{label} 중복 {key}: {duplicates}")

    for label, docs in (("Summary", summaries), ("Eligibility", rules)):
        duplicates = duplicate_values(docs, "policy_id")
        if duplicates:
            errors.append(f"{label} 중복 policy_id: {duplicates}")

    origin_by_name = {doc["policy_name"]: doc for doc in origins}
    zip_by_name = {doc["policy_name"]: doc for doc in zip_origins}
    summary_by_name = {doc["policy_name"]: doc for doc in summaries}
    rule_by_name = {doc["policy_name"]: doc for doc in rules}

    origin_names = set(origin_by_name)
    for label, names in (
        ("ZIP", set(zip_by_name)),
        ("Summary", set(summary_by_name)),
        ("Eligibility", set(rule_by_name)),
    ):
        missing = sorted(origin_names - names)
        extra = sorted(names - origin_names)
        if missing or extra:
            errors.append(f"{label} 정책명 불일치: 누락={missing}, 초과={extra}")

    for name in sorted(origin_names & set(zip_by_name)):
        if normalized_doc(origin_by_name[name]) != normalized_doc(zip_by_name[name]):
            errors.append(f"ZIP과 추출본 내용 불일치: {name}")

    rows = []
    total_additional_questions = 0
    for summary in summaries:
        name = summary["policy_name"]
        policy_id = summary["policy_id"]
        origin = origin_by_name.get(name, {})
        rule = rule_by_name.get(name, {})
        if rule and rule.get("policy_id") != policy_id:
            errors.append(
                f"policy_id 불일치: {name} Summary={policy_id}, Eligibility={rule.get('policy_id')}"
            )

        source = str(origin.get("source") or "").strip()
        if not source.startswith("https://www.nyj.go.kr/"):
            errors.append(f"남양주시 HTTPS 공식 링크가 아님: {name} -> {source}")

        basic = rule.get("basic_condition", {})
        info_only = rule.get("eligibility_mode") == "INFO_ONLY"

        missing_summary_keys = sorted(SUMMARY_CONDITION_KEYS - set(summary))
        if missing_summary_keys:
            errors.append(f"Summary 8조건 키 누락: {name} -> {missing_summary_keys}")
        if set(basic) != RULE_CONDITION_KEYS:
            errors.append(
                f"Rules 8조건 키 불일치: {name} -> {sorted(set(basic))}"
            )
        for summary_key in CONDITION_MAP:
            value = str(summary.get(summary_key) or "").strip()
            if not value:
                errors.append(f"Summary 빈 조건값: {name}/{summary_key}")
            if "확인 필요" in value:
                errors.append(f"자격 조건에 확인 필요 사용: {name}/{summary_key}={value}")
        for rule_key in RULE_CONDITION_KEYS:
            value = str(basic.get(rule_key) or "").strip()
            if not value:
                errors.append(f"Rules 빈 조건값: {name}/{rule_key}")
            if "확인 필요" in value:
                errors.append(f"자격 조건에 확인 필요 사용: {name}/{rule_key}={value}")

        questions = rule.get("additional_questions") or []
        if not isinstance(questions, list):
            errors.append(f"additional_questions 배열 아님: {name}")
            questions = []
        total_additional_questions += len(questions)
        question_ids = []
        for question in questions:
            if not isinstance(question, dict):
                errors.append(f"추가 질문 객체 형식 오류: {name}")
                continue
            question_id = str(question.get("question_id") or "").strip()
            question_text = str(question.get("question") or "").strip()
            options = question.get("options")
            if not question_id or not question_text:
                errors.append(f"추가 질문 ID/문구 누락: {name} -> {question}")
            question_ids.append(question_id)
            if not isinstance(options, list):
                errors.append(f"추가 질문 options 배열 아님: {name}/{question_id}")
            elif not options and question_id != "birth_date":
                errors.append(f"자유입력 질문 유형 미등록: {name}/{question_id}")
            elif options and len({str(item).strip() for item in options}) != len(options):
                errors.append(f"추가 질문 선택지 중복: {name}/{question_id}")
        if len(question_ids) != len(set(question_ids)):
            errors.append(f"추가 질문 ID 중복: {name}")
        if info_only and questions:
            errors.append(f"INFO_ONLY에 자격 추가 질문 존재: {name}")

        # 공통 Profile로 직접 답할 수 없는 실제 basic condition은
        # 반드시 정책별 추가 질문으로 확인해야 한다.
        needs_question = []
        if basic.get("income") not in (None, "", "해당없음"):
            needs_question.append("income")
        employment = str(basic.get("employment") or "")
        if (
            employment not in ("", "해당없음")
            and "미취업" not in employment
            and not employment.startswith("재직자")
        ):
            needs_question.append("employment")
        housing = str(basic.get("housing") or "")
        if housing not in ("", "해당없음", "무주택"):
            needs_question.append("housing")
        if needs_question and not questions:
            errors.append(
                f"비정형 basic condition 추가 질문 누락: {name} -> {needs_question}"
            )

        # 사용자가 정한 프로젝트 공통값: 원문에 별도 기준이 없더라도
        # 신청형 청년정책은 남양주시·만 19~39세·2026년을 기본 범위로 둔다.
        if policy_id.startswith("NYJ-YOUTH-"):
            expected_residency = (
                "경기도(남양주시 포함)" if policy_id == "NYJ-YOUTH-001" else "남양주시"
            )
            if summary.get("residency_condition") != expected_residency:
                errors.append(
                    f"프로젝트 거주 기본값 불일치: {name} -> {summary.get('residency_condition')}"
                )
            if "2026" not in str(summary.get("business_period") or ""):
                errors.append(f"프로젝트 사업기간 기본값 누락: {name}")
            if policy_id in DEFAULT_AGE_POLICY_IDS:
                expected_age = "만 19세~만 39세"
                if summary.get("age_condition") != expected_age or basic.get("age") != expected_age:
                    errors.append(
                        f"프로젝트 연령 기본값 불일치: {name} Summary={summary.get('age_condition')}, Rules={basic.get('age')}"
                    )

        expected_startup = EXPECTED_STARTUP_EXCEPTIONS.get(policy_id, "해당없음")
        summary_startup = str(summary.get("startup_condition") or "해당없음")
        rule_startup = str(basic.get("startup") or "해당없음")
        if summary_startup not in ALLOWED_STARTUP_CONDITIONS:
            errors.append(f"startup_condition 허용값 위반: {name} -> {summary_startup}")
        if rule_startup not in ALLOWED_STARTUP_CONDITIONS:
            errors.append(f"Rules startup 허용값 위반: {name} -> {rule_startup}")
        if summary_startup != expected_startup or rule_startup != expected_startup:
            errors.append(
                f"startup_condition 정책 불일치: {name} Summary={summary_startup}, Rules={rule_startup}, 예상={expected_startup}"
            )
        for summary_key, rule_key in CONDITION_MAP.items():
            summary_value = str(summary.get(summary_key) or "해당없음").strip()
            rule_value = str(basic.get(rule_key) or "해당없음").strip()
            if summary_value != rule_value:
                if not info_only:
                    errors.append(
                        f"조건 불일치: {name}/{rule_key} Summary={summary_value}, Eligibility={rule_value}"
                    )
                elif rule_value != "해당없음":
                    errors.append(
                        f"INFO_ONLY Rules 조건은 해당없음이어야 함: {name}/{rule_key}={rule_value}"
                    )

            if rule_value != "해당없음":
                content = str(origin.get("content") or "")
                tokens = evidence_tokens(rule_value)
                local_equivalent = (
                    "남양주시" in rule_value and "관내" in content
                )
                approved_project_default = (
                    policy_id.startswith("NYJ-YOUTH-")
                    and (
                        rule_key == "residency"
                        or (rule_key == "age" and policy_id in DEFAULT_AGE_POLICY_IDS)
                    )
                )
                if (
                    tokens and not local_equivalent and not approved_project_default
                    and not any(token in content for token in tokens)
                ):
                    warnings.append(
                        f"원문 자동 근거 확인 필요: {name}/{rule_key}={rule_value}"
                    )

        service_type = summary.get("service_type", "POLICY")
        if policy_id.startswith("NYJ-EXPLAIN-"):
            if not info_only:
                errors.append(f"정보·시설 정책 INFO_ONLY 누락: {name}")
            if service_type not in {"INFORMATION", "FACILITY"}:
                errors.append(f"정보·시설 service_type 누락: {name}")
            if not rule.get("info_only_reason"):
                errors.append(f"INFO_ONLY 안내 이유 누락: {name}")
        elif info_only:
            errors.append(f"신청 정책이 잘못 INFO_ONLY 처리됨: {name}")

        interests = summary.get("recommendation_interests") or []
        rows.append({
            "policy_id": policy_id,
            "policy_name": name,
            "service_type": service_type,
            "eligibility_mode": rule.get("eligibility_mode", "FULL"),
            "recommendation_interests": interests,
            "source": source,
            "zip_equal": normalized_doc(origin) == normalized_doc(zip_by_name.get(name, {})),
        })

    result = {
        "status": "PASS" if not errors else "FAIL",
        "counts": {
            "zip_origins": len(zip_origins),
            "extracted_origins": len(origins),
            "summaries": len(summaries),
            "eligibility_rules": len(rules),
            "additional_questions": total_additional_questions,
        },
        "errors": errors,
        "warnings": sorted(set(warnings)),
        "policies": rows,
    }
    return result


def write_reports(result):
    JSON_REPORT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 32개 정책 데이터·링크 전수 감사",
        "",
        f"- 최종 상태: **{result['status']}**",
        f"- ZIP 원본 / 추출본 / Summary / Eligibility: "
        f"{result['counts']['zip_origins']} / {result['counts']['extracted_origins']} / "
        f"{result['counts']['summaries']} / {result['counts']['eligibility_rules']}",
        f"- 오류: {len(result['errors'])}건",
        f"- 원문 수동 재확인 권고: {len(result['warnings'])}건",
        f"- 구조·표시를 검사한 추가 질문: {result['counts']['additional_questions']}개",
        "",
        "| ID | 정책명 | 유형 | 자격 처리 | 추천 분야 | ZIP 일치 | 공식 링크 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in result["policies"]:
        interests = ", ".join(item["recommendation_interests"]) or "기본 분류"
        lines.append(
            f"| {item['policy_id']} | {item['policy_name']} | {item['service_type']} | "
            f"{item['eligibility_mode']} | {interests} | "
            f"{'PASS' if item['zip_equal'] else 'FAIL'} | [열기]({item['source']}) |"
        )
    lines.extend(["", "## 오류", ""])
    lines.extend(f"- {item}" for item in result["errors"] or ["없음"])
    lines.extend(["", "## 원문 수동 재확인 권고", ""])
    lines.extend(f"- {item}" for item in result["warnings"] or ["없음"])
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    args = parser.parse_args()
    result = audit(args.zip.resolve())
    write_reports(result)
    print(json.dumps({
        "status": result["status"],
        "counts": result["counts"],
        "errors": len(result["errors"]),
        "warnings": len(result["warnings"]),
        "report": REPORT_PATH.name,
    }, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
