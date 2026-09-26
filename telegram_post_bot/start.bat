@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (echo Сначала запусти install.bat & pause & exit /b 1)
echo Бот запущен. Не закрывай это окно — иначе бот остановится.
.venv\Scripts\python bot.py
pause
