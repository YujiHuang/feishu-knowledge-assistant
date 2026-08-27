"""历史轮数配置项的回归测试：python tests/test_history.py

历史每多带一轮，就多发一遍上一轮的问题和答案，是纯 token 成本。这里验证
retrieval.history_rounds 真的控制住了发出去的消息条数。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import answer as A

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


class FakeCfg:
    """cfg.get("llm", "model") 这种取值方式的最小替身。"""

    def __init__(self, over: dict | None = None):
        self.d = {("llm", "base_url"): "https://gw.test/v1",
                  ("llm", "api_key"): "k", ("llm", "model"): "m",
                  ("llm", "temperature"): 0.2}
        self.d.update(over or {})

    def get(self, *path, default=None):
        v = self.d.get(tuple(path), default)
        return default if v is None else v   # 与 app/config.py 的空值语义一致


class FakeResp:
    status_code = 200

    @staticmethod
    def json():
        return {"choices": [{"message": {"content": "答案 [1]"}}]}


sent: list[dict] = []


def fake_post(url, headers=None, json=None, timeout=None):   # noqa: A002
    sent.append(json)
    return FakeResp()


A.httpx.post = fake_post

SRC = [{"n": 1, "kind": "feishu", "title": "t", "context": "t", "text": "正文"}]
HIST = [{"q": f"问{i}", "a": f"答{i}"} for i in range(1, 6)]   # 5 轮历史
_ABSENT = object()


def run(rounds=_ABSENT, history=HIST):
    """rounds 省略 = 配置项缺失；否则按给的值配上去。返回发出去的 messages。"""
    over = {} if rounds is _ABSENT else {("retrieval", "history_rounds"): rounds}
    sent.clear()
    A.AnswerEngine(None, None, FakeCfg(over))._generate("现在呢？", SRC, history)
    return sent[0]["messages"]


print("1) 配置项缺失时默认 2 轮")
msgs = run()
check("system + 2 轮×2 + 本次提问 = 6 条", len(msgs) == 6, len(msgs))
check("带的是最近 2 轮", [m["content"] for m in msgs[1:5]] == ["问4", "答4", "问5", "答5"],
      [m["content"] for m in msgs[1:5]])
check("首条是 system", msgs[0]["role"] == "system")
check("末条是本次提问", "现在呢？" in msgs[-1]["content"])
check("历史消息 role 交替正确",
      [m["role"] for m in msgs[1:5]] == ["user", "assistant"] * 2,
      [m["role"] for m in msgs[1:5]])

print("2) 配置能调大调小")
check("history_rounds=1 → 4 条", len(run(1)) == 4, len(run(1)))
check("history_rounds=4 → 10 条", len(run(4)) == 10, len(run(4)))
check("history_rounds=0 → 只有 system + 提问", len(run(0)) == 2, len(run(0)))
check("轮数超过实际历史不报错", len(run(99)) == 2 + 2 * len(HIST), len(run(99)))
check("=2 比 =4 少 4 条消息", len(run(4)) - len(run(2)) == 4)

print("3) 边界：历史为空 / 配置写成字符串或 null")
check("无历史时只有 system + 提问", len(run(2, [])) == 2, len(run(2, [])))
check("无历史时 None 也不炸", len(run(2, None)) == 2, len(run(2, None)))
check("配成字符串 '3' 也能用", len(run("3")) == 8, len(run("3")))
check("配成 null 回落到默认 2 轮", len(run(None)) == 6, len(run(None)))
check("配成负数视为不带历史", len(run(-1)) == 2, len(run(-1)))

print("4) 请求体其余部分没被改坏")
msgs = run(2)
body = sent[0]
check("model 正确", body["model"] == "m", body.get("model"))
check("temperature 正确", body["temperature"] == 0.2, body.get("temperature"))
check("参考资料进了最后一条 user 消息", "正文" in msgs[-1]["content"])
check("提示词禁用 # 标题的规则还在", "禁止使用 # 标题语法" in msgs[0]["content"])

print(f"\n通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
