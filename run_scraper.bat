@echo off
chcp 65001 >nul 2>&1

set "WORKDIR=C:\Users\user\Downloads\web-scrape-main\web-scrape-main"
set "PYTHON=C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "LOGFILE=%WORKDIR%\scraper_log.txt"

:: Wait for network connectivity (up to 5 minutes after wake-from-sleep)
set ATTEMPTS=0
:check_network
ping -n 1 -w 3000 www.momoshop.com.tw >nul 2>&1
if %ERRORLEVEL%==0 goto network_ready
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 30 (
    echo [bat] Network not available after 5 minutes, aborting. >> "%LOGFILE%"
    exit /b 1
)
echo [bat] Waiting for network... attempt %ATTEMPTS%/30 >> "%LOGFILE%"
timeout /t 10 /nobreak >nul
goto check_network

:network_ready
echo ============================================ >> "%LOGFILE%"
echo Run started: %DATE% %TIME% >> "%LOGFILE%"
echo ============================================ >> "%LOGFILE%"

cd /d "%WORKDIR%"
"%PYTHON%" scraper.py >> "%LOGFILE%" 2>&1

echo Exit code: %ERRORLEVEL% >> "%LOGFILE%"
echo. >> "%LOGFILE%"
