@echo off
rem 让 cmd 用 UTF-8 显示，否则中文提示在简中 Windows 上是乱码
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
cd /d "%~dp0"

title HTE Studio

echo.
echo   HTE Studio
echo   ----------------------------------------
echo.

rem ---- 1. 找一个可用的 Python (>= 3.11) -------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo   [x] 没有找到 Python 3.11 或更高版本。
    echo.
    echo       请到 https://www.python.org/downloads/ 安装 Python 3.11+，
    echo       安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

rem ---- 2. 首次运行：建虚拟环境 ---------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   [1/3] 首次运行，正在创建虚拟环境...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo   [x] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo   [1/3] 虚拟环境就绪
)

set "VPY=.venv\Scripts\python.exe"

rem 虚拟环境可能坏掉（比如系统 Python 被升级过），先确认它还能跑
"%VPY%" -c "pass" >nul 2>&1
if errorlevel 1 (
    echo   [!] 虚拟环境不可用，正在重建...
    rmdir /s /q .venv
    %PY% -m venv .venv
    if errorlevel 1 (
        echo   [x] 重建虚拟环境失败。
        pause
        exit /b 1
    )
)

rem ---- 3. 依赖：用 requirements.txt 的哈希判断要不要重装 -------------
set "STAMP=.venv\.deps-hash"
for /f %%h in ('%PY% -c "import hashlib;print(hashlib.sha256(open(r'requirements.txt','rb').read()).hexdigest()[:16])"') do set "REQHASH=%%h"

set "OLDHASH="
if exist "%STAMP%" set /p OLDHASH=<"%STAMP%"

if not "%OLDHASH%"=="%REQHASH%" (
    echo   [2/3] 正在安装依赖，首次约需 1 分钟...
    "%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
    "%VPY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo   [x] 依赖安装失败。常见原因：没有联网，或公司网络需要代理。
        echo       可以手动执行： .venv\Scripts\python.exe -m pip install -r requirements.txt
        echo       如果还是不行，删掉 .venv 文件夹再双击一次。
        echo.
        pause
        exit /b 1
    )
    >"%STAMP%" echo %REQHASH%
) else (
    echo   [2/3] 依赖就绪
)

rem ---- 4. 起服务（浏览器由 app 自己拉起）-----------------------------
echo   [3/3] 正在启动...
echo.
"%VPY%" -m app.main
echo.
echo   服务已停止。
pause
