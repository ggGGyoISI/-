#!/usr/bin/env bash
# Установка для macOS / Linux
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Установи Python 3.10+: https://www.python.org/downloads/"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Готово! Впиши BOT_TOKEN, LLM_API_KEY и ADMIN_IDS в файл .env, затем запусти ./start.sh"
