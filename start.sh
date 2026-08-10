#!/usr/bin/env bash
# DoctorAgent 开发环境启动脚本（跨平台：Linux / macOS / Windows WSL）
# 用法: cd 项目根目录 && bash start.sh
#
# 安全约定：
#   - 绝不在此文件内硬编码任何密钥 / 口令 / Token。
#   - 口令优先取自 git-ignored 的本地 .env；若未设置，则在本机数据目录
#     .doctoragent/ 下生成并持久化一个强随机口令（该目录已被 .gitignore 忽略，
#     不会被提交）。
#   - API Token 留空即本地开发关闭鉴权（符合 .env.example 语义）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="$ROOT/.doctoragent"

# 确保数据目录存在（不含任何密钥，仅运行时产物）
mkdir -p "$DATA/vault" "$DATA/inbox" "$DATA/index" "$DATA/logs" "$DATA/Config"

# ── 读取本地 .env（git-ignored，绝不提交）──
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

export DOCTORAGENT_ENV="${DOCTORAGENT_ENV:-dev}"
export DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER="${DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER:-FilePassword}"
export DOCTORAGENT_PATHS__VAULT="${DOCTORAGENT_PATHS__VAULT:-$DATA/vault}"
export DOCTORAGENT_PATHS__INBOX="${DOCTORAGENT_PATHS__INBOX:-$DATA/inbox}"
export DOCTORAGENT_PATHS__INDEX="${DOCTORAGENT_PATHS__INDEX:-$DATA/index}"
export DOCTORAGENT_PATHS__LOGS="${DOCTORAGENT_PATHS__LOGS:-$DATA/logs}"
export DOCTORAGENT_PATHS__CONNECTIONS="${DOCTORAGENT_PATHS__CONNECTIONS:-$DATA/Config/connections.json}"
export DOCTORAGENT_PATHS__SETTINGS="${DOCTORAGENT_PATHS__SETTINGS:-$DATA/Config/settings.json}"

# ── 主密钥口令：绝不硬编码 ──
# FilePassword provider 必须提供口令。优先使用 .env 中已设置的值；
# 否则在本地数据目录生成并持久化一个强随机口令（仅本机、git-ignored）。
if [ -z "${DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD:-}" ]; then
  PW_FILE="$DATA/master_key_password"
  if [ -f "$PW_FILE" ]; then
    DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD="$(cat "$PW_FILE")"
  else
    # 32 字节随机熵 -> 64 位十六进制，强度充足
    DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '%s' "$DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD" > "$PW_FILE"
    chmod 600 "$PW_FILE"
    echo "⚠️  未检测到主密钥口令，已在本地生成并保存到："
    echo "    $PW_FILE"
    echo "    （请妥善保管；一旦丢失，金库需重新初始化；轮换口令会重新加密整个金库）"
  fi
  export DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD
fi

# ── API Token：留空 = 本地开发关闭鉴权 ──
# 如需开启，在 .env 中设置 DOCTORAGENT_API_TOKEN，切勿硬编码在此脚本。
if [ -n "${DOCTORAGENT_API_TOKEN:-}" ]; then
  echo "  API Token: (由 .env 提供，出于安全不回显)"
fi

# ── 跨平台定位 venv 入口 ──
DA_BIN=""
if [ -f "$ROOT/.venv/Scripts/doctoragent.exe" ]; then
  DA_BIN="$ROOT/.venv/Scripts/doctoragent.exe"   # Windows
elif [ -f "$ROOT/.venv/bin/doctoragent" ]; then
  DA_BIN="$ROOT/.venv/bin/doctoragent"            # Linux / macOS
elif command -v doctoragent >/dev/null 2>&1; then
  DA_BIN="doctoragent"                            # 已激活的虚拟环境 / 已安装
fi

if [ -z "$DA_BIN" ]; then
  echo "❌ 未找到 doctoragent 可执行文件。请先创建虚拟环境：" >&2
  echo "    python -m venv .venv && .venv/bin/activate && pip install -e \".[server,clinical]\"" >&2
  exit 1
fi

echo "DoctorAgent starting on http://127.0.0.1:8000"
echo "  Console: http://127.0.0.1:8000/console/"
echo "  API Docs: http://127.0.0.1:8000/docs"
echo ""

exec "$DA_BIN" serve --host 127.0.0.1 --port 8000
