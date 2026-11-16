"""恢复报告：CSV（给 Excel）+ Markdown（给人看）+ 终端摘要。"""
from __future__ import annotations

import csv
import os
import time
from collections import Counter, defaultdict

from .formats import CATEGORIES

STATUS_LABEL = {
    "ok": "完整",
    "warn": "可用（可能不完整）",
    "suspect": "可疑",
    "skipped": "跳过",
    "duplicate": "重复",
}
KIND_LABEL = {"live": "活动文件", "deleted": "已删除", "carved": "雕刻"}


def human(n: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def write_csv(path: str, results) -> None:
    # utf-8-sig：Excel 打开中文表头不乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["序号", "来源", "状态", "可信度", "类别", "原始路径", "输出文件",
                    "大小(字节)", "SHA256", "扩展名", "备注"])
        for i, r in enumerate(results, 1):
            w.writerow([
                i, KIND_LABEL.get(r.kind, r.kind), STATUS_LABEL.get(r.status, r.status),
                r.confidence, CATEGORIES.get(r.category, r.category),
                r.orig_path, r.out_path or "", r.size, r.sha256, r.fmt, r.note,
            ])


def write_markdown(path: str, results, stats, volume, source: str, out_dir: str,
                   options: dict) -> None:
    lines: list[str] = []
    add = lines.append
    add("# exFAT 数据恢复报告")
    add("")
    add(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"- 数据源：`{source}`")
    add(f"- 输出目录：`{out_dir}`")
    add(f"- 耗时：{stats.elapsed:.1f} 秒")
    if options:
        add(f"- 参数：{', '.join(f'{k}={v}' for k, v in options.items())}")
    add("")

    add("## 一、卷信息")
    add("")
    add("| 项目 | 值 |")
    add("| --- | --- |")
    for k, v in volume.items():
        add(f"| {k} | {v} |")
    add("")

    add("## 二、恢复结果总览")
    add("")
    by_kind = Counter(r.kind for r in results)
    by_status = Counter(r.status for r in results)
    add("| 来源 | 数量 |")
    add("| --- | --- |")
    for k, label in KIND_LABEL.items():
        add(f"| {label} | {by_kind.get(k, 0)} |")
    add(f"| **合计** | **{len(results)}** |")
    add("")
    add("| 状态 | 数量 |")
    add("| --- | --- |")
    for k, label in STATUS_LABEL.items():
        add(f"| {label} | {by_status.get(k, 0)} |")
    add("")

    add("### 按类别统计")
    add("")
    add("| 类别 | 完整 | 可用 | 可疑 | 合计字节 |")
    add("| --- | --- | --- | --- | --- |")
    cat_stat: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "warn": 0, "suspect": 0, "bytes": 0})
    for r in results:
        if r.status in ("ok", "warn", "suspect"):
            cat_stat[r.category][r.status] += 1
            cat_stat[r.category]["bytes"] += r.size
    for cat, st in sorted(cat_stat.items(), key=lambda x: -x[1]["bytes"]):
        add(f"| {CATEGORIES.get(cat, cat)} | {st['ok']} | {st['warn']} | {st['suspect']} | {human(st['bytes'])} |")
    add("")

    good = [r for r in results if r.status in ("ok", "warn")]
    add(f"## 三、恢复清单（显示前 {min(len(good), 200)} 条，共 {len(good)} 条，完整清单见 report.csv）")
    add("")
    add("| # | 来源 | 状态 | 文件 | 大小 | 备注 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(good[:200], 1):
        name = os.path.basename(r.out_path or r.orig_path)
        note = r.note.replace("|", "/")[:80]
        add(f"| {i} | {KIND_LABEL.get(r.kind, r.kind)} | {STATUS_LABEL.get(r.status, r.status)} "
            f"| `{name}` | {human(r.size)} | {note} |")
    add("")

    suspect = [r for r in results if r.status == "suspect"]
    if suspect:
        add(f"## 四、可疑文件（{len(suspect)} 条）")
        add("")
        add("这些文件长度或结构对不上，可能只恢复出前一段数据。建议按下面的顺序再试：")
        add("")
        add("1. 先做整盘镜像（`dd`），再对镜像跑 `--deep-verify --carve-all`；")
        add("2. 用其它工具交叉验证（如 PhotoRec）比对结果；")
        add("3. 若原始文件来自相机/手机，可尝试厂商工具按文件系统结构重建。")
        add("")
        add("| 原始路径 | 大小 | 当前判定 | 备注 |")
        add("| --- | --- | --- | --- |")
        for r in suspect[:100]:
            add(f"| `{r.orig_path}` | {human(r.size)} | {r.confidence} | {r.note.replace('|', '/')[:100]} |")
        add("")

    add("## 五、说明")
    add("")
    add("- **完整**：文件头魔数与尾部结束标记都能对上，结构闭合，通常可直接打开。")
    add("- **可用（可能不完整）**：内容类型正确，但尾部标记缺失或类型与扩展名不符，建议逐个打开确认。")
    add("- **可疑**：认不出内容或结构不成立，多半是被新数据覆盖的区域。")
    add("- 已删除文件的目录项来自 exFAT 的 in-use 位清零痕迹，恢复出来的是删除前的名字。")
    add("- 若同一个文件既有 `files/` 版本又有 `carved/` 版本，优先用 `files/`（保留了原始目录和文件名）。")
    add("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def summary_text(results, stats, volume) -> str:
    by_status = Counter(r.status for r in results)
    by_kind = Counter(r.kind for r in results)
    lines = [
        "",
        "─" * 62,
        "恢复结果",
        "─" * 62,
        f"  活动文件 {by_kind.get('live', 0)} 个 ｜ 已删除 {by_kind.get('deleted', 0)} 个 "
        f"｜ 雕刻 {by_kind.get('carved', 0)} 个",
        f"  完整 {by_status.get('ok', 0)} ｜ 可用 {by_status.get('warn', 0)} "
        f"｜ 可疑 {by_status.get('suspect', 0)} ｜ 重复 {by_status.get('duplicate', 0)} "
        f"｜ 跳过 {by_status.get('skipped', 0)}",
        f"  恢复到 {human(stats.bytes)}，耗时 {stats.elapsed:.1f} 秒",
        "",
    ]
    return "\n".join(lines)
