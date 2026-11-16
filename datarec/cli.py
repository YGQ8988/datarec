"""命令行入口。

用法示例：
    python3 datarec.py info    -s /dev/rdisk4s1
    python3 datarec.py list    -s /dev/rdisk4s1 --deleted
    python3 datarec.py recover -s /dev/rdisk4s1 -o ~/Desktop/救回的数据
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, oslayer
from .carve import carve_dmg, scan as carve_scan
from .device import Source, SourceError
from .exfat import ExFatVolume
from .formats import CATEGORIES, category_of
from .recover import Recoverer
from .report import human, summary_text, write_csv, write_markdown

MiB = 1024 * 1024


def _open_source(args) -> tuple[Source, ExFatVolume]:
    src = Source(args.source, offset=args.offset or 0)
    vol = ExFatVolume.open(src, offset=args.offset)
    return src, vol


def _preflight(src: Source, out_dir: str | None, force: bool) -> None:
    mounts = src.mount_points()
    if mounts and not force:
        print(f"注意：数据源当前处于挂载状态（{', '.join(mounts)}）。", file=sys.stderr)
        print("      恢复前请先卸载，避免系统缓存把元数据又写回盘上：", file=sys.stderr)
        if oslayer.IS_WINDOWS:
            print("          在「磁盘管理」里把该卷设为脱机，或先安全弹出再重新插上",
                  file=sys.stderr)
        else:
            dev = os.path.basename(src.path)
            print(f"          diskutil unmountDisk /dev/{dev.lstrip('r')}", file=sys.stderr)
        print("      （确认已卸载后加 --force 可跳过本提示）", file=sys.stderr)
    if out_dir:
        real_out = os.path.realpath(os.path.abspath(os.path.expanduser(out_dir)))
        for m in mounts:
            real_m = os.path.realpath(m)
            if real_out == real_m or real_out.startswith(real_m + os.sep):
                raise SystemExit(
                    f"输出目录 {out_dir} 就在要恢复的那个卷上！\n"
                    "往待恢复的盘里写文件会覆盖还没救出来的数据，换个位置（比如内置硬盘）。")


def cmd_info(args) -> int:
    src, vol = _open_source(args)
    _preflight(src, None, args.force)
    print("卷信息")
    print("─" * 62)
    for k, v in vol.info.summary().items():
        print(f"  {k:<14}{v}")

    files, dirs, warnings = vol.walk()
    live = [f for f in files if not f.deleted]
    gone = [f for f in files if f.deleted]

    print("\n目录统计")
    print("─" * 62)
    print(f"  活动文件 {len(live)} 个，已删除（可恢复）{len(gone)} 个，目录 {len(dirs)} 个")
    print(f"  活动文件占用 {human(sum(f.size for f in live))}，"
          f"已删除文件曾占用 {human(sum(f.size for f in gone))}")

    cats: dict[str, list[int]] = {}
    for f in live + gone:
        c = category_of(f.name)
        row = cats.setdefault(c, [0, 0])
        row[0] += 1
        row[1] += f.size
    print("\n  按类别：")
    for c, (n, b) in sorted(cats.items(), key=lambda x: -x[1][1]):
        print(f"    {CATEGORIES.get(c, c):<6}{n:>5} 个  {human(b)}")

    # ---- 一致性体检：FAT 链 vs 目录项声明
    print("\n健康检查")
    print("─" * 62)
    broken = 0
    nocheck = 0
    for f in live:
        if not vol.valid_cluster(f.first_cluster) or f.size == 0:
            continue
        if f.no_fat_chain:
            continue
        need = max(1, -(-f.size // vol.cs))
        chain, problem = vol.follow_chain(f.first_cluster, need)
        if problem:
            broken += 1
        nocheck += 1
    if broken:
        print(f"  · {broken} 个文件的 FAT 链不完整（典型的「没弹盘就拔」痕迹）——这些会走连续簇兜底")
    elif nocheck:
        print(f"  · {nocheck} 个链式文件 FAT 链完整")
    print(f"  · {sum(1 for f in live if f.no_fat_chain)} 个文件标记为连续分配（NoFatChain），"
          "这类文件即便 FAT 丢了也能整段读出来")
    if vol.info.volume_flags & 0x2:
        print("  · 卷的脏标志位是打开的：上次没有正常卸载，符合误拔特征")
    if warnings:
        print(f"  · {len(warnings)} 条目录结构告警（多数是已删除目录项指回同一簇）")
    print("\n下一步：python3 datarec.py recover -s <数据源> -o <输出目录>")
    return 0


def cmd_list(args) -> int:
    src, vol = _open_source(args)
    _preflight(src, None, args.force)
    files, dirs, _ = vol.walk()
    want_types = set(args.types.split(",")) if args.types else None
    rows = []
    for f in files:
        if args.deleted and not f.deleted:
            continue
        if args.live and f.deleted:
            continue
        if want_types and category_of(f.name) not in want_types:
            continue
        rows.append(f)
    print(f"{'状态':<4} {'类型':<6} {'大小':>10}  {'簇':>8}  {'链':<10} 路径")
    print("─" * 96)
    for f in sorted(rows, key=lambda x: x.path):
        state = "删除" if f.deleted else "活动"
        kind = "目录" if f.is_dir else CATEGORIES.get(category_of(f.name), "其他")
        chain = "连续" if f.no_fat_chain else "FAT链"
        print(f"{state:<4} {kind:<6} {human(f.size):>10}  {f.first_cluster:>8}  {chain:<10} {f.path}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_gui(args) -> int:
    """拉起图形界面。"""
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(f"打不开图形界面：{exc}", file=sys.stderr)
        print("tkinter 是标准库；Linux 上需要先装 python3-tk。", file=sys.stderr)
        return 1
    return gui_main()


def cmd_disks(args) -> int:
    """列出可扫描的磁盘与分区，省得用户自己去翻设备名。"""
    items = oslayer.list_sources(include_all=args.all)
    if not items:
        print("没找到可用的磁盘。可以把盘做成镜像文件，然后用 -s 指向镜像。")
        return 0
    print(f"{'设备':<26} {'容量':>10}  {'状态':<8} 说明")
    print("─" * 88)
    for it in items:
        state = "已挂载" if it["mounted"] else "未挂载"
        print(f"{it['path']:<26} {oslayer.human_size(it['size']):>10}  "
              f"{state:<8} {it['title'].strip()}")
        if it.get("detail"):
            print(f"{'':<26} {'':>10}  {'':<8} {it['detail']}")
    print("\n提示：用 -s 指定设备，例如 "
          + (r'-s \\.\PhysicalDrive1' if oslayer.IS_WINDOWS else "-s /dev/rdisk4s1"))
    if not oslayer.is_admin():
        print("      读物理磁盘需要管理员权限：Windows 请右键「以管理员身份运行」，"
              "macOS 请用 sudo。")
    return 0


def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-s", "--source", required=True,
                   help="数据源：磁盘设备（如 /dev/rdisk4s1）或镜像文件")
    p.add_argument("--offset", type=int, default=None,
                   help="引导扇区在数据源内的偏移（默认自动探测，含 GPT/MBR 分区表）")
    p.add_argument("--force", action="store_true",
                   help="跳过「源卷仍在挂载」等安全提示")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="datarec",
        description="exFAT 数据恢复工具（只读扫描，不写源盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 datarec.py disks                                   列出可扫描的磁盘
  python3 datarec.py info    -s /dev/rdisk4s1                看卷信息与体检
  python3 datarec.py list    -s /dev/rdisk4s1 --deleted       只看已删除文件
  python3 datarec.py recover -s /dev/rdisk4s1 -o ~/Desktop/救回的数据
  python3 datarec.py recover -s disk.img -o ./out --types images,videos
  （Windows 上把 /dev/rdisk4s1 换成 \\\\.\\PhysicalDrive1 或 \\\\.\\F: 这样的路径）
""")
    ap.add_argument("--version", action="version", version=f"datarec {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_disks = sub.add_parser("disks", help="列出磁盘与分区，挑数据源用")
    p_disks.add_argument("--all", action="store_true", help="连内置系统盘一起列出")
    p_disks.set_defaults(func=cmd_disks)

    p_gui = sub.add_parser("gui", help="打开图形界面")
    p_gui.set_defaults(func=cmd_gui)

    p_info = sub.add_parser("info", help="看卷信息与可恢复性体检")
    _common_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_list = sub.add_parser("list", help="列出文件条目（含已删除）")
    _common_args(p_list)
    p_list.add_argument("--deleted", action="store_true", help="只看已删除")
    p_list.add_argument("--live", action="store_true", help="只看活动文件")
    p_list.add_argument("--types", help="按类别过滤，如 images,videos")
    p_list.set_defaults(func=cmd_list)

    p_rec = sub.add_parser("recover", help="执行恢复")
    _common_args(p_rec)
    p_rec.add_argument("-o", "--out", default=None,
                       help="输出目录（默认 ./recovered_<时间戳>）")
    p_rec.add_argument("--types", default=None,
                       help="只恢复这些类别：images,videos,documents,archives,diskimages,packages,audio")
    p_rec.add_argument("--min-size", type=int, default=0, help="小于该字节数的文件跳过")
    p_rec.add_argument("--start", type=int, default=0, help="雕刻起始偏移（字节）")
    p_rec.add_argument("--end", type=int, default=None, help="雕刻结束偏移（字节）")
    p_rec.add_argument("--no-carve", action="store_true", help="只按目录项恢复，不做全盘雕刻")
    p_rec.add_argument("--carve-all", action="store_true",
                       help="雕刻时不跳过已恢复文件占用的区域（更慢，但能发现藏在里的文件）")
    p_rec.add_argument("--deep-verify", action="store_true",
                       help="对每个文件做完整结构复算（更准、更慢）")
    p_rec.add_argument("--keep-appledouble", action="store_true", help="保留 ._ 开头的资源叉文件")
    p_rec.add_argument("--keep-system", action="store_true", help="保留系统目录（.fseventsd 等）")
    p_rec.add_argument("--dry-run", action="store_true", help="只演练，不真正落盘")
    p_rec.add_argument("-q", "--quiet", action="store_true", help="不逐个文件打印进度")
    p_rec.set_defaults(func=cmd_recover)

    p_carve = sub.add_parser("carve", help="跳过文件系统，纯按内容雕刻")
    _common_args(p_carve)
    p_carve.add_argument("-o", "--out", required=True, help="输出目录")
    p_carve.add_argument("--types", default=None)
    p_carve.add_argument("--min-size", type=int, default=4096)
    p_carve.set_defaults(func=cmd_carve)
    return ap


def cmd_recover(args) -> int:
    out_dir = args.out or os.path.join(os.getcwd(),
                                       "recovered_" + time.strftime("%Y%m%d_%H%M%S"))
    src, vol = _open_source(args)
    _preflight(src, out_dir, args.force)

    types = set(args.types.split(",")) if args.types else None
    if types:
        bad = types - set(CATEGORIES)
        if bad:
            print(f"未知类别：{', '.join(sorted(bad))}；可选：{', '.join(CATEGORIES)}", file=sys.stderr)
            return 2

    print("exFAT 数据恢复")
    print("─" * 62)
    print(f"  数据源   {src.path}")
    print(f"  卷大小   {human(vol.info.size_bytes)}，簇大小 {human(vol.info.cluster_size)}")
    print(f"  输出到   {out_dir}")
    print(f"  模式     {'只演练' if args.dry_run else '实际写出'}"
          f"{'' if not args.no_carve else '，不做雕刻'}")
    print()

    rec = Recoverer(
        src, vol, out_dir,
        types=types,
        min_size=args.min_size,
        deep_verify=args.deep_verify,
        keep_appledouble=args.keep_appledouble,
        keep_system=args.keep_system,
        carve=not args.no_carve,
        carve_all=args.carve_all,
        dry_run=args.dry_run,
        offset_start=args.start or 0,
        offset_end=args.end,
        verbose=not args.quiet,
    )
    try:
        results = rec.run()
    except KeyboardInterrupt:
        print("\n已中断，正在写入已完成的恢复报告…", file=sys.stderr)
        results = rec.results

    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
        write_csv(os.path.join(out_dir, "report.csv"), results)
        write_markdown(os.path.join(out_dir, "report.md"), results, rec.stats,
                       vol.info.summary(), src.path, out_dir,
                       {"类别": args.types or "全部", "深度校验": args.deep_verify,
                        "雕刻": not args.no_carve})
        # 清掉临时目录
        partial = os.path.join(out_dir, ".partial")
        if os.path.isdir(partial) and not os.listdir(partial):
            os.rmdir(partial)

    print(summary_text(results, rec.stats, vol.info.summary()))
    if not args.dry_run:
        print(f"  报告：{os.path.join(out_dir, 'report.md')}")
        print(f"        {os.path.join(out_dir, 'report.csv')}")
        print("  目录：files/（活动文件，保留原路径）  deleted/（已删除）  "
              "carved/（纯雕刻）  带「可疑」后缀的需人工确认")
    return 0


def cmd_carve(args) -> int:
    src, vol = _open_source(args)
    _preflight(src, args.out, args.force)
    types = set(args.types.split(",")) if args.types else None
    start = vol.info.offset + vol.info.heap_offset
    end = min(src.device_size or vol.info.size_bytes, vol.info.offset + vol.info.size_bytes)
    print(f"纯雕刻：{human(end - start)} 待扫描…")
    items = carve_scan(src, start, end, min_size=args.min_size, categories=types)
    items.extend(carve_dmg(src, start, end))
    os.makedirs(args.out, exist_ok=True)
    for item in items:
        cat_dir = os.path.join(args.out, item.category)
        os.makedirs(cat_dir, exist_ok=True)
        stem = item.ext if item.fmt == "zip" else item.fmt
        name = f"{stem}_{item.offset:012x}.{item.ext}"
        with open(os.path.join(cat_dir, name), "wb") as fh:
            remaining, pos = item.size, item.offset
            while remaining > 0:
                chunk = src.read(pos, min(4 * MiB, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
                pos += len(chunk)
        print(f"  {item.category:<12}{name:<36}{human(item.size)}  {item.conf}")
    print(f"共雕出 {len(items)} 个文件 → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except SourceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
