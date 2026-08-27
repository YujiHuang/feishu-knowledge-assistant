"""环境变量覆盖配置的回归测试：python tests/test_config_env.py

目的：API key 只活在终端会话里，不落到 config.yaml。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import _from_env, load_config

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


YAML = """\
llm:
  base_url: "https://api.deepseek.com/v1"
  api_key: ""
  model: "deepseek-v4-flash"
  temperature: 0.2
retrieval:
  final_top_k: 8
  history_rounds:
feishu:
  app_secret: "file-secret"
"""

ENV_KEYS = ("KA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
            "KA_LLM_MODEL", "KA_FEISHU_APP_SECRET", "TAVILY_API_KEY",
            "KA_WEB_SEARCH_API_KEY")


def clean():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


tmp = Path(tempfile.mkdtemp()) / "config.yaml"
tmp.write_text(YAML, encoding="utf-8")
cfg = load_config(str(tmp))

print("1) key 不在文件里时靠环境变量拿")
clean()
check("文件里是空的", cfg.get("llm", "api_key") == "")
check("_from_env 此时没有来源", _from_env(("llm", "api_key")) is None)
os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
check("DEEPSEEK_API_KEY 生效", cfg.get("llm", "api_key") == "sk-from-env",
      cfg.get("llm", "api_key"))
check("_from_env 能报出来源", _from_env(("llm", "api_key")) == "sk-from-env")
os.environ["KA_LLM_API_KEY"] = "sk-ka"
check("KA_ 前缀优先级更高", cfg.get("llm", "api_key") == "sk-ka",
      cfg.get("llm", "api_key"))

print("2) 环境变量优先于文件里的值")
clean()
check("没设环境变量时用文件值", cfg.get("llm", "model") == "deepseek-v4-flash")
os.environ["KA_LLM_MODEL"] = "deepseek-v4-pro"
check("KA_LLM_MODEL 覆盖文件", cfg.get("llm", "model") == "deepseek-v4-pro")
clean()
check("清掉环境变量后回到文件值", cfg.get("llm", "model") == "deepseek-v4-flash")

print("3) 空字符串按「没设」处理，不会把配置擦成空")
clean()
os.environ["KA_LLM_MODEL"] = ""
check("空环境变量不覆盖", cfg.get("llm", "model") == "deepseek-v4-flash",
      cfg.get("llm", "model"))
os.environ["KA_FEISHU_APP_SECRET"] = ""
check("空环境变量不擦掉 app_secret",
      cfg.get("feishu", "app_secret") == "file-secret")

print("4) 别名与无关变量")
clean()
os.environ["TAVILY_API_KEY"] = "tv-1"
check("web_search.api_key 认 TAVILY_API_KEY",
      cfg.get("web_search", "api_key") == "tv-1")
check("无关路径不受影响", cfg.get("llm", "base_url").endswith("/v1"))
check("不存在的配置项返回 default",
      cfg.get("llm", "不存在的键", default="兜底") == "兜底")

print("5) YAML 空值回落到 default（手改配置最容易踩）")
clean()
check("history_rounds 写成空 → 用 default", cfg.get("retrieval", "history_rounds",
                                                   default=2) == 2,
      cfg.get("retrieval", "history_rounds", default=2))
check("存在的数值项照常读", cfg.get("retrieval", "final_top_k") == 8)
check("temperature 仍是浮点不是字符串",
      isinstance(cfg.get("llm", "temperature"), float),
      type(cfg.get("llm", "temperature")))

print("6) config.yaml 里确实没有 key（本仓库当前状态）")
real = Path(__file__).resolve().parents[1] / "config.yaml"
if real.exists():
    text = real.read_text(encoding="utf-8")
    line = [ln for ln in text.splitlines()
            if ln.strip().startswith("api_key:") and "#" not in ln.split("api_key:")[0]]
    check("llm.api_key 在文件里是空的",
          any(ln.split("api_key:")[1].strip().strip('"\'') == "" for ln in line[:1]),
          line[:1])
else:
    print("  （跳过：没有 config.yaml）")

clean()
print(f"\n通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
