"""飞书机器人入口（独立进程）：python -m app.bot

前提：
1. 主服务已在本机运行（python -m app.main），机器人通过 /api/ask 复用同一套检索问答；
2. 飞书后台已为应用开启「机器人」能力；
3. 事件订阅选择「长连接」模式（无需公网地址），订阅 im.message.receive_v1；
4. 权限开通（缺一个就会「@了没反应」，且是静默的）：
   - im:message                        发送消息（回复卡片）
   - im:message.group_at_msg:readonly  接收群聊中 @机器人 的消息事件 ← 群聊必需
   - im:message.p2p_msg:readonly       接收单聊消息事件             ← 单聊必需
5. 以上任何改动（加权限、改事件订阅）都必须去「版本管理与发布」创建新版本并发布，
   否则后台看着是开了、实际不生效。

排查顺序：@ 一次，看终端有没有 [收到] 那行。没有 → 事件没推过来，回去查 3/4/5；
有但没回复 → 看 ⚠️ 回复失败 的 code。单聊能通、群聊不通 = 缺第 4 条里的 group_at_msg。

行为：
- 单聊：直接回答；群聊：被 @ 时回答；
- 回答走 config 里 bot.projects 白名单（空 = 全部项目）；
- 本地附件路径（/api/media/...）对他人无意义，替换为「附件请在来源文档中查看」。
"""
import json
import re
import threading
from collections import deque

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageReactionRequest,
                                 CreateMessageReactionRequestBody,
                                 DeleteMessageReactionRequest, Emoji,
                                 P2ImMessageReceiveV1, PatchMessageRequest,
                                 PatchMessageRequestBody, ReplyMessageRequest,
                                 ReplyMessageRequestBody)

from .config import load_config

cfg = load_config()
ASK_URL = (f"http://{cfg.get('server', 'host', default='127.0.0.1')}:"
           f"{cfg.get('server', 'port', default=8787)}/api/ask")
APP_ID = cfg.get("feishu", "app_id")
APP_SECRET = cfg.get("feishu", "app_secret")

_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_seen: deque = deque(maxlen=500)   # event_id 去重
_hist: dict[str, deque] = {}       # chat_id → 最近几轮 (问, 答)，实现多轮追问记忆
_HIST_MAX = 8                      # 每个会话最多留几轮；实际带进提示词的轮数见 retrieval.history_rounds


def build_card(question: str, history: list | None = None) -> tuple[dict, str]:
    """调主服务问答，返回 (飞书卡片, 原始回答文本)。history 为本会话最近几轮问答。"""
    body = {"question": question}
    projects = cfg.get("bot", "projects", default=[]) or []
    if projects:
        body["projects"] = projects
    if history:
        body["history"] = [{"q": q, "a": a} for q, a in history]
    try:
        r = httpx.post(ASK_URL, json=body, timeout=180).json()
    except Exception as e:  # noqa
        return _md_card(f"⚠️ 知识库服务未响应，请确认主服务已启动（{e}）"), ""
    if r.get("error"):
        return _md_card(f"⚠️ {r['error']}"), ""

    raw_answer = r.get("answer", "")
    # 本地附件链接对其他人不可用 → 指向来源文档
    answer = re.sub(r"[（(]?/api/media/[A-Za-z0-9_\-]+[）)]?",
                    "（附件请在来源文档中查看）", raw_answer)
    # 飞书卡片不渲染 # 标题语法 → 转为加粗
    answer = re.sub(r"^#{1,6}\s*(.+)$", r"**\1**", answer, flags=re.M)
    # 正文引用序号 [n] → 可点击的定位链接（链接文本用平衡方括号 [n]）
    refmap = {ref["n"]: ref["url"] for s in r.get("sources", [])
              for ref in s.get("refs", [])}

    def _cite(m):
        n = int(m.group(1))
        return f"[[{n}]]({refmap[n]})" if refmap.get(n) else m.group(0)

    answer = re.sub(r"\[(\d{1,2})\]", _cite, answer)

    elements = [{"tag": "markdown", "content": answer}]
    src_lines = []
    for s in r.get("sources", []):
        nums = " ".join(f"[[{ref['n']}]]({ref['url']})" for ref in s.get("refs", []))
        icon = "📄" if s.get("kind") == "feishu" else "🌐"
        src_lines.append(f"{nums} {icon} [{s.get('title')}]({s.get('url')})")
    if src_lines:
        elements += [{"tag": "hr"},
                     {"tag": "markdown",
                      "content": "**来源**（点序号定位到原文位置）\n" + "\n".join(src_lines)}]
    return {"config": {"wide_screen_mode": True}, "elements": elements}, raw_answer


def _md_card(text: str) -> dict:
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}]}


def _reply(message_id: str, card: dict) -> str | None:
    """回复卡片，返回新消息的 message_id（用于后续原地更新）。"""
    req = (ReplyMessageRequest.builder()
           .message_id(message_id)
           .request_body(ReplyMessageRequestBody.builder()
                         .content(json.dumps(card, ensure_ascii=False))
                         .msg_type("interactive")
                         .build())
           .build())
    resp = _client.im.v1.message.reply(req)
    if not resp.success():
        print(f"⚠️ 回复失败 code={resp.code} msg={resp.msg}")
        return None
    return resp.data.message_id if resp.data else None


def _patch(message_id: str, card: dict) -> bool:
    """原地更新卡片（占位 → 正式回答）。"""
    req = (PatchMessageRequest.builder()
           .message_id(message_id)
           .request_body(PatchMessageRequestBody.builder()
                         .content(json.dumps(card, ensure_ascii=False))
                         .build())
           .build())
    resp = _client.im.v1.message.patch(req)
    if not resp.success():
        print(f"⚠️ 卡片更新失败 code={resp.code} msg={resp.msg}")
        return False
    return True


def _react(message_id: str, emoji_type: str = "Typing") -> str | None:
    """给用户的消息加表情回应，返回 reaction_id 供撤销（失败不影响主流程）。"""
    try:
        req = (CreateMessageReactionRequest.builder()
               .message_id(message_id)
               .request_body(CreateMessageReactionRequestBody.builder()
                             .reaction_type(Emoji.builder()
                                            .emoji_type(emoji_type).build())
                             .build())
               .build())
        resp = _client.im.v1.message_reaction.create(req)
        if resp.success() and resp.data:
            return resp.data.reaction_id
    except Exception:  # noqa
        pass
    return None


def _unreact(message_id: str, reaction_id: str | None):
    """撤销表情回应（回答已给出，敲键盘消失）。"""
    if not reaction_id:
        return
    try:
        req = (DeleteMessageReactionRequest.builder()
               .message_id(message_id)
               .reaction_id(reaction_id)
               .build())
        _client.im.v1.message_reaction.delete(req)
    except Exception:  # noqa
        pass


def _handle(data: P2ImMessageReceiveV1):
    event_id = data.header.event_id if data.header else None
    msg = data.event.message
    # 收到即打日志。「@了没反应」时，这行有没有出现能直接分清两种故障：
    # 没出现 = 事件根本没推过来（权限/事件订阅/没发布新版本）；
    # 出现了但没回复 = 推过来了但发送失败，下面 _reply 会打 code 和 msg。
    print(f"[收到] chat_type={getattr(msg, 'chat_type', '?')} "
          f"msg_type={msg.message_type} mentions={len(msg.mentions or [])} "
          f"chat_id={getattr(msg, 'chat_id', '?')}")
    if event_id and event_id in _seen:
        print("  ↳ 重复事件，已忽略")
        return
    if event_id:
        _seen.append(event_id)

    if msg.message_type != "text":
        _reply(msg.message_id, _md_card("目前只支持文字提问～"))
        return
    try:
        text = json.loads(msg.content).get("text", "")
    except Exception:
        return
    question = re.sub(r"@_user_\d+", "", text).strip()
    if not question:
        _reply(msg.message_id, _md_card(
            f"你好，我是{cfg.get('bot', 'name', default='知识库助手')}，@我并输入问题即可。"))
        return
    # 即时反馈：敲键盘表情（答案给出后撤销）+ 占位卡片（答案生成后原地更新）
    reaction_id = _react(msg.message_id, "Typing")
    placeholder_id = _reply(msg.message_id, _md_card("🤔 已收到，正在查阅知识库…"))
    chat_id = getattr(msg, "chat_id", None) or "default"
    hist = _hist.setdefault(chat_id, deque(maxlen=_HIST_MAX))
    # 只把配置指定的轮数发给后端（改 config.yaml 即时生效，无需重启机器人）
    rounds = int(cfg.get("retrieval", "history_rounds", default=2) or 0)

    def _run():
        card, raw_answer = build_card(question, list(hist)[-rounds:] if rounds > 0 else [])
        if not (placeholder_id and _patch(placeholder_id, card)):
            _reply(msg.message_id, card)   # 更新失败则退化为新发一条
        _unreact(msg.message_id, reaction_id)
        if raw_answer:
            hist.append((question, raw_answer))

    threading.Thread(target=_run, daemon=True).start()


def main():
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle)
               .build())
    print(f"飞书机器人已启动（长连接模式），问答后端：{ASK_URL}")
    projects = cfg.get("bot", "projects", default=[]) or []
    print(f"检索范围：{'全部项目' if not projects else '白名单 ' + str(projects)}")
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                        log_level=lark.LogLevel.INFO)
    ws.start()


if __name__ == "__main__":
    main()
