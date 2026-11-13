#!/usr/bin/env bash
# Временный общедоступный адрес для показа с любого компьютера с Docker: сервис в режиме стенда и туннель
# Cloudflare без регистрации. Адрес вида https://<слова>.trycloudflare.com печатается ниже и действует,
# пока работает этот скрипт и включён компьютер.
set -euo pipefail
cd "$(dirname "$0")/.."
export NMCD_PUBLIC=1 NMCD_MAX_QUEUE="${NMCD_MAX_QUEUE:-4}" LLM_DAILY_TOKEN_BUDGET="${LLM_DAILY_TOKEN_BUDGET:-2000000}"
docker compose up -d --build
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Нужна программа cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  exit 1
fi
exec cloudflared tunnel --no-autoupdate --url "http://localhost:${NMCD_PORT:-8000}"
