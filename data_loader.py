"""
데이터 로딩 및 전처리
- 원문 32개에 policy_id 보강
- Summary/Rules 로딩
- Policy Bundle 생성
"""
import json
import os
from config import POLICY_ORIGIN_DIR, SUMMARY_FILE, RULES_FILE


def load_summary_documents():
    """summary_documents_with_policy_id.json 로딩"""
    with open(SUMMARY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_eligibility_rules():
    """policy_eligibility_rules.json 로딩"""
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_origin_documents():
    """policy_origin_extracted/json 아래 32개 원문 JSON 로딩"""
    documents = []
    for root, dirs, files in os.walk(POLICY_ORIGIN_DIR):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                # 상위 폴더명을 category로 사용 (이미 JSON에 category 있음)
                documents.append(doc)
    return documents


def enrich_origin_policy_ids(origin_docs, summary_docs):
    """
    원문 32개에 policy_id를 보강.
    - Summary/Rules와 policy_name이 일치하는 32개 → 고정 policy_id 부여
    - 구버전 데이터에 Summary가 없는 문서가 있으면 임시 고유 ID 부여
    """
    # Summary의 policy_name → policy_id 매핑
    name_to_id = {doc["policy_name"]: doc["policy_id"] for doc in summary_docs}
    
    explain_only_counter = 1
    enriched = []
    
    for doc in origin_docs:
        policy_name = doc.get("policy_name", "")
        category = doc.get("category", "")
        
        if policy_name in name_to_id:
            doc["policy_id"] = name_to_id[policy_name]
        else:
            # 설명 전용 정책에 고유 ID 부여
            doc["policy_id"] = f"NYJ-EXPLAIN-{explain_only_counter:03d}"
            explain_only_counter += 1
        
        enriched.append(doc)
    
    return enriched


def build_policy_bundles(summary_docs, rules_docs, origin_docs=None):
    """
    Summary와 Rules를 policy_id로 결합하여 Policy Bundle 32개 생성.
    원문이 함께 전달되면 공식 출처 URL도 Bundle에 포함한다.
    """
    rules_map = {r["policy_id"]: r for r in rules_docs}
    source_map = {}
    for origin in origin_docs or []:
        policy_id = origin.get("policy_id")
        source = str(origin.get("source") or "").strip()
        if policy_id and source.startswith(("http://", "https://")):
            source_map.setdefault(policy_id, source)
    bundles = []
    
    for summary in summary_docs:
        pid = summary["policy_id"]
        rule = rules_map.get(pid)
        if rule:
            bundle = {
                "policy_id": pid,
                "policy_name": summary["policy_name"],
                "category": summary.get("category", ""),
                "sub_category": summary.get("sub_category", ""),
                "tags": summary.get("tags", []),
                "summary": summary.get("summary", ""),
                "main_target": summary.get("main_target", ""),
                "benefit": summary.get("benefit", ""),
                "age_condition": summary.get("age_condition", ""),
                "residency_condition": summary.get("residency_condition", ""),
                "income_condition": summary.get("income_condition", ""),
                "employment_condition": summary.get("employment_condition", ""),
                "student_condition": summary.get("student_condition", ""),
                "startup_condition": summary.get("startup_condition", ""),
                "housing_condition": summary.get("housing_condition", ""),
                "marriage_condition": summary.get("marriage_condition", ""),
                "business_period": summary.get("business_period", ""),
                "application_period": summary.get("application_period", ""),
                "service_type": summary.get("service_type", "POLICY"),
                "recommendation_interests": summary.get("recommendation_interests", []),
                "source": source_map.get(pid, ""),
                # Rules 필드
                "basic_condition": rule.get("basic_condition", {}),
                "caution_condition": rule.get("caution_condition", []),
                "unknown_flag": rule.get("unknown_flag", False),
                "additional_questions": rule.get("additional_questions", []),
                "eligibility_mode": rule.get("eligibility_mode", "FULL"),
                "info_only_reason": rule.get("info_only_reason", ""),
                "unverified_conditions": rule.get("unverified_conditions", []),
            }
            bundles.append(bundle)
    
    return bundles


def get_all_data():
    """모든 데이터를 로딩하고 전처리하여 반환"""
    summary_docs = load_summary_documents()
    rules_docs = load_eligibility_rules()
    origin_docs = load_origin_documents()
    
    # 원문에 policy_id 보강
    enriched_origins = enrich_origin_policy_ids(origin_docs, summary_docs)
    
    # Policy Bundle 생성
    bundles = build_policy_bundles(summary_docs, rules_docs, enriched_origins)
    
    return {
        "origin_docs": enriched_origins,
        "summary_docs": summary_docs,
        "rules_docs": rules_docs,
        "bundles": bundles,
    }
