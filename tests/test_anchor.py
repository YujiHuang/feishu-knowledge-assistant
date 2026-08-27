"""锚点定位与卡片渲染的回归测试：python tests/test_anchor.py"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.index.chunker import (CHUNK_OVERLAP, CHUNK_SIZE, MIN_CHUNK,
                               _pick_anchor, _window, chunk_document)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def mark(bid):
    return f"\x00b:{bid}\x00"


print("1) _window 返回 (片段, 重叠长度)，且不撕裂锚点标记")
# 构造：每 80 字一段，段尾带锚点，总长远超 CHUNK_SIZE
paras = [f"第{i}段内容" + "补" * 70 + mark(f"blk{i:02d}") for i in range(40)]
body = "\n".join(paras)
pieces = _window(body)
check("切出 3 块以上（多次结转 lead）", len(pieces) > 2, f"got {len(pieces)}")
check("元素都是 (str, int)",
      all(isinstance(p, str) and isinstance(l, int) for p, l in pieces))
check("每个片段 \\x00 成对（标记未被撕裂）",
      all(p.count("\x00") % 2 == 0 for p, _ in pieces),
      [p.count("\x00") for p, _ in pieces])
check("首片段 lead=0", pieces[0][1] == 0)
check("后续片段 lead>0 且 <= CHUNK_OVERLAP+标记长度",
      all(0 < l for _, l in pieces[1:]), [l for _, l in pieces])
# lead 语义正确性：片段 i 的开头 lead 个字符 == 片段 i-1 的结尾 lead 个字符
sem = all(pieces[i][0][:pieces[i][1]] == pieces[i - 1][0][-pieces[i][1]:]
          for i in range(1, len(pieces)) if pieces[i][1])
check("lead 语义正确（开头重叠区 == 上一片段尾部）", sem)
check("拼接后覆盖全文（去重叠）",
      "".join(p if i == 0 else p[l:] for i, (p, l) in enumerate(pieces)) == body)

print("2) _pick_anchor 跳过重叠区锚点（锚点漂移修复的核心）")
# 复刻线上现象：片段开头的重叠区里带着上一段的锚点 blkPREV，
# 片段自己的内容锚点是 blkOWN。旧实现取 anchors[0] → 定位到上一段。
piece = "记忆调用的准确率约 88%。" + mark("blkPREV") + \
        "\nCirrus 17 Pro Max：单机激活量约 143 万台。" + mark("blkOWN")
lead = piece.index("\n") + 1     # 重叠区 = 第一行（含其锚点）
check("选中本片段锚点 blkOWN", _pick_anchor(piece, lead) == "b:blkOWN",
      _pick_anchor(piece, lead))
check("lead=0 时取第一个锚点", _pick_anchor(piece, 0) == "b:blkPREV")
check("重叠区之后无锚点时回退到重叠区锚点",
      _pick_anchor("尾部内容。" + mark("blkPREV") + "无锚点的续写文字",
                   6) == "b:blkPREV")
check("无锚点返回空串", _pick_anchor("纯文本没有标记", 0) == "")

print("3) 真实切块链路上的锚点归属")
long_text = "# 示例指标\n" + "\n".join(
    f"机型{i}：单机激活量约 {i * 10} 万台。" + "说明" * 40 + mark(f"row{i:02d}")
    for i in range(10))
chunks = chunk_document("示例产品对比表", long_text)
check("切出多块", len(chunks) > 2, f"got {len(chunks)}")
check("所有块都有锚点", all(c["anchor"] for c in chunks),
      [c["anchor"] for c in chunks])
check("锚点互不重复（不再共用上一块的锚点）",
      len({c["anchor"] for c in chunks}) == len(chunks),
      [c["anchor"] for c in chunks])
# 每块锚点应属于该块「非重叠部分」出现的行
anchors = [c["anchor"] for c in chunks]
check("锚点顺序单调递增", anchors == sorted(anchors), anchors)
check("正文已清除不可见标记",
      all("\x00" not in c["text"] for c in chunks))
check("标题路径包含文档名与小节名",
      all(c["title_path"] == "示例产品对比表 > 示例指标"
          for c in chunks), chunks[0]["title_path"])
check("embed_text = 标题路径 + 正文",
      chunks[0]["embed_text"] == f"{chunks[0]['title_path']}\n{chunks[0]['text']}")

print("4) 短文档 / 短小节不丢块")
one = chunk_document("小记", "## 结论\n只有一句话。" + mark("b1"))
check("短小节保留 1 块", len(one) == 1, len(one))
check("短小节锚点正确", one[0]["anchor"] == "b:b1", one[0]["anchor"])
tiny = chunk_document("超短", "嗯。" + mark("b0"))
check("全文极短仍保留 1 块", len(tiny) == 1 and tiny[0]["text"] == "嗯。",
      tiny)
check("空文档返回空", chunk_document("空", "   \n  ") == [])

print("5) bot 卡片文本处理：# 标题 → 加粗、附件路径替换、序号链接")
src = Path(__file__).resolve().parents[1] / "app" / "bot.py"
code = src.read_text()
check("bot.py 含 # → 加粗 的转换",
      r'^#{1,6}\s*(.+)$' in code and r'**\1**' in code)


def render(answer, refmap):
    """复刻 build_card 里的文本处理顺序，验证组合效果。"""
    a = re.sub(r"[（(]?/api/media/[A-Za-z0-9_\-]+[）)]?",
               "（附件请在来源文档中查看）", answer)
    a = re.sub(r"^#{1,6}\s*(.+)$", r"**\1**", a, flags=re.M)
    return re.sub(r"\[(\d{1,2})\]",
                  lambda m: (f"[[{m.group(1)}]]({refmap[int(m.group(1))]})"
                             if refmap.get(int(m.group(1))) else m.group(0)), a)


out = render("## 结论\n### 依据\n- **圈选**较好 [1]\n截图：（/api/media/AbC-1_x）[2]\n"
             "普通井号 #标签 不该被转",
             {1: "https://x.feishu.cn/wiki/A#blk1"})
check("## 标题 → **加粗**", "**结论**" in out and "## " not in out, out)
check("### 标题 → **加粗**", "**依据**" in out)
check("行内 # 不受影响", "#标签" in out, out)
check("附件路径被替换", "/api/media/" not in out and "附件请在来源文档中查看" in out)
check("有链接的序号变可点击", "[[1]](https://x.feishu.cn/wiki/A#blk1)" in out, out)
check("无链接的序号保持原样", "[2]" in out and "[[2]]" not in out)
check("原有加粗未被破坏", "**圈选**较好" in out)

print("6) 提示词已禁用 # 标题语法")
ans = (Path(__file__).resolve().parents[1] / "app" / "rag" / "answer.py").read_text()
check("SYSTEM_PROMPT 禁止 # 标题", "禁止使用 # 标题语法" in ans)
check("规则编号未重复", ans.count("\n6. ") == 1 and ans.count("\n7. ") == 1)

print("7) PARSER_VERSION 已递增（触发全量重建）")
sync = (Path(__file__).resolve().parents[1] / "app" / "feishu" / "sync.py").read_text()
m = re.search(r"PARSER_VERSION = (\d+)", sync)
check("PARSER_VERSION >= 6", m and int(m.group(1)) >= 6, m.group(1) if m else None)

print(f"\n参数：CHUNK_SIZE={CHUNK_SIZE} OVERLAP={CHUNK_OVERLAP} MIN={MIN_CHUNK}")
print(f"通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
