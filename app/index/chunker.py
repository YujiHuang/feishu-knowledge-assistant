"""标题感知切块：先按 Markdown 标题分节，再按长度滑窗。
正文行末可携带不可见锚点标记（\\x00...\\x00），切块时提取为 chunk 的 anchor 并从文本中清除。"""
import re

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MARK_RE = re.compile("\x00([^\x00]+)\x00")

CHUNK_SIZE = 600      # 目标块长（字符）
CHUNK_OVERLAP = 100   # 相邻块重叠
MIN_CHUNK = 50        # 过短的碎块丢弃


def _clean(t: str) -> str:
    return _MARK_RE.sub("", t).replace("\x00", "").strip()


def _clean_with_anchors(piece: str) -> tuple[str, list[tuple[int, str]]]:
    """去标记并记录每个锚点在干净文本中的偏移。

    锚点标记位于它所属那一行的行尾，因此偏移 = 该行正文在 clean 中的结束位置：
    锚点 i 覆盖 clean[offset(i-1):offset(i)] 这段文本。引用定位时取
    「第一个 offset > 命中位置」的锚点即可回到那一行对应的飞书块。
    """
    segs: list[str] = []
    anchors: list[tuple[int, str]] = []
    last = n = 0
    for m in _MARK_RE.finditer(piece):
        seg = piece[last:m.start()].replace("\x00", "")
        segs.append(seg)
        n += len(seg)
        anchors.append((n, m.group(1)))
        last = m.end()
    segs.append(piece[last:].replace("\x00", ""))
    raw = "".join(segs)
    # strip() 会平移偏移：左侧裁掉多少就整体减多少，右侧裁掉的锚点收敛到末尾
    left = len(raw) - len(raw.lstrip())
    clean = raw.strip()
    fixed = [(min(len(clean), max(0, off - left)), a) for off, a in anchors]
    return clean, fixed


def chunk_document(doc_title: str, text: str) -> list[dict]:
    """返回 [{text, title_path, embed_text, anchor, anchors}]。

    embed_text = 标题路径 + 正文，让向量携带上下文；text 用于展示与生成；
    anchor  = 块级锚点（片段开头那一行），用于文档级来源链接；
    anchors = [(偏移, 锚点)]，用于把答案里的某一句精确定位到对应的行/记录。
    """
    sections = _split_sections(text)
    chunks = []
    for title_path, body in sections:
        full_path = " > ".join([doc_title] + title_path) if title_path else doc_title
        pieces = _window(body)
        for piece, lead in pieces:
            clean, anchors = _clean_with_anchors(piece)
            # 滑窗产生的碎尾块丢弃；完整的短小节保留
            if len(pieces) > 1 and len(clean) < MIN_CHUNK:
                continue
            chunks.append({
                "text": clean,
                "title_path": full_path,
                "embed_text": f"{full_path}\n{clean}",
                "anchor": _pick_anchor(piece, lead),
                "anchors": anchors,
            })
    # 全文过短的文档至少保留一块
    if not chunks and text.strip():
        clean, anchors = _clean_with_anchors(text)
        clean = clean[:CHUNK_SIZE]
        chunks.append({"text": clean,
                       "title_path": doc_title,
                       "embed_text": f"{doc_title}\n{clean}",
                       "anchor": _pick_anchor(text, 0),
                       "anchors": [(o, a) for o, a in anchors if o <= len(clean)]})
    return chunks


def _pick_anchor(piece: str, lead: int) -> str:
    """选片段锚点：优先取重叠区之后的第一个锚点（重叠区内容属于上一片段，
    取它会导致定位偏到相邻段落）；片段内无独立锚点时才回退到重叠区锚点。"""
    first = ""
    for m in _MARK_RE.finditer(piece):
        if not first:
            first = m.group(1)
        if m.start() >= lead:
            return m.group(1)
    return first


def _split_sections(text: str) -> list[tuple[list[str], str]]:
    """按标题层级切分，返回 [(标题路径, 节内正文)]。标题中的锚点标记会被清除。"""
    path: list[tuple[int, str]] = []   # [(level, title)]
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), _clean(m.group(2))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, title))
            sections.append(([t for _, t in path], []))
        else:
            sections[-1][1].append(line)
    return [(p, "\n".join(lines).strip()) for p, lines in sections
            if "\n".join(lines).strip()]


def _window(body: str) -> list[tuple[str, int]]:
    """返回 [(片段, 开头重叠区长度)]。重叠区是上一片段的尾部内容。"""
    if len(body) <= CHUNK_SIZE:
        return [(body, 0)]
    pieces, start, lead = [], 0, 0
    while start < len(body):
        end = min(start + CHUNK_SIZE, len(body))
        # 尽量在句号/换行处断开
        if end < len(body):
            cut = max(body.rfind("\n", start + MIN_CHUNK, end),
                      body.rfind("。", start + MIN_CHUNK, end))
            if cut > start:
                end = cut + 1
        # 不撕裂锚点标记：若截断点落在 \x00...\x00 内部，延伸到标记结束
        if end < len(body) and body.count("\x00", start, end) % 2 == 1:
            close = body.find("\x00", end)
            end = (close + 1) if close != -1 else len(body)
        pieces.append((body[start:end], lead))
        if end >= len(body):
            break
        new_start = max(end - CHUNK_OVERLAP, start + 1)
        lead = end - new_start   # 下一片段开头有多长是本片段的重复内容
        start = new_start
    return pieces
