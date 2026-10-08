@echo off
setlocal
rem ------------------------------------------------------------------
rem  Partner Compensation - Current model (20/40/40)
rem  Double-click this file to start the program. Keep the window open
rem  while you use it; closing the window turns the program off.
rem ------------------------------------------------------------------
title Partner Compensation - Current model (20/40/40)
cd /d "%~dp0"
set "PORT=8501"
set "URL=http://localhost:%PORT%"

echo.
echo  ==========================================================
echo   Partner Compensation - Current model (20/40/40)
echo  ==========================================================
echo.

rem --- Already running? Just open the browser. ---------------------
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  The program is already running. Opening it in your browser...
    start "" "%URL%"
    timeout /t 3 >nul
    exit /b 0
)

rem --- Find Python ------------------------------------------------------
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo  Python was not found on this computer.
    echo.
    echo  1. Install Python 3.12 from https://www.python.org/downloads/
    echo  2. On the first installer screen, tick "Add python.exe to PATH".
    echo  3. Then double-click this file again.
    echo.
    pause
    exit /b 1
)

rem --- Install or update the program's parts when needed ---------------
if not exist "data" mkdir "data"
set "NEED_INSTALL=1"
if exist "data\requirements.installed" (
    fc /b "requirements.txt" "data\requirements.installed" >nul 2>&1 && set "NEED_INSTALL="
)
if defined NEED_INSTALL (
    echo  Installing the program's parts. This happens only the first time
    echo  and after updates that need it. Please wait...
    echo.
    %PY% -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  The installation did not complete. Check your internet connection
        echo  and try again. If it keeps failing, send a screenshot of this
        echo  window for help.
        echo.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" "data\requirements.installed" >nul
    echo.
)

rem --- Start --------------------------------------------------------------
echo  Starting... your browser will open at %URL%
echo.
echo  KEEP THIS WINDOW OPEN while you use the program.
echo  To stop the program, close this window.
echo.
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start "" %URL%"
%PY% -m streamlit run app.py --server.port %PORT%

echo.
echo  The program has stopped. If you did not close it yourself, send a
echo  screenshot of the messages above for help.
echo.
pause
