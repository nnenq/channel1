@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка бота для перезалива Shorts ===

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python не найден. Установи Python 3.11+ с https://www.python.org/downloads/
  echo При установке поставь галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo [1/3] Создаю окружение Python...
  %PY% -m venv .venv || (pause & exit /b 1)
)
echo [2/3] Ставлю библиотеки (пара минут)...
.venv\Scripts\python -m pip install --upgrade pip -q --no-cache-dir
.venv\Scripts\python -m pip install -r requirements.txt -q --no-cache-dir || (pause & exit /b 1)

if not exist bin mkdir bin
if not exist bin\cloudflared.exe (
  echo [3/3] Скачиваю cloudflared для HTTPS-адреса мини-апки...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile bin\cloudflared.exe"
)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Сейчас откроется файл .env - впиши туда BOT_TOKEN от @BotFather и сохрани.
  pause
  notepad .env
)

echo.
echo Готово! Положи client_secret.json от Google в эту папку и запусти start.bat
pause
