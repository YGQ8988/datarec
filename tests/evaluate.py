#!/usr/bin/env python3
"""验收脚本：对事故镜像跑恢复，再和原始 sha256 清单比对，算召回率。

用法：python3 tests/evaluate.py [scenario ...]
默认跑 delete / crash_fat / crash_meta。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IMG = os.path.join(HERE, "img")
WORK = "/tmp/datarec_test"

SCENARIOS = {
    "base": [],
    "delete": [],
    "crash_fat": [],
    "crash_meta": [],
}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(name: str, extra: list[str] | None = None) -> tuple[str, float]:
    img = os.path.join(IMG, f"{name}.img")
    out = os.path.join(WORK, name)
    subprocess.run(["rm", "-rf", out], check=False)
    os.makedirs(WORK, exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, "datarec.py"), "recover",
           "-s", img, "-o", out, "-q"] + (extra or [])
    t0 = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0
    if res.returncode != 0:
        print(res.stdout[-3000:])
        print(res.stderr[-3000:])
        raise SystemExit(f"{name} 运行失败")
    return out, dt


def evaluate(name: str, out: str, dt: float) -> dict:
    with open(os.path.join(IMG, "manifest.json")) as fh:
        manifest = json.load(fh)
    want = {k: v for k, v in manifest.items() if not os.path.basename(k).startswith("._")}

    got: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(out):
        for f in files:
            p = os.path.join(root, f)
            if f in ("report.csv", "report.md"):
                continue
            try:
                h = sha256(p)
            except OSError:
                continue
            got.setdefault(h, []).append(os.path.relpath(p, out))

    hit, miss, wrong = [], [], []
    for rel, meta in sorted(want.items()):
        if meta["sha256"] in got:
            pred = got[meta["sha256"]][0]
            hit.append((rel, pred))
            # 名字也得对得上，才说明不是"蒙对内容"
            if os.path.basename(rel) not in pred:
                wrong.append((rel, pred))
        else:
            miss.append(rel)

    rows = list(csv.DictReader(open(os.path.join(out, "report.csv"), encoding="utf-8-sig")))
    ok_rows = sum(1 for r in rows if r["状态"] in ("完整", "可用（可能不完整）"))
    suspect = sum(1 for r in rows if r["状态"] == "可疑")
    total_bytes = sum(want[r[0]]["size"] for r in hit)

    print(f"\n=== {name} ===")
    print(f"  用时 {dt:.1f}s，报告 {len(rows)} 行（可用 {ok_rows}，可疑 {suspect}）")
    print(f"  字节级召回 {len(hit)}/{len(want)}  "
          f"({100 * len(hit) / max(1, len(want)):.0f}%)  共 {total_bytes / 1048576:.1f} MB")
    for rel in miss:
        status = next((r["状态"] + " " + r["备注"][:40] for r in rows
                       if r["原始路径"].endswith(rel)), "报告里没有")
        print(f"    ✗ {rel:<34} {status}")
    for rel, pred in wrong:
        print(f"    ? {rel} 内容命中但文件名不符 → {pred}")
    return {"name": name, "hit": len(hit), "total": len(want), "bytes": total_bytes}


def main() -> None:
    names = sys.argv[1:] or ["delete", "crash_fat", "crash_meta"]
    extra: list[str] = []
    if "--carve" in os.environ.get("DR_ARGS", ""):
        extra = os.environ["DR_ARGS"].split()
    results = []
    for name in names:
        out, dt = run(name, extra)
        results.append(evaluate(name, out, dt))
    print("\n汇总")
    for r in results:
        print(f"  {r['name']:<12}{r['hit']}/{r['total']}  "
              f"{100 * r['hit'] / max(1, r['total']):.0f}%  {r['bytes'] / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
