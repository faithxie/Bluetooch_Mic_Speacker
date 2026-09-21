@echo off
chcp 65001 >nul
title BTSPK 打包工具
cd /d "%~dp0"

echo ============================================================
echo   BTSPK 蓝牙音频路由 - 打包为 EXE
echo ============================================================
echo.

REM ---- 1. 定位 Python ----
set "PY="
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
    echo [1/4] 使用虚拟环境 Python: venv\Scripts\python.exe
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        echo [1/4] 未找到 venv，使用系统 Python: py -3
    ) else (
        echo [错误] 找不到 Python。请先安装 Python 3.10+ 或在项目目录创建 venv。
        pause & exit /b 1
    )
)

REM ---- 2. 检查依赖 ----
echo [2/4] 检查打包依赖...
%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo       未安装 PyInstaller，正在安装...
    %PY% -m pip install pyinstaller
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败，请检查网络。
        pause & exit /b 1
    )
)
%PY% -c "import sounddevice, numpy, scipy" >nul 2>nul
if errorlevel 1 (
    echo       缺少运行依赖，正在安装 requirements.txt...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败。
        pause & exit /b 1
    )
)
echo       依赖检查通过。

REM ---- 3. 清理旧产物 ----
echo [3/4] 清理旧的 build/dist 产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM ---- 4. 打包 ----
echo [4/4] 开始打包（首次约 1-3 分钟，请稍候）...
echo.
%PY% -m PyInstaller --noconfirm --clean build_exe.spec
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上方日志。
    pause & exit /b 1
)

echo.
echo ============================================================
echo   打包完成！
echo ------------------------------------------------------------
echo   产物：dist\BTSPK.exe
echo   直接双击即可运行，可复制到任意 Windows 电脑使用
echo   （无需安装 Python）。
echo.
echo   说明：
echo     - 设备偏好保存在 %%APPDATA%%\BTSPK\.device_prefs.json
echo     - 运行日志保存在 %%APPDATA%%\BTSPK\BTSPK.log
echo ============================================================
echo.

if exist "dist\BTSPK.exe" (
    for %%A in ("dist\BTSPK.exe") do echo   文件大小: %%~zA 字节
    echo.
    choice /c YN /m "是否立即运行 dist\BTSPK.exe"
    if errorlevel 2 goto :done
    start "" "dist\BTSPK.exe"
)

:done
pause
