"""配置加载。"""
import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent

# 环境变量可以覆盖任意配置项：路径 llm.api_key → KA_LLM_API_KEY。
# 这样密钥只活在启动进程的那个终端会话里，不用落到 config.yaml。
# 另外接受各家的习惯命名，方便直接复用已有的 export。
# 注意：环境变量取到的一定是字符串，所以这个机制是给密钥/名称类配置用的，
# 不要用它覆盖 temperature、top_k 这些数值项。
_ENV_ALIASES = {
    ("llm", "api_key"): ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    ("feishu", "app_secret"): (),
    ("web_search", "api_key"): ("TAVILY_API_KEY",),
}


def _from_env(keys: tuple[str, ...]) -> str | None:
    names = ["KA_" + "_".join(k.upper() for k in keys)]
    names += [n for n in _ENV_ALIASES.get(keys, ()) if n not in names]
    for n in names:
        v = os.environ.get(n)
        if v:                      # 空字符串按"没设"处理，避免误伤
            return v
    return None


class Config:
    def __init__(self, data: dict):
        self._d = data or {}

    def __getitem__(self, key):
        return self._d[key]

    def get(self, *keys, default=None):
        env = _from_env(keys)
        if env is not None:
            return env
        cur = self._d
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return default if cur is None else cur

    @property
    def data_dir(self) -> Path:
        p = Path(os.path.expanduser(self.get("storage", "data_dir", default="~/.knowledge-assistant")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def redirect_uri(self) -> str:
        host = self.get("server", "host", default="127.0.0.1")
        port = self.get("server", "port", default=8787)
        return f"http://{host}:{port}/callback"


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else _ROOT / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path}。请先执行: cp config.example.yaml config.yaml 并填入真实值。"
        )
    with open(cfg_path, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))
