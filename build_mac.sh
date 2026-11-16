#!/bin/bash
# 在 macOS 上打包成可双击运行的 DataRec.app（顺带可选做成 .dmg）
#
# 注意：这里打出来的是 **macOS 的程序**，不是 exe。
# PyInstaller 不能跨平台编译，Windows 的 exe 必须由 Windows 来打，
# 见 .github/workflows/build-windows.yml 或项目里的 build_windows.bat。
#
# 用法：
#   bash build_mac.sh            # 只出 DataRec.app
#   bash build_mac.sh --dmg      # 额外做一个分发包 DataRec.dmg
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "==> 用 $($PY --version 2>&1) 打包（架构 $(uname -m)）"

if ! $PY -m PyInstaller --version >/dev/null 2>&1; then
    echo "==> 安装 PyInstaller（只在打包时需要，运行本身不依赖它）"
    $PY -m pip install --upgrade pyinstaller
fi

echo "==> 开始打包"
# --windowed          : 生成 .app 包，双击不弹终端
# --osx-bundle-id     : 给个标识，方便系统记住权限设置
# --collect-all datarec : 确保子模块全都被收进去
$PY -m PyInstaller --noconfirm --clean \
    --windowed \
    --name DataRec \
    --osx-bundle-identifier com.datarec.gui \
    datarec_gui.py

APP="dist/DataRec.app"
[ -d "$APP" ] || { echo "打包失败：没生成 $APP"; exit 1; }

# 没签名的包首次打开会被 Gatekeeper 拦，去掉隔离标记就能直接双击
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo
echo "==> 完成：$APP"
echo "    双击即可运行；也可以拖到「应用程序」文件夹。"
echo "    读物理磁盘需要在终端里跑： sudo $APP/Contents/MacOS/DataRec"
echo "    （对镜像文件操作不需要管理员权限，直接双击就行）"

if [ "${1:-}" = "--dmg" ]; then
    echo "==> 打 dmg"
    rm -f dist/DataRec.dmg
    hdiutil create -volname "DataRec" -srcfolder "$APP" \
                   -ov -format UDZO dist/DataRec.dmg >/dev/null
    echo "==> 完成：dist/DataRec.dmg"
fi
