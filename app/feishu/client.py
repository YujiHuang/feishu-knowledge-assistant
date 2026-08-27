"""飞书 OpenAPI 客户端：drive / wiki / docx，带限频与重试。"""
import json
import re
import time
import urllib.parse
from pathlib import Path

import httpx

from .auth import FeishuAuth

API = "https://open.feishu.cn/open-apis"


class FeishuAPIError(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(f"feishu api error code={code}: {msg}")


class FeishuClient:
    def __init__(self, auth: FeishuAuth, qps: float = 4.0, data_dir: Path | None = None):
        self.auth = auth
        self._min_interval = 1.0 / qps
        self._last_call = 0.0
        # 多维表格附件 token → 下载所需的 extra 权限参数（bitablePerm）
        self._media_file = (data_dir / "media_map.json") if data_dir else None
        self.media_extra: dict[str, str] = {}
        if self._media_file and self._media_file.exists():
            try:
                self.media_extra = json.loads(self._media_file.read_text())
            except Exception:
                self.media_extra = {}

    def _register_media(self, att: dict):
        """从附件字段自带的下载 URL 里提取 extra 参数并持久化。"""
        token = att.get("file_token")
        if not token:
            return
        for key in ("url", "tmp_url"):
            u = att.get(key) or ""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            if qs.get("extra"):
                if self.media_extra.get(token) != qs["extra"][0]:
                    self.media_extra[token] = qs["extra"][0]
                    if self._media_file:
                        self._media_file.write_text(
                            json.dumps(self.media_extra, ensure_ascii=False))
                return

    # ---------- 基础 ----------
    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            wait = self._min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            resp = httpx.get(
                API + path, params=params or {},
                headers={"Authorization": f"Bearer {self.auth.access_token()}"},
                timeout=30,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"code": -1, "msg": resp.text[:200]}
            code = data.get("code")
            if code == 0:
                return data.get("data", {})
            if code in (99991400,) or resp.status_code == 429:  # 限频，指数退避
                time.sleep(2 ** attempt)
                continue
            raise FeishuAPIError(code, data.get("msg"))
        raise FeishuAPIError(99991400, "rate limited after retries")

    def _paged(self, path: str, params: dict, item_key: str = "items"):
        page_token = ""
        while True:
            p = dict(params)
            if page_token:
                p["page_token"] = page_token
            data = self._get(path, p)
            yield from (data.get(item_key) or data.get("files") or [])
            if not data.get("has_more"):
                return
            page_token = data.get("page_token") or data.get("next_page_token") or ""
            if not page_token:
                return

    # ---------- drive（我的空间） ----------
    def list_drive_files(self, folder_token: str = ""):
        """遍历云盘文件夹，返回 (file dict) 迭代器。type: docx/folder/sheet/..."""
        params = {"page_size": 200}
        if folder_token:
            params["folder_token"] = folder_token
        yield from self._paged("/drive/v1/files", params)

    # ---------- wiki（知识库） ----------
    def list_wiki_spaces(self):
        yield from self._paged("/wiki/v2/spaces", {"page_size": 50})

    def list_wiki_nodes(self, space_id: str, parent_node_token: str = ""):
        params = {"page_size": 50}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        yield from self._paged(f"/wiki/v2/spaces/{space_id}/nodes", params)

    def get_wiki_node(self, node_token: str) -> dict:
        return self._get("/wiki/v2/spaces/get_node", {"token": node_token})["node"]

    # ---------- docx ----------
    def get_doc_info(self, document_id: str) -> dict:
        """文档元信息（title、revision_id 等）。"""
        return self._get(f"/docx/v1/documents/{document_id}")["document"]

    def get_doc_raw(self, document_id: str) -> str:
        return self._get(f"/docx/v1/documents/{document_id}/raw_content")["content"]

    def get_doc_markdown(self, document_id: str) -> str:
        """拉取 blocks 并转成 Markdown 风格文本（保留标题层级）。
        每行末尾附带不可见锚点标记（\\x00b:block_id\\x00），供切块后生成定位链接。
        文档内嵌入的多维表格会按行展开（每行带字段名）。失败回退纯文本。"""
        try:
            lines = []
            for block in self._paged(
                f"/docx/v1/documents/{document_id}/blocks", {"page_size": 500}
            ):
                bid = block.get("block_id")
                mark = f"\x00b:{bid}\x00" if bid else ""
                bt = block.get("bitable")
                if isinstance(bt, dict) and bt.get("token"):
                    sub = self._bitable_lines(bt["token"])
                    if sub and mark:
                        sub[1] = sub[1] + mark   # 表头行锚到嵌入块
                    lines.extend(sub)
                    continue
                sh = block.get("sheet")
                if isinstance(sh, dict) and sh.get("token"):
                    sub = self._embedded_sheet_lines(sh["token"])
                    if sub and mark:
                        sub[1] = sub[1] + mark
                    lines.extend(sub)
                    continue
                # 图片/文件附件（截图、录屏等）→ 本地可点击链接
                img = block.get("image")
                if isinstance(img, dict) and img.get("token"):
                    lines.append(f"【附件·图片】/api/media/{img['token']}" + mark)
                    continue
                fl = block.get("file")
                if isinstance(fl, dict) and fl.get("token"):
                    lines.append(
                        f"【附件·{fl.get('name') or '文件'}】/api/media/{fl['token']}" + mark)
                    continue
                line = _block_to_line(block)
                if line is not None:
                    lines.append(line + mark)
            text = "\n".join(lines).strip()
            return text or self.get_doc_raw(document_id)
        except FeishuAPIError:
            return self.get_doc_raw(document_id)

    # ---------- bitable（嵌入文档的多维表格） ----------
    MAX_BITABLE_ROWS = 1000

    def _first_view(self, app_token: str, table_id: str) -> str:
        """取数据表默认视图 ID（记录深链带 view 参数才稳定）。失败返回空。"""
        try:
            for v in self._paged(
                    f"/bitable/v1/apps/{app_token}/tables/{table_id}/views",
                    {"page_size": 20}):
                return v.get("view_id") or ""
        except FeishuAPIError:
            pass
        return ""

    def _bitable_rows(self, app_token: str, table_id: str,
                      view_id: str = "") -> list[str]:
        """按行展开一张数据表：- 字段: 值 | ...，行末附记录锚点标记。"""
        lines, n = [], 0
        for rec in self._paged(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            {"page_size": 500},
        ):
            cells = []
            for k, v in (rec.get("fields") or {}).items():
                val = flatten_bitable_value(v, self._register_media)
                if val:
                    cells.append(f"{k}: {val}")
            if cells:
                rid = rec.get("record_id") or ""
                mark = f"\x00r:{app_token}:{table_id}:{rid}:{view_id}\x00" if rid else ""
                lines.append("- " + " | ".join(cells) + mark)
                n += 1
            if n >= self.MAX_BITABLE_ROWS:
                lines.append(f"-（行数过多，已截断至 {self.MAX_BITABLE_ROWS} 行）")
                break
        return lines

    def _bitable_lines(self, token: str) -> list[str]:
        """嵌入块 token 格式为 app_token_table_id，按行展开为文本。"""
        app_token, _, table_id = token.partition("_")
        try:
            if not table_id:
                tables = list(self._paged(
                    f"/bitable/v1/apps/{app_token}/tables", {"page_size": 100}))
                if not tables:
                    return []
                table_id = tables[0]["table_id"]
            rows = self._bitable_rows(app_token, table_id,
                                      self._first_view(app_token, table_id))
            return ["", "【嵌入多维表格】"] + rows if rows else []
        except FeishuAPIError as e:
            if e.code == 99991679:
                return ["", "【嵌入多维表格：缺少 bitable:app:readonly 权限，内容未解析】"]
            if e.code == 1254302:
                return ["", "【嵌入多维表格：该表开启了高级权限，无法读取】"]
            return ["", f"【嵌入多维表格读取失败：code={e.code}】"]

    # ---------- 素材（截图/录屏等附件） ----------
    def get_media_tmp_url(self, file_token: str) -> str:
        """换取素材的临时下载链接（有效期短，即取即用）。
        多维表格附件需携带 extra（bitablePerm）权限参数。"""
        params = {"file_tokens": file_token}
        extra = self.media_extra.get(file_token)
        if extra:
            params["extra"] = extra
        data = self._get("/drive/v1/medias/batch_get_tmp_download_url", params)
        urls = data.get("tmp_download_urls") or []
        if not urls:
            raise FeishuAPIError(-1, "未获取到临时下载链接"
                                 + ("" if extra else "（该附件可能来自多维表格且缺少权限参数，"
                                    "请重新同步一次以重建附件映射）"))
        return urls[0]["tmp_download_url"]

    # ---------- 独立多维表格（/base/ 或 wiki bitable 节点） ----------
    def get_bitable_info(self, app_token: str) -> dict:
        return self._get(f"/bitable/v1/apps/{app_token}")["app"]

    def get_bitable_text(self, app_token: str) -> str:
        """整个多维表格 → 每张数据表一节，按行展开。"""
        lines = []
        for tbl in self._paged(f"/bitable/v1/apps/{app_token}/tables",
                               {"page_size": 100}):
            tid = tbl["table_id"]
            rows = self._bitable_rows(app_token, tid, self._first_view(app_token, tid))
            if rows:
                lines.append(f"## 表：{tbl.get('name') or tid}")
                lines.extend(rows)
        return "\n".join(lines).strip()

    # ---------- 电子表格（sheet） ----------
    MAX_SHEET_ROWS = 1000
    MAX_SHEET_COLS = 52

    def get_sheet_info(self, spreadsheet_token: str) -> dict:
        return self._get(f"/sheets/v3/spreadsheets/{spreadsheet_token}")["spreadsheet"]

    def get_sheet_text(self, spreadsheet_token: str) -> str:
        """整个电子表格 → 每个工作表一节；首行视为表头，按行展开。"""
        data = self._get(f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query")
        lines = []
        for sh in data.get("sheets") or []:
            if sh.get("hidden") or sh.get("resource_type") != "sheet":
                continue
            rows = self._sheet_rows(spreadsheet_token, sh)
            if rows:
                lines.append(f"## 表：{sh.get('title') or sh.get('sheet_id')}")
                lines.extend(rows)
        return "\n".join(lines).strip()

    def _sheet_rows(self, ss_token: str, sheet_meta: dict) -> list[str]:
        gp = sheet_meta.get("grid_properties") or {}
        n_rows = min(int(gp.get("row_count") or 200), self.MAX_SHEET_ROWS + 1)
        n_cols = min(int(gp.get("column_count") or 20), self.MAX_SHEET_COLS)
        rng = f"{sheet_meta['sheet_id']}!A1:{_col_letter(n_cols)}{n_rows}"
        vr = self._get(
            f"/sheets/v2/spreadsheets/{ss_token}/values/{rng}",
            {"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"},
        )
        values = ((vr.get("valueRange") or {}).get("values")) or []
        if len(values) < 2:
            return []
        header = [flatten_bitable_value(c) or f"列{i+1}" for i, c in enumerate(values[0])]
        lines = []
        for row in values[1:]:
            cells = []
            for i, cell in enumerate(row):
                val = flatten_bitable_value(cell)
                if val:
                    cells.append(f"{header[i] if i < len(header) else f'列{i+1}'}: {val}")
            if cells:
                lines.append("- " + " | ".join(cells))
        return lines

    def _embedded_sheet_lines(self, token: str) -> list[str]:
        """文档内嵌电子表格块，token 格式 spreadsheet_token_sheet_id。"""
        ss_token, _, sheet_id = token.partition("_")
        try:
            data = self._get(f"/sheets/v3/spreadsheets/{ss_token}/sheets/query")
            lines = []
            for sh in data.get("sheets") or []:
                if sheet_id and sh.get("sheet_id") != sheet_id:
                    continue
                rows = self._sheet_rows(ss_token, sh)
                if rows:
                    lines.extend(["", "【嵌入电子表格】"] + rows)
            return lines
        except FeishuAPIError as e:
            if e.code == 99991679:
                return ["", "【嵌入电子表格：缺少 sheets:spreadsheet:readonly 权限，内容未解析】"]
            return ["", f"【嵌入电子表格读取失败：code={e.code}】"]


def _col_letter(n: int) -> str:
    """1→A, 26→Z, 27→AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def flatten_bitable_value(v, register=None) -> str:
    """把 bitable 字段值（字符串/数字/对象/数组等）压平成一段文本。
    register: 附件回调，用于登记下载权限参数。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        # 附件（截图/录屏）：保留 token，转成本地可点击路径
        if v.get("file_token"):
            if register:
                register(v)
            return f"{v.get('name') or '附件'}（/api/media/{v['file_token']}）"
        for key in ("text", "name", "full_address", "link"):
            if v.get(key):
                return str(v[key])
        return ""
    if isinstance(v, list):
        parts = [flatten_bitable_value(x, register) for x in v]
        return "，".join(p for p in parts if p)
    return str(v)


# ---------- block → 文本 ----------
_HEADING = re.compile(r"^heading(\d)$")
_TEXT_KEYS = ("text", "bullet", "ordered", "code", "quote", "todo",
              "heading1", "heading2", "heading3", "heading4", "heading5",
              "heading6", "heading7", "heading8", "heading9")


def _elements_text(payload: dict) -> str:
    parts = []
    for el in payload.get("elements", []):
        run = el.get("text_run")
        if run and run.get("content"):
            parts.append(run["content"])
        mention = el.get("mention_doc")
        if mention and mention.get("title"):
            parts.append(mention["title"])
    return "".join(parts).strip()


def _block_to_line(block: dict) -> str | None:
    for key in _TEXT_KEYS:
        payload = block.get(key)
        if isinstance(payload, dict) and "elements" in payload:
            txt = _elements_text(payload)
            if not txt:
                return None
            m = _HEADING.match(key)
            if m:
                return "#" * min(int(m.group(1)), 6) + " " + txt
            if key == "bullet":
                return "- " + txt
            if key == "quote":
                return "> " + txt
            return txt
    return None


# ---------- URL 工具 ----------
def doc_url(doc_token: str, base: str = "https://feishu.cn") -> str:
    return f"{base}/docx/{doc_token}"


def wiki_url(node_token: str, base: str = "https://feishu.cn") -> str:
    return f"{base}/wiki/{node_token}"


def parse_doc_link(url: str) -> tuple[str, str] | None:
    """返回 (kind, token)，kind ∈ docx|wiki|bitable|sheet|unsupported。"""
    for kind, pat in (("docx", r"/docx/([A-Za-z0-9]+)"),
                      ("wiki", r"/wiki/([A-Za-z0-9]+)"),
                      ("bitable", r"/base/([A-Za-z0-9]+)"),
                      ("sheet", r"/sheets/([A-Za-z0-9]+)")):
        m = re.search(pat, url)
        if m:
            return kind, m.group(1)
    m = re.search(r"/(docs|mindnotes|file)/([A-Za-z0-9]+)", url)
    if m:
        return "unsupported", m.group(2)
    return None
