#!/usr/bin/env bash
# 启动主服务： ./start.sh          （网页界面 http://127.0.0.1:8787）
# 启动机器人： ./start.sh bot      （另开一个终端标签页，需主服务已在跑）
#
# 用 bash 而不是 zsh 写，是因为 read 的语法两者不兼容；这样不管你的默认 shell 是哪个都能跑。
# LLM 的 key 不落盘：优先取环境变量，其次取 macOS 钥匙串，都没有就当场问你。
set -euo pipefail
cd "$(dirname "$0")"

KEYCHAIN_SERVICE="knowledge-assistant-llm"

if [ ! -d .venv ]; then
  echo "没有 .venv，先建环境："
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
if [ ! -f config.yaml ]; then
  echo "没有 config.yaml，先复制一份填上飞书应用信息：cp config.example.yaml config.yaml"
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 机器人不调模型（它 POST 主服务的 /api/ask），不需要 key
if [ "${1:-}" = "bot" ]; then
  exec python -m app.bot
fi

KEY="${DEEPSEEK_API_KEY:-${KA_LLM_API_KEY:-}}"
SRC="环境变量"
if [ -z "$KEY" ]; then
  KEY="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
  SRC="钥匙串"
fi
if [ -z "$KEY" ]; then
  read -rsp "LLM API Key（不回显，粘贴后回车）: " KEY || true
  echo
  SRC="本次输入"
  if [ -n "$KEY" ]; then
    read -rp "存进 macOS 钥匙串，以后不用再输？[y/N] " yn || yn=""
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
      security add-generic-password -s "$KEYCHAIN_SERVICE" -a "$USER" -w "$KEY" -U
      echo "已存入钥匙串（服务名 $KEYCHAIN_SERVICE）。要删：security delete-generic-password -s $KEYCHAIN_SERVICE"
      SRC="钥匙串"
    fi
  fi
fi
if [ -z "$KEY" ]; then
  echo "没有 key，主服务能启动但提问会失败。继续启动。"
else
  echo "LLM key 来源：$SRC"
fi

export DEEPSEEK_API_KEY="$KEY"
exec python -m app.main
