"""LanceDB 新增 anchors 列的落盘/读回与旧表迁移测试：python tests/test_store_anchors.py"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.index.store import ChunkStore

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def row(i, anchors):
    return {"id": f"D1#{i}", "doc_token": "D1", "title": "示例表",
            "title_path": "示例表 > 示例指标", "url": "https://f/wiki/A#HEAD",
            "doc_url": "https://f/wiki/A",
            "anchors": json.dumps(anchors, ensure_ascii=False),
            "project": "示例项目", "mtime": 1750000000,
            "text": f"Cirrus 17 Pro Max 单机激活量约 19{i} 万台",
            "vector": [0.1 * i, 0.2, 0.3, 0.4]}


tmp = Path(tempfile.mkdtemp())
try:
    print("1) 新表写入与读回")
    st = ChunkStore(tmp)
    anchors = [[30, "https://f/wiki/A#ROWaurora"], [70, "https://f/wiki/A#ROWcirrus"]]
    st.replace_doc("D1", [row(0, anchors), row(1, anchors)])
    st.rebuild_keyword_index()
    check("行数正确", st.counts()["chunks"] == 2, st.counts())
    check("schema 含 anchors 列", "anchors" in set(st._table().schema.names),
          st._table().schema.names)

    kw = st.keyword_search("Cirrus 激活量", 5)
    check("关键词检索有结果", len(kw) > 0, len(kw))
    check("关键词结果带回 anchors", bool(kw and kw[0].get("anchors")),
          kw[0].keys() if kw else None)
    check("anchors 可解析回原结构",
          bool(kw) and json.loads(kw[0]["anchors"]) == anchors,
          kw[0].get("anchors") if kw else None)

    vec = st.vector_search([0.1, 0.2, 0.3, 0.4], 5)
    check("向量检索有结果", len(vec) > 0, len(vec))
    check("向量结果带回 anchors", bool(vec and vec[0].get("anchors")))
    check("向量结果已剥离 vector 字段", bool(vec) and "vector" not in vec[0])

    print("2) 空 anchors 与超长 anchors 不炸")
    st.replace_doc("D2", [{**row(9, []), "id": "D2#0", "doc_token": "D2"}])
    big = [[i * 7, f"https://f/wiki/A#B{i:04d}"] for i in range(200)]
    st.replace_doc("D3", [{**row(8, big), "id": "D3#0", "doc_token": "D3"}])
    st.rebuild_keyword_index()
    check("三篇文档共存", st.counts()["docs"] == 3, st.counts())
    hits = {r["doc_token"]: r for r in st.keyword_search("Cirrus 激活量", 10)}
    check("空 anchors 存成 []", json.loads(hits["D2"]["anchors"]) == []
          if "D2" in hits else False, list(hits))
    check("200 个锚点完整读回",
          len(json.loads(hits["D3"]["anchors"])) == 200 if "D3" in hits else False)

    print("3) 项目过滤仍生效")
    check("命中项目", len(st.keyword_search("Cirrus", 10, {"示例项目"})) > 0)
    check("过滤掉不存在的项目", st.keyword_search("Cirrus", 10, {"不存在的项目"}) == [])

    print("4) 旧表（无 anchors 列）自动清空以触发全量重建")
    tmp2 = Path(tempfile.mkdtemp())
    try:
        old = ChunkStore(tmp2)
        legacy = {k: v for k, v in row(0, []).items() if k != "anchors"}
        old.replace_doc("D1", [legacy])
        check("旧结构表已建立", old.counts()["chunks"] == 1)
        del old
        fresh = ChunkStore(tmp2)      # 重新打开：应检测到缺列并删表
        check("旧表被清空", fresh.counts()["chunks"] == 0, fresh.counts())
        check("清空后检索为空，不抛异常", fresh.keyword_search("Cirrus", 5) == [])
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
