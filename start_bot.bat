@echo off
title 19Embed Bot - Auto Restart
setlocal

:loop
echo [%date% %time%] Starting bot...
call .venv\Scripts\activate.bat
python -u "main.py"

rem Exit code 2 is a configuration or token failure; restarting cannot fix it.
if %ERRORLEVEL%==2 (
    echo [%date% %time%] Configuration error, not restarting. Check logs\bot.log
    pause
    exit /b 2
)
if %ERRORLEVEL%==0 (
    echo [%date% %time%] Clean shutdown.
    exit /b 0
)

echo [%date% %time%] Bot exited with code %ERRORLEVEL%. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
