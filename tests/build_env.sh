#!/bin/bash
# 重建测试环境：raw exFAT 镜像 → 挂载 → 写样本 → 生成 base.img 与三类事故镜像
set -e
cd "$(dirname "$0")/.."
IMG=tests/img
rm -rf "$IMG"; mkdir -p "$IMG"
mkfile -n 96m "$IMG/test.img"
DEV=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$IMG/test.img" | awk '{print $1}')
echo "设备：$DEV"
newfs_exfat -v DATARECTEST "/dev/r${DEV#/dev/}" >/dev/null
diskutil mount "$DEV" >/dev/null
python3 tests/make_samples.py /Volumes/DATARECTEST
# 补 dmg / pkg / jpg 样本（需要系统工具）
sips -s format jpeg /Volumes/DATARECTEST/photos/IMG_0001.png --out /Volumes/DATARECTEST/photos/IMG_0001.jpg >/dev/null
mkdir -p /tmp/dr_payload && echo payload > /tmp/dr_payload/file.txt
pkgbuild --root /tmp/dr_payload --identifier com.datarec.test --version 1.0 \
         --install-location /tmp/dr /Volumes/DATARECTEST/packages/installer.pkg >/dev/null
hdiutil create -size 8m -fs HFS+ -volname TestDmg -quiet /Volumes/DATARECTEST/packages/app.dmg
diskutil unmount "$DEV" >/dev/null
hdiutil detach "$DEV" -quiet
cp "$IMG/test.img" "$IMG/base.img"
python3 tests/make_scenarios.py
echo "环境就绪：$IMG"
