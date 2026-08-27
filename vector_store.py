"""
VectorStore 관리 (ChromaDB + OpenAI Embeddings)
- 원문 정책 문서 32개를 embedding하여 적재
- EXPLAIN에서 유사 정책 Top 3 검색
"""
import os
import chromadb
from chromadb.utils import embedding_functions
from config import VECTOR_DB_DIR, COLLECTION_NAME, TOP_K


def get_embedding_function():
    """OpenAI embedding 함수 (text-embedding-3-small)"""
    api_key = os.getenv("OPENAI_API_KEY")
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key=api_key,
        model_name="text-embedding-3-small",
    )


def initialize_vector_store(enriched_origins):
    """
    원문 정책 32개를 ChromaDB에 적재.
    정책 하나당 vector 하나 (policy_name + category + content를 결합)
    """
    client = chromadb.Client()  # in-memory for MVP
    ef = get_embedding_function()
    
    # 기존 컬렉션 삭제 후 재생성
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    
    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
    )
    
    documents = []
    metadatas = []
    ids = []
    
    for doc in enriched_origins:
        policy_id = doc.get("policy_id", "UNKNOWN")
        policy_name = doc.get("policy_name", "")
        category = doc.get("category", "")
        content = doc.get("content", "")
        source = doc.get("source", "")
        
        # embedding 대상: policy_name을 강조하여 검색 정확도 향상
        combined_text = f"정책명: {policy_name}\n카테고리: {category}\n정책명: {policy_name}\n\n{content}"
        
        documents.append(combined_text)
        metadatas.append({
            "policy_id": policy_id,
            "policy_name": policy_name,
            "category": category,
            "source": source,
        })
        ids.append(policy_id)
    
    collection.add(
        documents=documents,
        metadatas=metadatas,
        ids=ids,
    )
    
    return client, collection


def retrieve_policy_candidates(collection, query, top_k=TOP_K):
    """
    사용자 질문으로 유사 정책 문서 Top K 검색 (순수 RAG)
    반환: [{policy_id, policy_name, category, source, content, distance}]
    """
    # Top K를 넉넉하게 검색 후 반환
    results = collection.query(
        query_texts=[query],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )
    
    candidates = []
    if results and results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            candidates.append({
                "policy_id": doc_id,
                "policy_name": results["metadatas"][0][i]["policy_name"],
                "category": results["metadatas"][0][i]["category"],
                "source": results["metadatas"][0][i]["source"],
                "content": results["documents"][0][i],
                "distance": results["distances"][0][i] if results["distances"] else None,
            })
    
    return candidates
