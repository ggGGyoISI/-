@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка бота для постов ===
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python не найден. Скачай его с https://www.python.org/downloads/
  echo При установке ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH".
  echo Потом запусти install.bat ещё раз.
  start https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv (
  echo Создаю окружение...
  python -m venv .venv || (echo Ошибка создания окружения & pause & exit /b 1)
)
echo Устанавливаю библиотеки (1-3 минуты)...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt || (echo Ошибка установки библиотек & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo.
echo === Готово! ===
echo Сейчас откроется файл настроек .env — впиши BOT_TOKEN, LLM_API_KEY и ADMIN_IDS,
echo сохрани (Ctrl+S) и закрой. Потом запускай бота файлом start.bat
notepad .env
pause
