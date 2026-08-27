"""带引用的回答生成：知识库优先，置信度不足时联网兜底。"""
import json
import re

import httpx

SYSTEM_PROMPT = """你是一名严谨的企业知识助手，服务对象是一位战略分析师。

规则（必须严格遵守）：
1. 只能依据下面提供的【参考资料】回答，禁止使用资料之外的任何知识或推测。
2. 引用规则：答案中的每个事实性论断后面必须标注来源编号，格式如 [1]、[2]。
3. 如果参考资料不足以回答问题，必须明确回答："知识库和网络检索结果中未找到足够依据"，并简要说明缺什么，不要编造。
4. 资料之间有冲突时，指出冲突并分别标注来源。
5. 参考资料中形如 /api/media/xxxx 的路径是可点击的附件（截图、录屏、文件）。当用户询问是否有截图/录屏，或附件与答案相关时，把该路径原样写进回答（不要改写或省略）。
6. 排版只使用加粗、"- "列表和缩进，禁止使用 # 标题语法（渲染环境不支持）。
7. 用简体中文、简洁的分析师风格作答，先给结论，再给依据。"""


class AnswerEngine:
    def __init__(self, retriever, web_searcher, cfg):
        self.retriever = retriever
        self.web = web_searcher
        self.cfg = cfg

    def ask(self, question: str, projects: list[str] | None = None,
            history: list[dict] | None = None) -> dict:
        history = history or []
        # 追问往往省略主语（"那华东呢？"），把上一问拼进检索 query 补上下文
        query = (history[-1].get("q", "") + " " + question).strip() if history else question
        result = self.retriever.search(query, projects)
        chunks = result["chunks"]
        sources = [{"n": i + 1, "kind": "feishu",
                    "title": c["title"],                 # 展示：文档原标题
                    "context": c.get("title_path") or c["title"],  # 喂给 LLM：含章节路径
                    "url": c["url"],                     # 块级定位链接（兜底）
                    "group": c.get("doc_url") or c["url"],   # 文档级链接（聚合用）
                    "anchors": _load_anchors(c.get("anchors")),   # 块内逐行锚点
                    "text": c["text"]}
                   for i, c in enumerate(chunks)]

        threshold = self.cfg.get("retrieval", "web_fallback_threshold", default=0.45)
        used_web = False
        if result["top_similarity"] < threshold and self.web.enabled:
            web_hits = self.web.search(question)
            used_web = bool(web_hits)
            base = len(sources)
            sources += [{"n": base + i + 1, "kind": "web",
                         "title": h["title"], "url": h["url"],
                         "group": h["url"], "text": h["text"]}
                        for i, h in enumerate(web_hits)]

        if not sources:
            return {"answer": "知识库中未找到相关内容" +
                    ("，且未配置联网搜索。" if not self.web.enabled else "，联网搜索也无结果。"),
                    "sources": [], "used_web": used_web,
                    "top_similarity": result["top_similarity"]}

        answer = self._generate(question, sources, history)
        refine_citation_anchors(answer, sources)   # 序号 → 它实际支撑的那一行
        cited = _cited_numbers(answer)
        shown = [s for s in sources if not cited or s["n"] in cited]
        # 同一文档合并为一行；每个序号保留各自的精确定位链接
        merged: dict[str, dict] = {}
        for s in shown:
            m = merged.setdefault(s["group"], {"ns": [], "refs": [], "kind": s["kind"],
                                               "title": s["title"], "url": s["group"]})
            m["ns"].append(s["n"])
            m["refs"].append({"n": s["n"], "url": s["url"]})
        return {
            "answer": answer,
            "sources": list(merged.values()),
            "used_web": used_web,
            "top_similarity": round(result["top_similarity"], 3),
        }

    def _generate(self, question: str, sources: list[dict],
                  history: list[dict] | None = None) -> str:
        refs = "\n\n".join(
            f"[{s['n']}]（{'飞书文档' if s['kind'] == 'feishu' else '网页'}："
            f"{s.get('context') or s['title']}）\n{s['text']}"
            for s in sources)
        user_msg = f"【参考资料】\n{refs}\n\n【问题】\n{question}"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 多轮追问：带上最近几轮问答。每轮都要重新发一遍问题和答案，是纯 token 成本，
        # 所以做成配置项（retrieval.history_rounds，默认 2 轮；设 0 则完全不带历史）。
        rounds = int(self.cfg.get("retrieval", "history_rounds", default=2) or 0)
        for h in ((history or [])[-rounds:] if rounds > 0 else []):
            if h.get("q"):
                messages.append({"role": "user", "content": str(h["q"])})
                messages.append({"role": "assistant", "content": str(h.get("a", ""))})
        messages.append({"role": "user", "content": user_msg})
        r = httpx.post(
            self.cfg.get("llm", "base_url").rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg.get('llm', 'api_key')}"},
            json={
                "model": self.cfg.get("llm", "model"),
                "temperature": self.cfg.get("llm", "temperature", default=0.2),
                "messages": messages,
            },
            timeout=120,
        )
        if r.status_code >= 400:
            raise RuntimeError(_gateway_error(r, self.cfg.get("llm", "model")))
        return r.json()["choices"][0]["message"]["content"]


_GATEWAY_HINTS = {
    401: "网关拒绝了 api_key（无效、已停用或已轮换）→ 去网关后台确认 key",
    403: "key 有效但无权调用这个模型 → 需要网关管理员给账号开通",
    402: "网关计费拒绝：账号额度用尽 / 欠费 / 该模型未开通计费 → 找网关管理员加额度",
    404: "路径或模型名不对 → 检查 base_url 是否以 /v1 结尾、model 名是否还存在",
    429: "被限流了，稍等再试（并发或每分钟配额打满）",
}


def _gateway_error(resp, model: str) -> str:
    """把网关的 HTTP 错误翻译成能直接行动的说明，并带上网关自己的原文。"""
    code = resp.status_code
    hint = _GATEWAY_HINTS.get(code) or (
        "网关自身异常，通常稍后自动恢复" if code >= 500 else "网关返回了未预期的错误")
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        detail = str(err.get("message") if isinstance(err, dict) else err or body)
    except Exception:  # noqa
        detail = (resp.text or "").strip()
    detail = " ".join(detail.split())[:300]
    return (f"大模型网关返回 {code}（模型 {model}）。{hint}。"
            + (f" 网关原文：{detail}" if detail else ""))


def _cited_numbers(answer: str) -> set[int]:
    return {int(n) for n in _CITE_RE.findall(answer)}


# ---------- 引用序号 → 精确行定位 ----------
# 一个 chunk 最长 600 字，可能横跨十几行（表格里就是十几条记录）。只用块首锚点
# 会把「第 8 行的数字」定位到「第 1 行的话题」。做法：把序号紧跟的那句话与 chunk
# 内各行做加权匹配，命中哪一行就用那一行的锚点。
_CITE_RE = re.compile(r"\[(\d{1,2})\]")
_SENT_END = "。！？；\n"
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_LAT_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")
_CJK_RE = re.compile(r"[一-鿿]{2,}")


def _load_anchors(raw) -> list[tuple[int, str]]:
    """入库时存的是 JSON 字符串 [[偏移, 定位链接], ...]。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return [(int(o), str(u)) for o, u in data]
    except Exception:  # noqa
        return []


def _cite_contexts(answer: str) -> dict[int, list[str]]:
    """收集每个序号前面紧邻的那句话（引用通常紧跟它支撑的论断）。"""
    out: dict[int, list[str]] = {}
    for m in _CITE_RE.finditer(answer):
        head = answer[max(0, m.start() - 160):m.start()]
        cut = max(head.rfind(ch) for ch in _SENT_END)
        seg = _CITE_RE.sub("", head[cut + 1:] if cut >= 0 else head).strip()
        if len(seg) < 4:                       # 太短（如「见下。[1]」）→ 放宽到整段
            seg = _CITE_RE.sub("", head).strip()
        if seg:
            out.setdefault(int(m.group(1)), []).append(seg)
    return out


def _weighted_tokens(s: str) -> dict[str, float]:
    """数字辨识度最高（192、17、90），其次英文词，最后汉字二元组（免分词）。"""
    w: dict[str, float] = {}
    for t in _NUM_RE.findall(s):
        w[t] = 4.0
    for t in _LAT_RE.findall(s):
        w[t.lower()] = max(w.get(t.lower(), 0.0), 2.5)
    for run in _CJK_RE.findall(s):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            w[g] = max(w.get(g, 0.0), 1.0)
    return w


def _best_offset(contexts: list[str], text: str) -> int | None:
    """返回最匹配那一行在 chunk 正文中的起始偏移；证据不足返回 None。"""
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((pos, ln.lower()))
        pos += len(ln) + 1
    best = best_score = need = 0.0
    hit = None
    for ctx in contexts:
        w = _weighted_tokens(ctx)
        if not w:
            continue
        total = sum(w.values())
        for start, low in lines:
            if len(low.strip()) < 2:
                continue
            score = sum(v for t, v in w.items() if t in low)
            if score > best_score:
                hit, best_score, need = start, score, total
    if hit is None or best_score < max(4.0, 0.25 * need):
        return None    # 匹配太弱，宁可退回块首锚点，不要乱跳
    return hit


def _anchor_for(offset: int, anchors: list[tuple[int, str]]) -> str | None:
    """锚点偏移记的是所属行的结束位置，故取第一个「偏移 > 命中位置」的锚点。"""
    for off, url in anchors:
        if off > offset:
            return url
    return anchors[-1][1] if anchors else None


def refine_citation_anchors(answer: str, sources: list[dict]) -> None:
    """就地把 source['url'] 收窄到该序号实际支撑的那一行（失败则保留块级链接）。"""
    contexts = _cite_contexts(answer)
    for s in sources:
        if s.get("kind") != "feishu" or not s.get("anchors"):
            continue
        ctx = contexts.get(s["n"])
        if not ctx:
            continue
        off = _best_offset(ctx, s.get("text", ""))
        if off is None:
            continue
        url = _anchor_for(off, s["anchors"])
        if url:
            s["url"] = url
