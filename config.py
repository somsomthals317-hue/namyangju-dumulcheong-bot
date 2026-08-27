"""
두물청 - 남양주시 청년정책 AI Agent
설정 파일
"""
import os

# 프로젝트 경로
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
VECTOR_DB_DIR = os.path.join(PROJECT_ROOT, "vector_db")

# 데이터 파일 경로
POLICY_ORIGIN_DIR = os.path.join(PROJECT_ROOT, "policy_origin_extracted", "json")
SUMMARY_FILE = os.path.join(PROJECT_ROOT, "summary_documents_with_policy_id.json")
RULES_FILE = os.path.join(PROJECT_ROOT, "policy_eligibility_rules.json")

# VectorStore 설정
COLLECTION_NAME = "dumoolchung_policies"
TOP_K = 5

# Gemini 설정 → OpenAI로 변경
OPENAI_MODEL = "gpt-4o-mini"

# 정보·시설 안내 카테고리. 추천에는 포함할 수 있지만, 자격은 INFO_ONLY로
# 안내하여 근거가 없는 PASS/FAIL을 만들지 않는다.
INFO_ONLY_CATEGORIES = ["청년창업센터", "청년정책"]
# 이전 이름을 참조하는 외부 코드가 있을 수 있어 호환용으로 유지한다.
EXPLAIN_ONLY_CATEGORIES = INFO_ONLY_CATEGORIES

# Profile 허용값
PROFILE_FIELDS = {
    "age": {"type": "int", "min": 0, "max": 100},
    "residency": {"type": "enum", "values": ["예", "아니오"]},
    "employment": {"type": "enum", "values": ["취업", "미취업"]},
    "student": {"type": "enum", "values": ["초등학생", "중학생", "고등학생", "대학생", "대학원생", "해당하지 않음"]},
    "startup": {"type": "enum", "values": ["창업 중", "창업 준비 중", "창업하지 않음"]},
    "housing": {"type": "enum", "values": ["주택 소유", "무주택"]},
    "marriage": {"type": "enum", "values": ["기혼", "미혼"]},
}
