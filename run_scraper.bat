@echo off
chcp 65001 >nul 2>&1

set "WORKDIR=C:\Users\user\Downloads\web-scrape-main\web-scrape-main"
set "PYTHON=C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "LOGFILE=%WORKDIR%\scraper_log.txt"

:: Wait 30 seconds for network to connect after wake-from-sleep
echo Waiting 30s for network... >> "%LOGFILE%"
timeout /t 30 /nobreak >nul

echo ============================================ >> "%LOGFILE%"
echo Run started: %DATE% %TIME% >> "%LOGFILE%"
echo ============================================ >> "%LOGFILE%"

cd /d "%WORKDIR%"
"%PYTHON%" scraper.py >> "%LOGFILE%" 2>&1

echo Exit code: %ERRORLEVEL% >> "%LOGFILE%"
echo. >> "%LOGFILE%"
