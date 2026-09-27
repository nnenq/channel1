@echo off
chcp 65001 >nul
cd /d "%~dp0"
where git >nul 2>nul || (
  echo Git не установлен. Скачай: https://git-scm.com/download/win
  pause
  exit /b 1
)
echo Скачиваю обновления...
git pull || (
  echo.
  echo Не получилось обновиться - скинь скриншот этого окна.
  pause
  exit /b 1
)
echo Обновляю библиотеки...
.venv\Scripts\python -m pip install -r requirements.txt -q --no-cache-dir --disable-pip-version-check
echo.
echo Готово! Закрой окно бота (если открыто) и запусти start.bat заново.
pause
