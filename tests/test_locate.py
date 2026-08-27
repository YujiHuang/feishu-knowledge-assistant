"""引用序号 → 精确行定位的端到端回归测试：python tests/test_locate.py

链路：带锚点标记的文本 → chunk_document → sync 的 anchors JSON → answer 的行匹配
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.feishu.sync import _chunk_url
from app.index.chunker import chunk_document
from app.rag.answer import (_anchor_for, _best_offset, _cite_contexts,
                            _load_anchors, refine_citation_anchors)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


# 假 token，只是为了拼出合法形状的 URL；断言只看 # 后面的锚点和 /base/ 深链形式
DOC_URL = "https://x.feishu.cn/wiki/FAKEWIKINODETOKEN01"


def blk(bid):
    return f"\x00b:{bid}\x00"


def build_sources(title, text, doc_url=DOC_URL):
    """复刻 sync._index_one 的行为，产出 answer.ask 里那种 sources 结构。"""
    out = []
    for i, c in enumerate(chunk_document(title, text)):
        anchors_json = json.dumps(
            [[off, _chunk_url(doc_url, a)] for off, a in c["anchors"]],
            ensure_ascii=False)
        out.append({"n": i + 1, "kind": "feishu", "title": title,
                    "url": _chunk_url(doc_url, c["anchor"]),
                    "group": doc_url,
                    "anchors": _load_anchors(anchors_json),
                    "text": c["text"]})
    return out


print("1) 复刻线上问题：表格块内第 N 行被引用")
# 机型名、数值都是编的，只为构造「一个 chunk 里多行、各行有独立辨识词」的形状
table = "\n".join([
    "核心表：一行 = 一个机型 × 一个原子能力" + blk("HEAD"),
    "Aurora X200 | 记忆调用的准确率 | 约 88% | 2026-07 实测" + blk("ROWaurora"),
    "Borealis F9 | 跨应用任务执行成功率 | 约 71% | 2026-07 实测" + blk("ROWborealis"),
    "Cirrus 17 Pro Max | 同日单机激活量 | 约 143 万台 | 未注明统计日期" + blk("ROWcirrus"),
    "Dorado M8 | 首销激活量 | 约 61 万台 | 渠道口径" + blk("ROWdorado"),
])
srcs = build_sources("示例产品对比表", table)
check("整段进同一个 chunk（正是出问题的场景）", len(srcs) == 1, len(srcs))
s = srcs[0]
check("chunk 保留了 5 个锚点", len(s["anchors"]) == 5,
      [a for _, a in s["anchors"]])
block_level = s["url"]
check("块级锚点仍是首行", block_level.endswith("#HEAD"), block_level)

answer = ("Cirrus 17 Pro Max：同日单机激活量约 143 万台，但资料未注明具体统计日期。[1]")
refine_citation_anchors(answer, srcs)
check("定位收窄到 Cirrus 那一行", s["url"].endswith("#ROWcirrus"), s["url"])

print("2) 同一 chunk 的不同论断各自定位到自己那一行")
for sentence, want in [
    ("Aurora X200 的记忆调用准确率约 88%。[1]", "#ROWaurora"),
    ("Borealis F9 跨应用任务执行成功率约 71%。[1]", "#ROWborealis"),
    ("Dorado M8 首销激活量约 61 万台。[1]", "#ROWdorado"),
    ("该表以「一行 = 一个机型 × 一个原子能力」的口径组织。[1]", "#HEAD"),
]:
    fresh = build_sources("示例产品对比表", table)
    refine_citation_anchors(sentence, fresh)
    check(f"{sentence[:14]}… → {want}", fresh[0]["url"].endswith(want),
          fresh[0]["url"])

print("3) 多维表格记录深链同样收窄")
recs = "\n".join([
    "圈选搜索 | Aurora | 已上线" + "\x00r:APP:TBL:recAURORA:VIEW\x00",
    "圈选搜索 | Cirrus | 规划中" + "\x00r:APP:TBL:recCIRRUS:VIEW\x00",
    "圈选搜索 | Dorado | 未见" + "\x00r:APP:TBL:recDORADO:VIEW\x00",
    "补充说明：口径以公开发布会为准，" + "占位" * 120,
])
rsrc = build_sources("示例功能明细表", recs,
                     "https://x.feishu.cn/wiki/FAKEWIKINODETOKEN02")
refine_citation_anchors("Cirrus 的圈选搜索仍处于规划中。[1]", rsrc)
check("命中 recCIRRUS 记录深链", "record=recCIRRUS" in rsrc[0]["url"],
      rsrc[0]["url"])
check("记录深链是 /base/ 直链形式", "/base/APP?table=TBL" in rsrc[0]["url"],
      rsrc[0]["url"])

print("4) 匹配太弱时不乱跳，保留块级锚点")
weak = build_sources("示例产品对比表", table)
before = weak[0]["url"]
refine_citation_anchors("综上，各家能力差异明显。[1]", weak)
check("泛泛而谈的句子不改写链接", weak[0]["url"] == before, weak[0]["url"])
noanchor = [{"n": 1, "kind": "feishu", "title": "x", "url": "u", "anchors": [],
             "text": "Cirrus 143 万台"}]
refine_citation_anchors("Cirrus 143 万台。[1]", noanchor)
check("无锚点数据不报错且不改写", noanchor[0]["url"] == "u")
web = [{"n": 1, "kind": "web", "title": "x", "url": "http://w",
        "anchors": [(5, "http://other")], "text": "Cirrus 143 万台"}]
refine_citation_anchors("Cirrus 143 万台。[1]", web)
check("网页来源不参与行定位", web[0]["url"] == "http://w")

print("5) 序号本身的数字不参与匹配")
ctx = _cite_contexts("甲的份额是 3 成。[7] 乙另有说法。[3]")
check("按序号归集上下文", set(ctx) == {7, 3}, ctx)
check("上下文里已剔除 [n]", all("[" not in c for cs in ctx.values() for c in cs),
      ctx)
check("上下文取的是紧邻那一句", ctx[7] == ["甲的份额是 3 成。"] or
      ctx[7][0].strip().endswith("3 成。"), ctx[7])
# 若不剔除，"[3]" 的 3 会与含 3 的行强匹配
trap = "\n".join(["无关行 A：占位内容" + blk("A"),
                  "关键行 B：数值 3 成" + blk("B")])
tsrc = build_sources("陷阱", trap)
refine_citation_anchors("乙的口径完全不同，没有给出数字。[3]", tsrc)
check("纯序号数字不会误导定位", tsrc[0]["url"].endswith("#A") or
      tsrc[0]["url"] == _chunk_url(DOC_URL, "b:A"), tsrc[0]["url"])

print("6) 偏移 → 锚点的边界语义")
anchors = [(10, "u1"), (25, "u2"), (40, "u3")]
check("行首偏移取该行锚点", _anchor_for(0, anchors) == "u1")
check("第二行内偏移取第二行锚点", _anchor_for(15, anchors) == "u2")
check("超出末尾取最后一个", _anchor_for(999, anchors) == "u3")
check("空锚点返回 None", _anchor_for(0, []) is None)
check("_best_offset 无 token 时返回 None",
      _best_offset(["、。"], "任意文本") is None)

print("7) 长文档跨 chunk 时锚点仍归属正确 chunk")
long_text = "\n".join(f"第{i:02d}行 指标{i} 数值 {100 + i} 万台" + blk(f"L{i:02d}")
                      for i in range(60))
lsrc = build_sources("长表", long_text)
check("切成多个 chunk", len(lsrc) > 1, len(lsrc))
target = "第47行的数值为 147 万台。[%d]"
found = None
for src in lsrc:
    if "第47行" in src["text"]:
        found = src
        break
check("能找到含目标行的 chunk", found is not None)
if found:
    refine_citation_anchors(target % found["n"], [found])
    check("定位到 L47", found["url"].endswith("#L47"), found["url"])

print(f"\n通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
