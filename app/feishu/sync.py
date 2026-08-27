"""同步管线：枚举可见文档 → 增量拉取 → 切块 → 向量化 → 入库。

同步状态存 data_dir/sync_state.json：{doc_token: {edit_time, title, url, source}}
"""
import json
import re
import threading
import time
from pathlib import Path

from .client import FeishuClient, FeishuAPIError, parse_doc_link

_META_KEYS = ("edit_time", "title", "url", "source", "project", "mtime", "kind")
_WIKI_KINDS = {"docx": "docx", "bitable": "bitable", "sheet": "sheet"}
# 解析逻辑升级时 +1：版本变化会触发下次同步全量重新解析入库
PARSER_VERSION = 7


def _chunk_url(base_url: str, anchor: str) -> str:
    """把切块锚点转成定位链接。
    b:block_id → 文档 URL#block_id（docx/wiki 均可滚动定位）；
    r:app:table:record:view → base 直链记录深链（wiki 壳转发不稳，统一用 base 形式）。"""
    if not anchor:
        return base_url
    if anchor.startswith("b:"):
        return base_url.split("#")[0] + "#" + anchor[2:]
    if anchor.startswith("r:"):
        parts = anchor.split(":")
        if len(parts) != 5:
            return base_url
        _, app, tid, rid, vid = parts
        m = re.match(r"(https://[^/]+)", base_url)
        domain = m.group(1) if m else "https://feishu.cn"
        vq = f"&view={vid}" if vid else ""
        return f"{domain}/base/{app}?table={tid}{vq}&record={rid}"
    return base_url


def _parse_ts(v) -> int:
    """把飞书返回的编辑时间（秒或毫秒，str/int）解析为 unix 秒；不可解析返回 0。"""
    try:
        t = int(str(v))
        return t // 1000 if t > 10**12 else t
    except (TypeError, ValueError):
        return 0


def _meta_sig(m: dict) -> tuple:
    """变更签名：编辑时间之外，目录移动（project）、改名、URL 变化也触发重建。"""
    return (m.get("edit_time"), m.get("title"), m.get("project"), m.get("url"))


class SyncState:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "sync_state.json"
        self.docs: dict = {}
        self.last_sync: float = 0
        self.parser_version: int = 0
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.docs = d.get("docs", {})
                self.last_sync = d.get("last_sync", 0)
                self.parser_version = d.get("parser_version", 0)
            except Exception:
                pass

    def save(self):
        self.path.write_text(json.dumps(
            {"docs": self.docs, "last_sync": self.last_sync,
             "parser_version": self.parser_version}, ensure_ascii=False))


class SyncProgress:
    def __init__(self):
        self.running = False
        self.phase = "idle"       # idle|enumerate|fetch|done|error
        self.total = 0
        self.done = 0
        self.removed = 0
        self.current = ""
        self.errors: list[str] = []

    def as_dict(self):
        return {"running": self.running, "phase": self.phase, "total": self.total,
                "done": self.done, "removed": self.removed,
                "current": self.current, "errors": self.errors[-10:]}


class Syncer:
    def __init__(self, client: FeishuClient, store, chunker, embedder, cfg, data_dir: Path):
        self.client = client
        self.store = store            # index.store.ChunkStore
        self.chunker = chunker        # index.chunker.chunk_document
        self.embedder = embedder
        self.cfg = cfg
        self.state = SyncState(data_dir)
        self.progress = SyncProgress()
        self._lock = threading.Lock()

    # ---------- 入口 ----------
    def run_async(self):
        if self.progress.running:
            return False
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def run(self):
        with self._lock:
            p = self.progress
            p.__init__()
            p.running = True
            try:
                p.phase = "enumerate"
                docs = self._enumerate()
                excludes = set(self.cfg.get("sync", "exclude_tokens", default=[]) or [])
                docs = {k: v for k, v in docs.items() if k not in excludes}
                force_all = self.state.parser_version != PARSER_VERSION
                changed = [t for t, m in docs.items()
                           if force_all or
                           _meta_sig(m) != _meta_sig(self.state.docs.get(t, {}))]
                removed = [t for t in self.state.docs if t not in docs]

                p.phase = "fetch"
                p.total = len(changed)
                for token in changed:
                    meta = docs[token]
                    p.current = meta["title"]
                    try:
                        self._index_one(token, meta)
                        self.state.docs[token] = {k: meta.get(k) for k in _META_KEYS}
                    except FeishuAPIError as e:
                        p.errors.append(f"{meta['title']}: {e}")
                    p.done += 1
                    if p.done % 20 == 0:
                        self.state.save()
                for token in removed:
                    self.store.delete_doc(token)
                    self.state.docs.pop(token, None)
                p.removed = len(removed)

                self.state.last_sync = time.time()
                self.state.parser_version = PARSER_VERSION
                self.state.save()
                self.store.rebuild_keyword_index()
                p.phase = "done"
            except Exception as e:  # noqa
                p.errors.append(str(e))
                p.phase = "error"
            finally:
                p.running = False

    # ---------- 枚举 ----------
    def _enumerate(self) -> dict:
        """返回 {doc_token: {title, url, edit_time, source, project, mtime}}，只收新版文档 docx。"""
        found: dict = {}
        excludes = set(self.cfg.get("sync", "exclude_tokens", default=[]) or [])
        self._title_prefixes = tuple(
            self.cfg.get("sync", "exclude_title_prefixes", default=[]) or [])
        self._title_keywords = list(
            self.cfg.get("sync", "exclude_title_keywords", default=[]) or [])

        if self.cfg.get("sync", "include_my_space", default=True):
            self._walk_folder("", found, excludes, project="我的空间")

        if self.cfg.get("sync", "include_wiki", default=True):
            allow = {str(x) for x in
                     (self.cfg.get("sync", "wiki_space_allowlist", default=[]) or [])}
            for space in self.client.list_wiki_spaces():
                sid = space.get("space_id")
                if allow and str(sid) not in allow:
                    continue
                space_name = space.get("name") or "知识库"
                # 一级目录名作为项目名；顶层散文档归到空间名下
                for node in self.client.list_wiki_nodes(sid, ""):
                    ntoken = node.get("node_token")
                    if ntoken in excludes:
                        continue
                    top_title = node.get("title") or "(untitled)"
                    subtree_project = top_title if node.get("has_child") else space_name
                    self._collect_wiki_node(node, found, excludes, subtree_project)
                    if node.get("has_child"):
                        self._walk_wiki(sid, ntoken, found, excludes, top_title)

        for url in self._all_extra_urls():
            try:
                self._resolve_extra(url, found)
            except FeishuAPIError as e:
                self.progress.errors.append(f"链接 {url}: {e}")
        return found

    # ---------- 手动添加的链接 ----------
    def _extra_file(self) -> Path:
        return self.state.path.parent / "extra_docs.json"

    def _all_extra_urls(self) -> list[str]:
        urls = list(self.cfg.get("sync", "extra_doc_urls", default=[]) or [])
        f = self._extra_file()
        if f.exists():
            try:
                urls += [u for u in json.loads(f.read_text()) if u not in urls]
            except Exception:
                pass
        return urls

    def _resolve_extra(self, url: str, found: dict):
        parsed = parse_doc_link(url)
        if not parsed or parsed[0] == "unsupported":
            return
        kind, token = parsed
        import time as _time
        daily = "daily-" + _time.strftime("%Y-%m-%d")   # 无编辑时间的类型至多每天刷新一次
        if kind == "wiki":
            node = self.client.get_wiki_node(token)
            nkind = _WIKI_KINDS.get(node.get("obj_type"))
            if nkind is None:
                return
            found[node["obj_token"]] = {
                "title": node.get("title") or "(untitled)", "url": url,
                "edit_time": str(node.get("obj_edit_time", "")),
                "mtime": _parse_ts(node.get("obj_edit_time")),
                "project": "手动添加", "kind": nkind, "source": "extra"}
        elif kind == "bitable":
            info = self.client.get_bitable_info(token)
            found[token] = {
                "title": info.get("name") or "(untitled)", "url": url,
                "edit_time": daily, "mtime": 0,
                "project": "手动添加", "kind": "bitable", "source": "extra"}
        elif kind == "sheet":
            info = self.client.get_sheet_info(token)
            found[token] = {
                "title": info.get("title") or "(untitled)", "url": url,
                "edit_time": daily, "mtime": 0,
                "project": "手动添加", "kind": "sheet", "source": "extra"}
        else:
            info = self.client.get_doc_info(token)
            found[token] = {
                "title": info.get("title") or "(untitled)", "url": url,
                "edit_time": str(info.get("revision_id", "")),
                "mtime": 0,
                "project": "手动添加", "kind": "docx", "source": "extra"}

    def add_doc_by_url(self, url: str) -> dict:
        """粘贴链接立即入库。返回 {ok, message}。"""
        url = url.strip()
        parsed = parse_doc_link(url)
        if not parsed:
            return {"ok": False, "message": "无法识别的链接，请提供 /docx/ 或 /wiki/ 链接"}
        if parsed[0] == "unsupported":
            return {"ok": False,
                    "message": "该链接是旧版文档/思维笔记/文件等类型，暂不支持入库"}
        found: dict = {}
        try:
            self._resolve_extra(url, found)
        except FeishuAPIError as e:
            if e.code == 1770032 or "forbidden" in str(e).lower():
                return {"ok": False, "message": "你没有这篇文档的阅读权限"}
            return {"ok": False, "message": f"读取失败：{e}"}
        if not found:
            return {"ok": False, "message": "该 wiki 节点类型暂不支持（支持文档/多维表格/电子表格）"}
        token, meta = next(iter(found.items()))
        try:
            self._index_one(token, meta)
        except FeishuAPIError as e:
            return {"ok": False, "message": f"内容拉取失败：{e}"}
        self.state.docs[token] = {k: meta.get(k) for k in _META_KEYS}
        self.state.save()
        # 持久化链接，之后每次同步都会检查更新
        urls = self._all_extra_urls()
        if url not in urls:
            urls.append(url)
        self._extra_file().write_text(json.dumps(urls, ensure_ascii=False))
        self.store.rebuild_keyword_index()
        return {"ok": True, "message": f"已入库：{meta['title']}"}

    def _title_excluded(self, title: str) -> bool:
        if self._title_prefixes and title.startswith(self._title_prefixes):
            return True
        return any(kw in title for kw in self._title_keywords)

    def _walk_folder(self, folder_token: str, found: dict, excludes: set, project: str):
        if folder_token in excludes:
            return
        for f in self.client.list_drive_files(folder_token):
            token, ftype = f.get("token"), f.get("type")
            if token in excludes:
                continue
            # 快捷方式：解析目标对象（用于持续收录别人分享的文档）
            if ftype == "shortcut":
                si = f.get("shortcut_info") or {}
                if si.get("target_type") == "docx" and si.get("target_token") not in excludes:
                    token, ftype = si["target_token"], "docx"
                else:
                    continue
            if ftype == "folder":
                # 顶层文件夹名作为项目名；子文件夹沿用父项目
                sub_project = f.get("name") if project == "我的空间" else project
                self._walk_folder(token, found, excludes, sub_project or project)
            elif ftype in ("docx", "bitable", "sheet"):
                name = f.get("name") or "(untitled)"
                if self._title_excluded(name):
                    continue
                found[token] = {
                    "title": name,
                    "url": f.get("url") or f"https://feishu.cn/docx/{token}",
                    "edit_time": str(f.get("modified_time", "")),
                    "mtime": _parse_ts(f.get("modified_time")),
                    "project": project,
                    "kind": ftype,
                    "source": "drive"}

    def _collect_wiki_node(self, node: dict, found: dict, excludes: set, project: str):
        """wiki 节点若是 docx/多维表格/电子表格 且未被排除则收录。"""
        kind = _WIKI_KINDS.get(node.get("obj_type"))
        if kind is None or node.get("obj_token") in excludes:
            return
        title = node.get("title") or "(untitled)"
        if self._title_excluded(title):
            return
        found[node["obj_token"]] = {
            "title": title,
            "url": f"https://feishu.cn/wiki/{node.get('node_token')}",
            "edit_time": str(node.get("obj_edit_time", "")),
            "mtime": _parse_ts(node.get("obj_edit_time")),
            "project": project,
            "kind": kind,
            "source": "wiki"}

    def _walk_wiki(self, space_id: str, parent: str, found: dict, excludes: set,
                   project: str = ""):
        for node in self.client.list_wiki_nodes(space_id, parent):
            ntoken = node.get("node_token")
            if ntoken in excludes:
                continue
            self._collect_wiki_node(node, found, excludes, project or "知识库")
            if node.get("has_child"):
                self._walk_wiki(space_id, ntoken, found, excludes, project)

    # ---------- 单篇入库 ----------
    def _index_one(self, doc_token: str, meta: dict):
        kind = meta.get("kind") or "docx"
        if kind == "bitable":
            text = self.client.get_bitable_text(doc_token)
        elif kind == "sheet":
            text = self.client.get_sheet_text(doc_token)
        else:
            text = self.client.get_doc_markdown(doc_token)
        chunks = self.chunker(meta["title"], text)
        if not chunks:
            self.store.delete_doc(doc_token)
            return
        vectors = self.embedder.encode([c["embed_text"] for c in chunks])
        rows = [{
            "id": f"{doc_token}#{i}",
            "doc_token": doc_token,
            "title": meta["title"],
            "title_path": c["title_path"],
            "url": _chunk_url(meta["url"], c.get("anchor", "")),   # 块级定位链接
            "doc_url": meta["url"],                                # 文档级链接（来源聚合用）
            # 块内每个锚点的偏移与定位链接：答案里的某一句可精确定位到对应行/记录
            "anchors": json.dumps(
                [[off, _chunk_url(meta["url"], a)] for off, a in c.get("anchors", [])],
                ensure_ascii=False),
            "project": meta.get("project") or "未分类",
            "mtime": int(meta.get("mtime") or 0),
            "text": c["text"],
            "vector": v,
        } for i, (c, v) in enumerate(zip(chunks, vectors))]
        self.store.replace_doc(doc_token, rows)
