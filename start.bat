@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Перезалив Shorts - бот (не закрывай окно)
if not exist .venv (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
rem YouTube часто меняет сайт - обновляем загрузчик при каждом запуске
.venv\Scripts\python -m pip install -U yt-dlp -q --disable-pip-version-check

:loop
.venv\Scripts\python -m reuploader.bot
if errorlevel 3 if not errorlevel 4 (
  pause
  exit /b 3
)
echo.
echo Бот остановился. Перезапуск через 15 секунд (закрой окно, чтобы выключить совсем)...
timeout /t 15 >nul
goto loop
