"""Chunk 存储与检索：LanceDB 向量库 + jieba/BM25 关键词内存索引。"""
import re
import threading
from pathlib import Path

import jieba
import lancedb
from rank_bm25 import BM25Okapi

_TABLE = "chunks"
_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    tokens = []
    for frag in _TOKEN_RE.findall(text.lower()):
        tokens.extend(jieba.cut_for_search(frag))
    return [t for t in tokens if t.strip()]


class ChunkStore:
    def __init__(self, data_dir: Path):
        self.db = lancedb.connect(str(data_dir / "lancedb"))
        self._bm25: BM25Okapi | None = None
        self._bm25_ids: list[dict] = []   # 与 bm25 corpus 对齐的 chunk 元数据
        self._corpus_sets: list[set] = []
        self._lock = threading.Lock()
        self._migrate()
        if _TABLE in self._table_names():
            self.rebuild_keyword_index()

    def _migrate(self):
        """旧版索引缺少 project/mtime/anchors 等列时无法追加新行：删表要求全量重建。"""
        if _TABLE not in self._table_names():
            return
        try:
            names = set(self._table().schema.names)
        except Exception:
            return
        if not {"project", "mtime", "doc_url", "anchors"} <= names:
            print("⚠️ 检测到旧版索引结构，已清空索引，"
                  "请重新点击「同步知识库」全量重建。")
            self.db.drop_table(_TABLE)

    def _table_names(self):
        try:
            return set(self.db.table_names())
        except Exception:
            return set()

    def _table(self):
        return self.db.open_table(_TABLE)

    # ---------- 写入 ----------
    def replace_doc(self, doc_token: str, rows: list[dict]):
        with self._lock:
            if _TABLE in self._table_names():
                self._table().delete(f"doc_token = '{doc_token}'")
                if rows:
                    self._table().add(rows)
            elif rows:
                self.db.create_table(_TABLE, rows)

    def delete_doc(self, doc_token: str):
        with self._lock:
            if _TABLE in self._table_names():
                self._table().delete(f"doc_token = '{doc_token}'")

    # ---------- 统计 ----------
    def counts(self) -> dict:
        if _TABLE not in self._table_names():
            return {"chunks": 0, "docs": 0}
        tbl = self._table()
        n = tbl.count_rows()
        docs = len(set(r["doc_token"] for r in
                       tbl.search().select(["doc_token"]).limit(n or 1).to_list())) if n else 0
        return {"chunks": n, "docs": docs}

    # ---------- 检索 ----------
    def vector_search(self, qvec: list[float], k: int,
                      projects: set[str] | None = None) -> list[dict]:
        if _TABLE not in self._table_names():
            return []
        fetch = k * 4 if projects else k
        res = self._table().search(qvec).metric("cosine").limit(fetch).to_list()
        out = []
        for r in res:
            if projects and r.get("project") not in projects:
                continue
            r.pop("vector", None)
            r["score"] = 1.0 - r.get("_distance", 1.0)   # 余弦相似度
            out.append(r)
            if len(out) >= k:
                break
        return out

    def rebuild_keyword_index(self):
        """全量加载 chunk 文本建 BM25（万级规模内存占用可忽略）。"""
        with self._lock:
            if _TABLE not in self._table_names():
                self._bm25, self._bm25_ids, self._corpus_sets = None, [], []
                return
            tbl = self._table()
            n = tbl.count_rows()
            if not n:
                self._bm25, self._bm25_ids, self._corpus_sets = None, [], []
                return
            rows = (tbl.search().select(["id", "doc_token", "title", "title_path",
                                         "url", "doc_url", "anchors",
                                         "project", "mtime", "text"])
                    .limit(n).to_list())
            corpus = [_tokenize(r["title_path"] + " " + r["text"]) for r in rows]
            self._bm25 = BM25Okapi(corpus)
            self._bm25_ids = rows
            self._corpus_sets = [set(toks) for toks in corpus]

    def keyword_search(self, query: str, k: int,
                       projects: set[str] | None = None) -> list[dict]:
        if not self._bm25:
            return []
        tokens = _tokenize(query)
        tokset = set(tokens)
        scores = self._bm25.get_scores(tokens)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in order:
            if len(out) >= k:
                break
            r = self._bm25_ids[i]
            if projects and r.get("project") not in projects:
                continue
            # 小语料下 BM25 IDF 可能非正：只要文档确实包含查询词就保留
            if scores[i] <= 0 and not (tokset & self._corpus_sets[i]):
                continue
            r = dict(r)
            r["score"] = float(scores[i])
            out.append(r)
        return out
