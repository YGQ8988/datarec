"""断点续扫：把"已经恢复过哪些文件"记在输出目录里。

为什么需要它：扫一块几 TB 的盘要几十分钟，中途停下（没电、要出门、想换个筛选条件）
是很正常的事。没有状态记录的话，下次只能从头再来，已经写好的文件还会被改名成
`IMG_0001_1.jpg` 再生产一份。

设计要点：
  · 指纹绑定：记录数据源的容量 + 卷序列号 + 引导扇区偏移。换了盘、换了卷，
    旧状态直接作废，绝不会拿 A 盘的进度去跳过 B 盘的文件。
  · 存活性校验：状态里记的文件必须在输出目录里真实存在、且大小对得上才算数。
    用户手工删掉几个文件再续扫，它们会被重新恢复。
  · 原子写：先写 .tmp 再 os.replace，进程被强杀也不会留下半个 JSON 把进度读坏。
  · 节流落盘：每恢复一个文件都写一次 JSON 的话，几万个文件就是几万次 fsync。
    默认 1.5 秒合并一次，中断和结束时强制落盘。
  · 跑完整了就删掉状态文件，下一轮是干净的开始；中断则保留。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

STATE_NAME = ".datarec_state.json"
SCHEMA = 1
FLUSH_INTERVAL = 1.5          # 秒；节流写盘的间隔


@dataclass
class Fingerprint:
    """数据源指纹：够唯一，又不依赖路径字符串（盘符/设备名会变）。"""
    path: str = ""
    size: int = 0             # 卷大小（字节）
    serial: int = 0           # exFAT 卷序列号
    offset: int = 0           # 引导扇区在数据源里的偏移

    def as_dict(self) -> dict:
        return {"path": self.path, "size": self.size,
                "serial": self.serial, "offset": self.offset}

    @classmethod
    def from_dict(cls, d: dict) -> "Fingerprint":
        return cls(path=str(d.get("path", "")), size=int(d.get("size", 0)),
                   serial=int(d.get("serial", 0)), offset=int(d.get("offset", 0)))

    def same_volume(self, other: "Fingerprint") -> bool:
        """同一个卷？不看路径，只看容量 + 序列号 + 偏移。

        序列号为 0 的卷（某些工具格出来不带序列号）退回比容量和偏移。
        """
        if self.size != other.size or self.offset != other.offset:
            return False
        if self.serial and other.serial:
            return self.serial == other.serial
        return True

    def describe(self) -> str:
        return (f"卷大小 {_human(self.size)}，序列号 {hex(self.serial) if self.serial else '无'}"
                f"，引导扇区偏移 {self.offset}")


def _human(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def file_key(orig_path: str) -> str:
    return "f:" + orig_path


def carve_key(offset: int) -> str:
    return "c:%d" % offset


@dataclass
class ResumeState:
    """输出目录里的进度记录。用 open() 构造，不要直接 new。"""
    path: str
    fingerprint: Fingerprint = field(default_factory=Fingerprint)
    done: dict[str, dict] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    reset_note: str = ""              # 载入时发现指纹不符/文件损坏时的说明
    _last_flush: float = 0.0
    _dirty: bool = False
    _closed: bool = False             # 已 clear()，之后的 flush 一律忽略

    # ---------------------------------------------------------------- 构造
    @classmethod
    def open(cls, out_dir: str, fingerprint: Fingerprint,
             *, create: bool = True) -> "ResumeState":
        """读输出目录里的状态；没有就建一个空的（create=False 时返回空对象不写盘）。"""
        path = os.path.join(out_dir, STATE_NAME)
        st = cls(path=path, fingerprint=fingerprint)
        st._load()
        if create:
            st.created = st.created or time.time()
        return st

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            self.reset_note = f"上次的进度文件读不出来（{exc}），当作全新一轮"
            self._bad_backup()
            return
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            self.reset_note = "上次的进度文件是旧版本格式，忽略"
            return
        fp = Fingerprint.from_dict(raw.get("source") or {})
        if not fp.same_volume(self.fingerprint):
            self.reset_note = (f"上次的进度属于另一个卷（{fp.describe()}），"
                               f"和当前盘对不上，当作全新一轮")
            return
        done = raw.get("done")
        if isinstance(done, dict):
            self.done = {k: v for k, v in done.items() if isinstance(v, dict)}
        self.created = float(raw.get("created") or time.time())
        self.updated = float(raw.get("updated") or time.time())

    def _bad_backup(self) -> None:
        try:
            os.replace(self.path, self.path + ".bad")
        except OSError:
            pass

    # ---------------------------------------------------------------- 查/记
    @property
    def count(self) -> int:
        return len(self.done)

    def has(self, key: str, size: int | None = None,
            out_dir: str | None = None) -> bool:
        """这条记录还算数吗？—— 记录在、文件真的在、大小对得上。"""
        rec = self.done.get(key)
        if not rec:
            return False
        if size and rec.get("size") and int(rec["size"]) != int(size):
            return False
        rel = rec.get("out")
        if rel and out_dir:
            full = os.path.join(out_dir, rel)
            if not os.path.exists(full):
                return False                    # 用户把文件删了/移走了 → 重做
            want = rec.get("bytes")
            if want:
                try:
                    if os.path.getsize(full) < int(want):
                        return False            # 被截断过，不可信
                except OSError:
                    return False
        return True

    def mark(self, key: str, *, size: int = 0, out: str = "", status: str = "ok",
             sha256: str = "", bytes: int = 0) -> None:
        self.done[key] = {"size": size, "out": out, "status": status,
                          "sha256": sha256, "bytes": bytes, "at": time.time()}
        self._dirty = True
        self.flush()

    def forget(self, key: str) -> None:
        if self.done.pop(key, None) is not None:
            self._dirty = True
            self.flush()

    def reset(self) -> None:
        """全部重来：清空记录并落盘（文件本身不动）。"""
        self._closed = False
        self.done.clear()
        self._dirty = True
        self.flush(force=True)

    # ---------------------------------------------------------------- 落盘
    def flush(self, force: bool = False) -> None:
        if self._closed:
            return                        # 已经判定整轮完成并删掉了，别再写回来
        if not self._dirty and not force:
            return
        now = time.time()
        if not force and now - self._last_flush < FLUSH_INTERVAL:
            return
        payload = {
            "schema": SCHEMA,
            "created": self.created,
            "updated": now,
            "source": self.fingerprint.as_dict(),
            "done": self.done,
        }
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            # 写不进去（只读目录、磁盘满）就当没有断点，恢复本身不能因此失败
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        self._last_flush = now
        self._dirty = False

    def clear(self) -> None:
        """整轮跑完了：删掉状态文件，下次是干净的。"""
        self._closed = True
        self._dirty = False
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.unlink(self.path + ".tmp")
        except OSError:
            pass
