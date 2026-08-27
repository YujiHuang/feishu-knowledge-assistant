"""FastAPI 入口：python -m app.main 或 uvicorn app.main:app --port 8787"""
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .config import load_config, _from_env
from .feishu.auth import FeishuAuth, AuthError
from .feishu.client import FeishuClient
from .feishu.sync import Syncer
from .index.chunker import chunk_document
from .index.embedder import build_embedder
from .index.store import ChunkStore
from .rag.answer import AnswerEngine
from .rag.retriever import Retriever
from .rag.websearch import WebSearcher

cfg = load_config()
auth = FeishuAuth(
    cfg.get("feishu", "app_id"), cfg.get("feishu", "app_secret"),
    cfg.get("feishu", "scopes"), cfg.redirect_uri, cfg.data_dir,
)
client = FeishuClient(auth, data_dir=cfg.data_dir)
store = ChunkStore(cfg.data_dir)
print("加载 embedding 模型（首次运行会下载，约 100MB）...")
embedder = build_embedder(cfg)
syncer = Syncer(client, store, chunk_document, embedder, cfg, cfg.data_dir)
engine = AnswerEngine(Retriever(store, embedder, cfg), WebSearcher(cfg), cfg)

# 启动时就把 LLM 配置摊开：key 走环境变量后，最常见的故障是「换了终端窗口忘了 export」，
# 那样要等提问失败才发现，这里提前一步说清楚。
_key_src = ("环境变量" if _from_env(("llm", "api_key"))
            else "config.yaml" if cfg.get("llm", "api_key") else "")
print(f"LLM: {cfg.get('llm', 'model')} @ {cfg.get('llm', 'base_url')}"
      + (f"，key 来自{_key_src}" if _key_src
         else "，⚠️ 没找到 api_key！先在本终端执行 "
              'read -rs "DEEPSEEK_API_KEY?API Key: " && export DEEPSEEK_API_KEY'))

app = FastAPI(title="知识助手 M1")
_INDEX_HTML = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


def _auto_sync_loop():
    """定时自动同步：sync.auto_sync_minutes 分钟间隔，0 或缺省为关闭。"""
    while True:
        time.sleep(60)
        try:
            cfg._d = load_config()._d   # 热加载配置
            minutes = cfg.get("sync", "auto_sync_minutes", default=0) or 0
            if minutes <= 0 or not auth.authorized or syncer.progress.running:
                continue
            if time.time() - syncer.state.last_sync >= minutes * 60:
                syncer.run_async()
        except Exception:  # noqa: 定时器永不退出
            pass


threading.Thread(target=_auto_sync_loop, daemon=True).start()


class AskRequest(BaseModel):
    question: str
    projects: list[str] | None = None   # 为空/None 表示全部项目
    history: list[dict] | None = None   # 多轮追问：[{q, a}, ...] 最近几轮


class AddDocRequest(BaseModel):
    url: str


@app.get("/", response_class=HTMLResponse)
def index():
    return _INDEX_HTML


@app.get("/anchor_test", response_class=HTMLResponse)
def anchor_test(url: str = ""):
    """【实验】块锚点定位效果验证：列出文档所有块 + 锚点跳转链接。"""
    import html as _html
    import re as _re
    from .feishu.client import parse_doc_link, _block_to_line

    form = ('<form><input name="url" style="width:60%;padding:6px" '
            f'value="{_html.escape(url)}" placeholder="粘贴 /docx/ 或 /wiki/ 文档链接">'
            '<button>列出块</button></form>')
    head = ('<html><head><meta charset="utf-8"><title>锚点定位测试</title>'
            '<style>body{font-family:sans-serif;max-width:900px;margin:24px auto}'
            'td{border-bottom:1px solid #eee;padding:6px;font-size:13px}</style>'
            '</head><body><h3>块锚点定位测试（实验）</h3>' + form)
    if not url:
        return HTMLResponse(head + "</body></html>")
    if not auth.authorized:
        return HTMLResponse(head + "<p>请先在主页连接飞书</p></body></html>")
    parsed = parse_doc_link(url)
    if not parsed or parsed[0] == "unsupported":
        return HTMLResponse(head + "<p>仅支持 /docx/、/wiki/、/base/ 链接</p></body></html>")
    kind, token = parsed
    wiki_url = None
    try:
        base = _re.match(r"(https://[^/]+)", url).group(1)
        if kind == "wiki":
            node = client.get_wiki_node(token)
            wiki_url = url.split("#")[0].split("?")[0]
            if node.get("obj_type") == "bitable":
                kind, token = "bitable", node["obj_token"]
            elif node.get("obj_type") == "docx":
                kind, token = "docx", node["obj_token"]
            else:
                return HTMLResponse(head + "<p>该 wiki 节点类型暂不支持测试</p></body></html>")

        # ---- 多维表格：记录级深链测试 ----
        if kind == "bitable":
            rows = []
            for tbl in client._paged(f"/bitable/v1/apps/{token}/tables",
                                     {"page_size": 100}):
                tid = tbl["table_id"]
                # 取默认视图：带 view 参数的记录深链才能稳定弹出记录卡片
                view_id = ""
                try:
                    for v in client._paged(
                            f"/bitable/v1/apps/{token}/tables/{tid}/views",
                            {"page_size": 20}):
                        view_id = v.get("view_id") or ""
                        break
                except Exception:
                    pass
                vq = f"&view={view_id}" if view_id else ""
                rows.append(f"<tr><td colspan=2><b>表：{_html.escape(tbl.get('name') or tid)}"
                            f"</b>（view={view_id or '未获取'}）</td></tr>")
                n = 0
                for rec in client._paged(
                        f"/bitable/v1/apps/{token}/tables/{tid}/records",
                        {"page_size": 20}):
                    rid = rec.get("record_id")
                    fields = rec.get("fields") or {}
                    label = " | ".join(str(v)[:20] for v in list(fields.values())[:2])
                    links = (f'<a href="{base}/base/{token}?table={tid}{vq}&record={rid}" '
                             f'target="_blank">base 记录定位</a>')
                    if wiki_url:
                        links += (f' ｜ <a href="{wiki_url}?table={tid}{vq}&record={rid}" '
                                  f'target="_blank">wiki 记录定位</a>')
                    rows.append(f"<tr><td>{_html.escape(label)}</td><td>{links}</td></tr>")
                    n += 1
                    if n >= 5:
                        break
            tip = "<p>点「记录定位」：应打开多维表格并弹出/定位到该行的记录卡片。</p>"
            return HTMLResponse(head + tip + "<table>" + "".join(rows)
                                + "</table></body></html>")

        # ---- 文档：块锚点测试 ----
        doc_id = token
        docx_url = f"{base}/docx/{doc_id}"
        rows = []
        for block in client._paged(f"/docx/v1/documents/{doc_id}/blocks",
                                   {"page_size": 500}):
            line = _block_to_line(block)
            bid = block.get("block_id")
            if not line or not bid:
                continue
            links = f'<a href="{docx_url}#{bid}" target="_blank">docx 定位</a>'
            if wiki_url:
                links += f' ｜ <a href="{wiki_url}#{bid}" target="_blank">wiki 定位</a>'
            rows.append(f"<tr><td>{_html.escape(line[:70])}</td><td>{links}</td></tr>")
            if len(rows) >= 60:
                rows.append("<tr><td colspan=2>（仅展示前 60 块）</td></tr>")
                break
        table = "<table>" + "".join(rows) + "</table>"
        tip = "<p>点右侧链接：应打开飞书并滚动定位到对应块（有短暂闪烁标识）。</p>"
        return HTMLResponse(head + tip + table + "</body></html>")
    except Exception as e:  # noqa
        return HTMLResponse(head + f"<p>出错：{_html.escape(str(e))}</p></body></html>")


@app.get("/api/status")
def status():
    counts = store.counts()
    return {
        "authorized": auth.authorized,
        "granted_scope": auth.granted_scope,
        "docs": counts["docs"],
        "chunks": counts["chunks"],
        "last_sync": syncer.state.last_sync,
        "web_search_enabled": engine.web.enabled,
        "sync": syncer.progress.as_dict(),
    }


@app.get("/auth/start")
def auth_start():
    return RedirectResponse(auth.authorize_url())


@app.get("/callback")
def auth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h3>授权被拒绝：{error}</h3>")
    try:
        auth.handle_callback(code, state)
    except AuthError as e:
        return HTMLResponse(f"<h3>授权失败：{e}</h3>")
    return RedirectResponse("/")


@app.post("/api/sync")
def start_sync():
    if not auth.authorized:
        return JSONResponse({"error": "请先连接飞书"}, status_code=400)
    # 每次同步前重载配置：修改 sync/retrieval 配置后无需重启服务
    try:
        cfg._d = load_config()._d
    except Exception as e:  # noqa
        return JSONResponse({"error": f"config.yaml 解析失败：{e}"}, status_code=400)
    started = syncer.run_async()
    return {"started": started, "message": "同步已在后台开始" if started else "已有同步在进行中"}


@app.get("/api/projects")
def projects():
    """已收录文档的项目分布：[{name, docs}]。"""
    counts: dict[str, int] = {}
    for meta in syncer.state.docs.values():
        p = meta.get("project") or "未分类"
        counts[p] = counts.get(p, 0) + 1
    return sorted(({"name": k, "docs": v} for k, v in counts.items()),
                  key=lambda x: -x["docs"])


@app.get("/api/docs")
def docs_list():
    """已收录文档清单（供检查非正式文档混入）。"""
    items = [{"title": m.get("title"), "url": m.get("url"),
              "project": m.get("project") or "未分类",
              "source": m.get("source"), "mtime": m.get("mtime") or 0}
             for m in syncer.state.docs.values()]
    return sorted(items, key=lambda x: (x["project"], -x["mtime"]))


@app.get("/api/media/{token}")
def media(token: str):
    """附件（截图/录屏）代理：向飞书换临时下载链接并跳转。"""
    if not auth.authorized:
        return JSONResponse({"error": "请先连接飞书"}, status_code=401)
    try:
        return RedirectResponse(client.get_media_tmp_url(token))
    except Exception as e:  # noqa
        return JSONResponse({"error": f"附件获取失败：{e}"}, status_code=404)


@app.post("/api/add_doc")
def add_doc(req: AddDocRequest):
    if not auth.authorized:
        return JSONResponse({"error": "请先连接飞书"}, status_code=400)
    try:
        result = syncer.add_doc_by_url(req.url)
    except AuthError as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    return result


@app.post("/api/ask")
def ask(req: AskRequest):
    q = req.question.strip()
    if not q:
        return JSONResponse({"error": "问题为空"}, status_code=400)
    if store.counts()["chunks"] == 0:
        return JSONResponse({"error": "知识库为空，请先连接飞书并完成同步"}, status_code=400)
    t0 = time.time()
    try:
        result = engine.ask(q, req.projects or None, req.history or None)
    except AuthError as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    except Exception as e:  # noqa
        # 终端里也留一份完整栈：只看 uvicorn 那行 500 没法排查
        print(f"\n[/api/ask] 问题：{q}")
        traceback.print_exc()
        return JSONResponse({"error": f"回答生成失败：{e}"}, status_code=500)
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    import uvicorn
    uvicorn.run(app,
                host=cfg.get("server", "host", default="127.0.0.1"),
                port=cfg.get("server", "port", default=8787))


if __name__ == "__main__":
    main()
