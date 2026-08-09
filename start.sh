#!/usr/bin/env bash
# DoctorAgent 开发环境启动脚本
# 用法: cd 项目根目录 && bash start.sh
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="$ROOT/.doctoragent"

# 确保数据目录存在
mkdir -p "$DATA/vault" "$DATA/inbox" "$DATA/index" "$DATA/logs" "$DATA/Config"

export DOCTORAGENT_ENV=dev
export DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER=FilePassword
export DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD='local-dev-master-key-2026'
export DOCTORAGENT_PATHS__VAULT="$DATA/vault"
export DOCTORAGENT_PATHS__INBOX="$DATA/inbox"
export DOCTORAGENT_PATHS__INDEX="$DATA/index"
export DOCTORAGENT_PATHS__LOGS="$DATA/logs"
export DOCTORAGENT_PATHS__CONNECTIONS="$DATA/Config/connections.json"
export DOCTORAGENT_PATHS__SETTINGS="$DATA/Config/settings.json"
export DOCTORAGENT_API_TOKEN='dev-local-audit-token-2026'

echo "DoctorAgent starting on http://127.0.0.1:8000"
echo "  Console: http://127.0.0.1:8000/console/"
echo "  API Docs: http://127.0.0.1:8000/docs"
echo "  API Token: $DOCTORAGENT_API_TOKEN"
echo ""

exec doctoragent serve --host 127.0.0.1 --port 8000
