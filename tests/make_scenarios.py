#!/usr/bin/env python3
"""造几个"事故现场"，用来验收恢复程序：

  delete.img     删掉若干文件（目录项 in-use 位清零，簇被回收）
  crash_fat.img  FAT 与分配位图全被清零（模拟没弹盘就拔、元数据没刷盘）
  crash_meta.img 在 crash_fat 基础上，再把两个目录项/目录簇抹掉（目录都丢了）

用法：python3 tests/make_scenarios.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from datarec.device import Source              # noqa: E402
from datarec.exfat import ExFatVolume          # noqa: E402

IMG_DIR = os.path.join(HERE, "img")
BASE = os.path.join(IMG_DIR, "base.img")

DELETE_LIST = [
    "photos/IMG_0001.jpg",
    "photos/sub/IMG_0002.png",
    "videos/clip_moovend.mov",
    "docs/notes.docx",
    "packages/installer.pkg",
]


def sh(cmd: list[str], **kw) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if out.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(cmd)}\n{out.stderr}")
    return out.stdout


def attach(image: str) -> str:
    out = sh(["hdiutil", "attach", "-nomount", "-imagekey",
              "diskimage-class=CRawDiskImage", image])
    return out.split()[0].strip()


def detach(dev: str) -> None:
    subprocess.run(["diskutil", "unmountDisk", dev], capture_output=True)
    subprocess.run(["hdiutil", "detach", dev, "-quiet"], capture_output=True)


def manifest(image: str) -> dict:
    """挂载一份干净镜像，算出所有原始文件的 sha256 清单。"""
    dev = attach(image)
    mnt = sh(["diskutil", "mount", dev]).strip().split()[-1]
    if not mnt.startswith("/Volumes/"):
        for line in sh(["mount"]).splitlines():
            if dev in line or dev.replace("disk", "rdisk") in line:
                mnt = line.split(" on ")[1].split(" (")[0]
    data = {}
    for root, _dirs, files in os.walk(mnt):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, mnt)
            if rel.startswith(".") or "__MACOSX" in rel:
                continue
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(4 << 20), b""):
                    h.update(chunk)
            data[rel] = {"size": os.path.getsize(full), "sha256": h.hexdigest()}
    detach(dev)
    return data


def make_delete() -> None:
    dst = os.path.join(IMG_DIR, "delete.img")
    shutil.copyfile(BASE, dst)
    dev = attach(dst)
    sh(["diskutil", "mount", dev])
    mnt = "/Volumes/DATARECTEST"
    for rel in DELETE_LIST:
        target = os.path.join(mnt, rel)
        if os.path.exists(target):
            os.unlink(target)
            print(f"  删除 {rel}")
    detach(dev)
    print(f"  → {dst}")


def zero_metadata_fat(img: str) -> None:
    with open(img, "r+b") as fh:
        fh.seek(3)
        assert fh.read(8) == b"EXFAT   ", "不是 exFAT 镜像"
        fh.seek(0)
        boot = fh.read(512)
        fat_off = struct.unpack_from("<I", boot, 80)[0] * (1 << boot[108])
        fat_len = struct.unpack_from("<I", boot, 84)[0] * (1 << boot[108])
        heap = struct.unpack_from("<I", boot, 88)[0] * (1 << boot[108])
        root = struct.unpack_from("<I", boot, 96)[0]
        cs = (1 << boot[108]) * (1 << boot[109])
        # FAT 全清（等价于"FAT 根本没来得及写回"）
        fh.seek(fat_off)
        fh.write(b"\x00" * fat_len)
        # 分配位图所在簇也清掉位图内容（保留目录项里的指针）
        fh.seek(heap + (2 - 2) * cs)
        fh.write(b"\x00" * cs)
        print(f"  已清零 FAT({fat_len} 字节) 与位图簇；root={root}")


def kill_dir_entries(img: str) -> None:
    """把 root 里 docs 的目录项、以及 photos 目录簇里的前几个条目抹掉。"""
    src = Source(img)
    vol = ExFatVolume.open(src)
    root = vol._root_spec()
    slots = vol.read_directory(root)
    targets = []
    for i in range(0, len(slots)):
        raw = slots[i]
        if raw[0] != 0x85:
            continue
        ent, _ = vol._parse_set(slots, i, "/", 0)
        if ent and ent.name in ("docs", "photos"):
            targets.append((ent.name, ent, i))
    src.close()

    with open(img, "r+b") as fh:
        boot = fh.read(512)
        heap = struct.unpack_from("<I", boot, 88)[0] * (1 << boot[108])
        root_clus = struct.unpack_from("<I", boot, 96)[0]
        cs = (1 << boot[108]) * (1 << boot[109])
        for name, ent, i in targets:
            off = heap + (ent.first_cluster - 2) * cs        # 该目录自己的簇
            root_off = heap + (root_clus - 2) * cs + i * 32  # root 里指向它的目录项
            fh.seek(root_off if name == "docs" else off)
            if name == "photos":
                # photos 目录原本有 1 簇，前 6 个槽（IMG_0001/scan/…）抹成 0
                fh.write(b"\x00" * 32 * 6)
                print(f"  抹掉 photos 目录簇前 6 个目录项")
            else:
                # 把 docs 目录项本身清零（含从属项）
                fh.write(b"\x00" * 32 * (1 + ent.secondaries))
                print(f"  抹掉 root 中 docs 的目录项（{1 + ent.secondaries} 个槽）")


def make_crash() -> None:
    dst = os.path.join(IMG_DIR, "crash_fat.img")
    shutil.copyfile(BASE, dst)
    zero_metadata_fat(dst)
    print(f"  → {dst}")

    dst2 = os.path.join(IMG_DIR, "crash_meta.img")
    shutil.copyfile(BASE, dst2)
    zero_metadata_fat(dst2)
    kill_dir_entries(dst2)
    print(f"  → {dst2}")


def main() -> None:
    if not os.path.exists(BASE):
        raise SystemExit("先跑 tests/make_samples.py 生成 base.img")
    print("生成原始文件清单…")
    man = manifest(BASE)
    with open(os.path.join(IMG_DIR, "manifest.json"), "w") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    print(f"  {len(man)} 个文件")

    print("场景 1：删除文件")
    make_delete()
    print("场景 2：误拔（FAT/位图未刷盘）")
    make_crash()


if __name__ == "__main__":
    main()
