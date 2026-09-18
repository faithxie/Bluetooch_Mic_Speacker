@echo off
title Bluetooth Audio Router
cd /d "%~dp0"

echo Starting Bluetooth Audio Router...
echo.

if exist "venv\Scripts\python.exe" (
    echo [INFO] Using venv Python
    venv\Scripts\python.exe main.py
) else (
    echo [INFO] Using system Python
    py -3 main.py
)

if errorlevel 1 (
    echo.
    echo [ERROR] Program exited with code %errorlevel%
    pause
)
