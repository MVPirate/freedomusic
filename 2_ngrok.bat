@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ========================================
echo Запуск Ngrok туннеля...
echo ВНИМАНИЕ: Если ссылка изменится, обнови WEBAPP_STUB_URL в bot.py!
echo ========================================
.\ngrok http 8000
pause