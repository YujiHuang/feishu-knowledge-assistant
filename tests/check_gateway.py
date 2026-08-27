"""网关连通性自检：python tests/check_gateway.py

不改任何配置，只按 config.yaml + 环境变量的最终结果去实打实调一次，逐层给结论：
  ① 域名/证书/端口通不通
  ② key 能不能过鉴权（GET /v1/models）
  ③ 你配的 model 在这个网关上存不存在、支不支持 chat 接口
  ④ 真发一次最小 chat/completions（请求体与 answer.py 完全一致）
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    try:
        import httpx
    except ImportError:
        sys.exit("请先 source .venv/bin/activate（需要 httpx）")
    from app.config import _from_env, load_config

    cfg = load_config()
    base = (cfg.get("llm", "base_url") or "").rstrip("/")
    model = cfg.get("llm", "model") or ""
    key = cfg.get("llm", "api_key") or ""
    src = "环境变量" if _from_env(("llm", "api_key")) else "config.yaml"
    if not key:
        print("✗ 没拿到 api_key。key 不落盘的用法是在启动前先执行：")
        print('    export DEEPSEEK_API_KEY="你的 key"')
        print("  然后在同一个终端里跑本脚本 / python -m app.main")
        print(f"  （当前进程里这些变量都是空的："
              f"{' '.join(n for n in ('KA_LLM_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY') if not os.environ.get(n))}）")
        return 1

    print(f"网关 : {base}")
    print(f"模型 : {model}")
    print(f"key  : {key[:4]}…{key[-4:]}（长度 {len(key)}，来自{src}）\n")
    headers = {"Authorization": f"Bearer {key}"}

    print("① 列模型（顺带验鉴权）")
    try:
        r = httpx.get(f"{base}/models", headers=headers, timeout=20)
        # 有的网关要求按用途筛选，不带参数会 400；这种情况补一次再试
        if r.status_code == 400:
            r = httpx.get(f"{base}/models", params={"purpose": "code"},
                          headers=headers, timeout=20)
    except Exception as e:  # noqa
        print(f"  ✗ 连不上：{type(e).__name__}: {e}")
        print("    → 证书报 not yet valid / expired，先查本机时间；"
              "超时则看是否要连公司网络或代理")
        return 1
    print(f"  HTTP {r.status_code}")
    if r.status_code in (401, 403):
        print(f"  ✗ key 没过鉴权：{' '.join(r.text.split())[:200]}")
        return 1

    models: dict[str, list[str]] = {}
    if r.status_code == 200:
        try:
            for m in r.json().get("data", []):
                models[m["id"]] = [a["id"] for a in m.get("support_apis", [])]
        except Exception:  # noqa
            print(f"  ! 返回不是预期结构，跳过模型校验：{r.text[:150]}")
    elif r.status_code == 404:
        print("  ! 这个网关没有 /models 接口，跳过模型校验（不影响下一步）")

    if models:
        print(f"  ✓ 鉴权通过，可用模型 {len(models)} 个：")
        for mid, apis in sorted(models.items()):
            tail = f"  [{','.join(apis)}]" if apis else ""
            print(f"      {mid}{tail}{'   ←你配的' if mid == model else ''}")
        if model not in models:
            print(f"  ✗ {model} 不在列表里 → 换成上面其中一个")
            return 1
        # 只有网关明确声明了接口类型时才校验；DeepSeek 等不返回这个字段
        if models[model] and "chat" not in models[model]:
            print(f"  ✗ {model} 不支持 chat 接口（只支持 {models[model]}）。"
                  "本项目发的是 OpenAI /chat/completions，换个支持 chat 的模型")
            return 1

    print("\n② 发一次最小请求（请求体与 answer.py 一致）")
    r = httpx.post(
        f"{base}/chat/completions", headers=headers,
        json={"model": model, "temperature": cfg.get("llm", "temperature", default=0.2),
              "messages": [{"role": "user", "content": "只回复 OK"}]},
        timeout=60)
    print(f"  HTTP {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"  ✓ 通了。回复：{d['choices'][0]['message']['content']!r}")
        print(f"    用量：{d.get('usage')}")
        return 0

    body = " ".join((r.text or "").split())[:400]
    print(f"  ✗ 失败：{body}")
    low = body.lower()
    if r.status_code == 402 or "quota" in low or "balance" in low:
        print("    → 余额/额度不足：充值，或找网关管理员加额度、换预算池")
    elif r.status_code == 400 and "temperature" in low:
        print("    → 该模型不接受这个 temperature，把 config.yaml 改成 1")
    elif r.status_code == 404:
        print("    → 路径或模型名不对：确认 base_url 结尾（DeepSeek 要 /v1，"
              "智谱是 /api/paas/v4）")
    elif r.status_code == 429:
        print("    → 被限流，等一会儿再试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
