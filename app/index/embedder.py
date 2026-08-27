"""Embedding 后端：本地 fastembed（默认）或 OpenAI 兼容接口（公司网关）。"""
import os

import httpx

# HuggingFace 的 Xet 下载后端在部分网络下不稳定（CAS Client Error），强制走普通 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# bge 中文模型检索查询前缀（官方建议）
_BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class LocalEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        from fastembed import TextEmbedding  # 延迟导入，首次会下载模型（约 100MB）
        self._model = TextEmbedding(model_name)
        self._is_bge = "bge" in model_name.lower()

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts, batch_size=32)]

    def encode_query(self, text: str) -> list[float]:
        if self._is_bge:
            text = _BGE_QUERY_PREFIX + text
        return self.encode([text])[0]


class OpenAIEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            batch = texts[i:i + 64]
            r = httpx.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": batch},
                timeout=60,
            )
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        return out

    def encode_query(self, text: str) -> list[float]:
        return self.encode([text])[0]


def build_embedder(cfg):
    if cfg.get("embedding", "provider", default="local") == "openai":
        return OpenAIEmbedder(
            cfg.get("embedding", "base_url"),
            cfg.get("embedding", "api_key"),
            cfg.get("embedding", "model"),
        )
    return LocalEmbedder(cfg.get("embedding", "local_model",
                                 default="BAAI/bge-small-zh-v1.5"))
