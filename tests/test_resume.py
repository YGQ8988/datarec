"""断点续扫的验收脚本：中途停 → 续扫 → 和"一次跑完"逐字节比对。

用法（在仓库根目录）：
    python3 tests/test_resume.py [镜像路径]

默认用 tests/img/base.img（先跑 bash tests/build_env.sh 造出来）。
判定三条：续扫不产生重复文件、最终结果与一次跑完完全一致、跑完后断点文件被清掉。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datarec.device import Source                      # noqa: E402
from datarec.exfat import ExFatVolume                  # noqa: E402
from datarec.recover import Cancelled, Recoverer       # noqa: E402
from datarec.state import STATE_NAME, Fingerprint, ResumeState   # noqa: E402

IMG = sys.argv[1] if len(sys.argv) > 1 else "tests/img/base.img"
FULL = "/tmp/datarec_resume_full"
PART = "/tmp/datarec_resume_part"
STOP_AFTER = 1.6          # 秒；够跑完目录项阶段、停在雕刻阶段


def open_volume():
    src = Source(IMG)
    vol = ExFatVolume.open(src, offset=None)
    fp = Fingerprint(path=src.path, size=vol.info.size_bytes,
                     serial=vol.info.serial, offset=vol.info.offset)
    return src, vol, fp


def run(out_dir: str, stop_after: float | None = None):
    """跑一轮恢复。stop_after 到点后模拟用户点「停止」。"""
    src, vol, fp = open_volume()
    state = ResumeState.open(out_dir, fp)
    evt = threading.Event()
    timer = threading.Timer(stop_after, evt.set) if stop_after else None
    if timer:
        timer.start()
    rec = Recoverer(src, vol, out_dir, verbose=False,
                    cancel=evt.is_set, state=state, progress=lambda _m: None)
    finished = False
    try:
        rec.run()
        finished = True
    except Cancelled:
        pass
    if timer:
        timer.cancel()
    return rec, state, finished


def files_of(root: str) -> dict[str, str]:
    """{相对路径: sha256}，跳过 .datarec_state.json 这类隐藏文件。"""
    out: dict[str, str] = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def main() -> int:
    if not os.path.exists(IMG):
        print(f"找不到 {IMG}，先跑 bash tests/build_env.sh")
        return 2

    print(f"镜像：{IMG}")
    for d in (FULL, PART):
        shutil.rmtree(d, ignore_errors=True)

    print("[1/3] 基准：一次跑完")
    _, _, ok = run(FULL)
    baseline = files_of(FULL)
    print(f"      完成={ok}，文件 {len(baseline)} 个")

    print(f"[2/3] 跑 {STOP_AFTER} 秒后停止")
    _, st, ok = run(PART, stop_after=STOP_AFTER)
    partial = files_of(PART)
    state_path = os.path.join(PART, STATE_NAME)
    print(f"      完成={ok}（应为 False），文件 {len(partial)} 个，"
          f"断点 {st.count} 条，断点文件在={os.path.exists(state_path)}")

    print("[3/3] 对同一目录续扫")
    rec, _st, ok = run(PART)
    final = files_of(PART)
    print(f"      完成={ok}，跳过 {rec.stats.resumed} 个，新恢复 {rec.stats.ok} 个")
    print(f"      跑完后断点文件已清除={not os.path.exists(state_path)}")

    dups = [p for p in final if "_1." in os.path.basename(p)]
    checks = [
        ("续扫后与基准文件名一致", set(final) == set(baseline)),
        ("续扫后与基准哈希一致", set(final.values()) == set(baseline.values())),
        ("没有 _1 之类的重复文件", not dups),
        ("完整跑完后断点文件被清掉", not os.path.exists(state_path)),
        ("中断时确实留下了断点文件", len(partial) < len(baseline)),
    ]
    print()
    for name, good in checks:
        print(("  ✓ " if good else "  ✗ ") + name)
    if dups:
        print("    重复文件示例：", dups[:5])
    return 0 if all(g for _, g in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
