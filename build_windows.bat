@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================
echo   exFAT 数据恢复 - Windows 打包
echo ================================================
echo.

rem ---- 找一个能用的 Python（优先 py 启动器，其次 python）
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [错误] 没找到 Python。先到 https://www.python.org/downloads/ 装 3.9 以上版本，
    echo        安装时勾选 "Add Python to PATH"，然后再双击本脚本。
    pause
    exit /b 1
)
echo 使用：%PY%
%PY% --version
echo.

rem ---- 装 PyInstaller（打包工具，只在打包时需要）
echo 检查 PyInstaller...
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo 正在安装 PyInstaller，请稍等...
    %PY% -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败。试试先换国内源：
        echo        %PY% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
        pause
        exit /b 1
    )
)

rem ---- 打包
rem   --noconsole : 双击运行时不弹黑窗口
rem   --onefile   : 打包成单个 exe，方便拷给别人
rem   想要"一打开就自动申请管理员权限"，在下面这行末尾再加一个 --uac-admin
echo.
echo 开始打包...
%PY% -m PyInstaller --noconfirm --clean ^
    --noconsole ^
    --onefile ^
    --name DataRec ^
    datarec_gui.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请把上面的报错发出来。
    pause
    exit /b 1
)

echo.
echo ================================================
echo   完成：dist\DataRec.exe
echo   这个文件可以直接拷到别的 Windows 电脑上运行，
echo   不需要对方装 Python。读物理磁盘时记得右键
echo   选择"以管理员身份运行"。
echo ================================================
echo.
pause
