@echo off
rem Runs the bot, logging to bot.log, restarting itself if it crashes.
cd /d "%~dp0"
set PYTHONUTF8=1
:loop
echo [%date% %time%] starting claude-tg >> bot.log
".venv\Scripts\python.exe" bot.py >> bot.log 2>&1
echo [%date% %time%] bot exited with %errorlevel%, restarting in 10s >> bot.log
timeout /t 10 /nobreak > nul
goto loop
