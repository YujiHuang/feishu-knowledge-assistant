"""联网搜索兜底（Tavily，可选）。api_key 为空时禁用。"""
import httpx


class WebSearcher:
    def __init__(self, cfg):
        self.api_key = cfg.get("web_search", "api_key", default="") or ""
        self.max_results = cfg.get("web_search", "max_results", default=5)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str) -> list[dict]:
        """返回 [{title, url, text}]。失败返回空列表，不阻塞问答。"""
        if not self.enabled:
            return []
        try:
            r = httpx.post(
                "https://api.tavily.com/search",
                json={"api_key": self.api_key, "query": query,
                      "max_results": self.max_results, "include_answer": False},
                timeout=15,
            )
            r.raise_for_status()
            return [{"title": item.get("title") or item["url"],
                     "url": item["url"],
                     "text": (item.get("content") or "")[:1500]}
                    for item in r.json().get("results", [])]
        except Exception:
            return []
