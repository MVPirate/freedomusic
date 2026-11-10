@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo Запускаю все процессы в одном окне Windows Terminal...

:: Запускаем 3 вкладки в одном окне (-w 0)
wt -w 0 nt --title "1. API" -d . cmd /k "1_api.bat" ; nt --title "2. Ngrok" -d . cmd /k "2_ngrok.bat" ; nt --title "3. Bot" -d . cmd /k "3_bot.bat"

exit