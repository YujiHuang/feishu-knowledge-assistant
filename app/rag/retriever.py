"""混合检索：向量 + BM25，RRF 融合，时效加权重排。"""
import math
import time


def rrf_fuse(result_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion。输入若干个按相关性降序的结果列表（元素含 id）。"""
    scores: dict[str, float] = {}
    by_id: dict[str, dict] = {}
    for results in result_lists:
        for rank, r in enumerate(results):
            rid = r["id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            by_id.setdefault(rid, r)
    ranked = sorted(scores, key=scores.get, reverse=True)
    out = []
    for rid in ranked:
        r = dict(by_id[rid])
        r["rrf_score"] = scores[rid]
        out.append(r)
    return out


def recency_rerank(chunks: list[dict], weight: float, half_life_days: float,
                   now: float | None = None) -> list[dict]:
    """时效加权：得分 = rrf ×（1 + weight × exp(-年龄/半衰期)）。mtime=0 不加权。"""
    now = now or time.time()
    for c in chunks:
        mtime = c.get("mtime") or 0
        boost = 0.0
        if mtime > 0:
            age_days = max(0.0, (now - mtime) / 86400)
            boost = math.exp(-age_days * math.log(2) / half_life_days)
        c["final_score"] = c["rrf_score"] * (1 + weight * boost)
    return sorted(chunks, key=lambda c: c["final_score"], reverse=True)


class Retriever:
    def __init__(self, store, embedder, cfg):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg

    def search(self, query: str, projects: list[str] | None = None) -> dict:
        """返回 {chunks: [...], top_similarity: float}。projects 非空时只检索这些项目。"""
        vk = self.cfg.get("retrieval", "vector_top_k", default=12)
        kk = self.cfg.get("retrieval", "keyword_top_k", default=12)
        fk = self.cfg.get("retrieval", "final_top_k", default=8)
        weight = self.cfg.get("retrieval", "recency_weight", default=0.5)
        half_life = self.cfg.get("retrieval", "recency_half_life_days", default=90)
        pset = set(projects) if projects else None

        qvec = self.embedder.encode_query(query)
        vec_hits = self.store.vector_search(qvec, vk, pset)
        kw_hits = self.store.keyword_search(query, kk, pset)
        fused = rrf_fuse([vec_hits, kw_hits])
        fused = recency_rerank(fused, weight, half_life)[:fk]
        top_sim = vec_hits[0]["score"] if vec_hits else 0.0
        return {"chunks": fused, "top_similarity": top_sim}
