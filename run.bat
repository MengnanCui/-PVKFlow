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

rem ---- 1. 找一个可用的 Python（3.11 到 3.15）--------------------------
rem 依赖只在这几个版本上都有 Windows 预编译包（见 requirements.txt 开头）。
rem 不能直接用 py -3：它给的是这台电脑上最新的 Python，可能比依赖支持的还新，
rem 那样 pip 会去从源码编译，没有编译器就一直卡在「正在安装依赖」。
rem 装了好几个时从新到旧挑第一个在范围内的。改范围要连 run.sh、CI 一起改。
set "PYOK=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 15) else 1)"
set "PY="
for %%v in (3.15 3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%v -c "!PYOK!" >nul 2>&1
        if not errorlevel 1 set "PY=py -%%v"
    )
)

if not defined PY (
    python -c "!PYOK!" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo   [x] 没有找到 Python 3.11 到 3.15。
    for /f "delims=" %%o in ('py -3 --version 2^>nul') do echo       这台电脑上的 py -3 是 %%o，不在支持范围内。
    for /f "delims=" %%o in ('python --version 2^>nul') do echo       这台电脑上的 python 是 %%o，不在支持范围内。
    echo.
    echo       请到 https://www.python.org/downloads/ 安装 Python 3.13
    echo       （3.11 到 3.15 之间哪个都行），安装时务必勾选 "Add python.exe to PATH"，
    echo       然后重新双击本文件。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%o in ('%PY% --version 2^>^&1') do set "PYVER=%%o"
echo   使用 %PYVER%（%PY%）

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

rem 虚拟环境可能坏掉（比如系统 Python 被升级过），或是以前用不支持的 Python 建的：
rem 先确认它还能跑、版本在范围内
"%VPY%" -c "!PYOK!" >nul 2>&1
if errorlevel 1 (
    echo   [!] 虚拟环境不可用（或 Python 版本不在支持范围内），正在重建...
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
    rem --only-binary：只装预编译包。宁可马上报错，也不要在没有编译器的电脑上编译到天荒地老
    "%VPY%" -m pip install -r requirements.txt --only-binary=:all: --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo   [x] 依赖安装失败。常见原因：
        echo       1. 没有联网，或公司网络需要代理
        echo       2. 某个依赖还没有出这个 Python 版本的安装包 —— 装 Python 3.13 再试
        echo       可以手动执行： .venv\Scripts\python.exe -m pip install -r requirements.txt --only-binary=:all:
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
