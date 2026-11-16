"""恢复主流程：按目录项恢复 + 断链兜底 + 全盘雕刻 + 落盘校验。

顺序上是分层的，越靠前越可信：
  1. 目录项 + NoFatChain/连续簇       —— 大多数 exFAT 文件的真实布局
  2. 目录项 + FAT 链                  —— 传统链式分配
  3. 目录项失效时，从 FirstCluster 直接结构性雕刻 —— 修"链断了但数据还在"
  4. 全盘签名雕刻                     —— 兜底捞目录项彻底丢失的文件
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import carve as carve_mod
from .device import Source, SourceError
from .errors import Cancelled
from .exfat import ExFatVolume, FileEntry
from .formats import allowed_formats, category_of, normalize_ext
from .state import ResumeState, carve_key, file_key
from .verify import check_written

MiB = 1024 * 1024
COPY_CHUNK = 4 * MiB
SUSPECT_TAG = "__可疑"
SYSTEM_DIRS = {".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems",
               "System Volume Information", "$RECYCLE.BIN", ".DocumentRevisions-V100"}


@dataclass
class Result:
    kind: str               # live / deleted / carved
    orig_path: str
    out_path: str | None
    size: int
    sha256: str
    fmt: str
    status: str             # ok / warn / suspect / skipped / duplicate
    confidence: int
    note: str
    category: str = "other"


@dataclass
class Stats:
    scanned_files: int = 0
    live: int = 0
    deleted: int = 0
    carved: int = 0
    ok: int = 0
    warn: int = 0
    suspect: int = 0
    skipped: int = 0
    duplicate: int = 0
    resumed: int = 0          # 上次已经恢复过、本次直接跳过
    bytes: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time


__all__ = ["Cancelled", "Result", "Stats", "Recoverer"]


class Recoverer:
    def __init__(self, src: Source, vol: ExFatVolume, out_dir: str, *,
                 types: set[str] | None = None,
                 min_size: int = 0,
                 deep_verify: bool = False,
                 keep_appledouble: bool = False,
                 keep_system: bool = False,
                 carve: bool = True,
                 carve_all: bool = False,
                 dry_run: bool = False,
                 offset_start: int = 0,
                 offset_end: int | None = None,
                 verbose: bool = True,
                 progress: Callable[[str], None] | None = None,
                 on_progress: Callable[[float, str], None] | None = None,
                 cancel: Callable[[], bool] | None = None,
                 only_paths: set[str] | None = None,
                 state: ResumeState | None = None):
        self.src = src
        self.vol = vol
        self.out_dir = os.path.abspath(out_dir)
        self.types = types
        self.min_size = min_size
        self.deep_verify = deep_verify
        self.keep_appledouble = keep_appledouble
        self.keep_system = keep_system
        self.do_carve = carve
        self.carve_all = carve_all
        self.dry_run = dry_run
        self.start = offset_start
        self.end = offset_end if offset_end is not None else self._volume_end()
        self.verbose = verbose
        self.say = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.on_progress = on_progress
        self.cancel = cancel
        self.only_paths = only_paths
        self.state = state

        self.results: list[Result] = []
        self.stats = Stats()
        self._hashes: dict[str, str] = {}
        self._claimed: list[tuple[int, int]] = []
        self._pending_note = ""          # 兜底雕刻失败时留下的说明，供上层记录

    # ------------------------------------------------------------------ 基础
    def _stopped(self) -> bool:
        try:
            return bool(self.cancel and self.cancel())
        except Exception:
            return False

    def _progress(self, frac: float, label: str) -> None:
        if not self.on_progress:
            return
        try:
            self.on_progress(max(0.0, min(1.0, frac)), label)
        except Exception:
            pass                            # 回调出错不许影响恢复本身


    def _volume_end(self) -> int:
        end = self.vol.info.size_bytes
        if self.src.device_size:
            end = min(end, self.src.device_size) if end else self.src.device_size
        return end

    def _cs(self) -> int:
        return self.vol.info.cluster_size

    def _clusters_for(self, size: int) -> int:
        return max(1, math.ceil(size / self._cs()))

    def _out(self, *parts: str) -> str:
        path = os.path.join(self.out_dir, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    @staticmethod
    def _sanitize(name: str) -> str:
        name = re.sub(r"[\x00-\x1f:/\\\r\n\t]", "_", name).strip().strip(".")
        if not name:
            name = "未命名"
        if len(name) > 120:
            stem, dot, ext = name.rpartition(".")
            name = (stem[:110] + "." + ext[:8]) if dot and len(ext) <= 8 else name[:120]
        return name

    def _rel_path(self, entry: FileEntry) -> str:
        parts = [self._sanitize(p) for p in entry.path.strip("/").split("/") if p]
        return os.path.join(*parts) if parts else self._sanitize(entry.name)

    def _category(self, entry: FileEntry) -> str:
        return category_of(entry.name)

    def _type_allowed(self, name: str) -> bool:
        return not self.types or category_of(name) in self.types

    def _is_system(self, entry: FileEntry) -> bool:
        if self.keep_system:
            return False
        parts = entry.path.strip("/").split("/")
        if any(p in SYSTEM_DIRS for p in parts[:-1] if p):
            return True
        return entry.name.startswith("._") and not self.keep_appledouble

    # ------------------------------------------------------------------ 主流程
    def run(self) -> list[Result]:
        try:
            return self._run()
        finally:
            if self.state:                 # 中断也要把进度落盘，这是续扫的前提
                self.state.flush(force=True)

    def _run(self) -> list[Result]:
        self._progress(0.0, "读取目录结构…")
        files, dirs, warnings = self.vol.walk(should_stop=self._stopped)
        for w in warnings:
            self.say(f"  · 提示：{w}")
        if self._stopped():
            raise Cancelled()

        entries = [f for f in files if not f.is_dir]
        self.stats.scanned_files = len(entries)

        live = [f for f in entries if not f.deleted]
        gone = [f for f in entries if f.deleted]

        self.say(f"[1/3] 目录项：活动文件 {len(live)} 个，已删除 {len(gone)} 个")
        picked: list[FileEntry] = []
        for ent in sorted(live, key=lambda e: e.path):
            if self._is_system(ent) or not self._type_allowed(ent.name):
                self.stats.skipped += 1
                continue
            if ent.size and ent.size < self.min_size:
                self.stats.skipped += 1
                continue
            picked.append(ent)

        for ent in sorted(gone, key=lambda e: e.path):
            if self._is_system(ent) or not self._type_allowed(ent.name):
                self.stats.skipped += 1
                continue
            if ent.size and ent.size < self.min_size:
                self.stats.skipped += 1
                continue
            picked.append(ent)

        if self.only_paths is not None:      # GUI 里手工勾选的那几个
            picked = [e for e in picked if e.path in self.only_paths]

        # 断点续扫：上次已经完整恢复过、文件还在输出目录里的，直接跳过
        if self.state and self.state.count:
            keep: list[FileEntry] = []
            for ent in picked:
                if self.state.has(file_key(ent.path), ent.size, self.out_dir):
                    self.stats.resumed += 1
                else:
                    keep.append(ent)
            if self.stats.resumed:
                self.say(f"  · 续扫：{self.stats.resumed} 个文件上次已恢复，本次跳过")
            picked = keep

        total = len(picked)
        for idx, ent in enumerate(picked, 1):
            if self._stopped():
                raise Cancelled()
            self._progress(0.7 * (idx - 1) / total if total else 0.0,
                           f"正在恢复 {idx}/{total}：{ent.name}")
            if self.verbose:
                self.say(f"  ({idx}/{total}) {'已删除 ' if ent.deleted else ''}{ent.path}")
            self._recover_entry(ent)
        self._progress(0.7, f"文件条目处理完毕，成功 {self.stats.ok} 个")

        # 记录活动文件占用的范围，供雕刻阶段跳过
        for ent in live:
            self._claim_entry(ent)
        for d in dirs:
            self._claim_entry(d)
        self._claim_metadata()

        if self.do_carve:
            self._carve_phase()
        self._progress(1.0, "完成")

        # 能走到这里说明整轮跑完了（没被 Cancel）；断点文件留着没意义，
        # 删掉，下一轮就是干净的开始。中断时走不到这行，进度自然保留。
        if self.state:
            self.state.clear()
        return self.results

    def _claim_entry(self, ent: FileEntry) -> None:
        if not self.vol.valid_cluster(ent.first_cluster):
            return
        size = ent.size or self._cs()
        n = self._clusters_for(min(size, self.vol.info.cluster_count * self._cs()))
        start = self.vol.info.offset + self.vol.cluster_offset(ent.first_cluster)
        self._claimed.append((start, min(self.end, start + n * self._cs())))

    def _claim_metadata(self) -> None:
        info = self.vol.info
        self._claimed.append((0, info.offset + info.heap_offset))
        for c in range(2, min(info.root_cluster, 6)):     # 位图、upcase 等系统文件
            start = info.offset + self.vol.cluster_offset(c)
            self._claimed.append((start, start + self._cs()))

    # ------------------------------------------------------------------ 单个文件
    def _recover_entry(self, ent: FileEntry) -> None:
        if not self.vol.valid_cluster(ent.first_cluster):
            self._record(ent, None, 0, "", "skipped", 0, "FirstCluster 非法，只能靠雕刻")
            return
        size = ent.size
        if size == 0:
            if not self._recover_by_carve_hint(ent, "目录项声明长度为 0，改从 FirstCluster 按内容雕刻"):
                self._record(ent, None, 0, "", "skipped", 0,
                             "声明长度为 0，且内容也推断不出长度（可能已被覆盖）")
            return

        attempts = self._strategies(ent, size)
        best: tuple[int, str, str, str, str, int] | None = None
        for label, payload, written_note in attempts:
            dest = self._temp_path(ent)
            try:
                digest, written = self._write_payload(dest, payload, size)
            except (OSError, SourceError) as exc:
                self._safe_unlink(dest)
                self.say(f"      ! {label} 写入失败：{exc}")
                continue
            score, verdict, note = check_written(dest, written, ent.name, self.deep_verify)
            notes = "；".join(x for x in (written_note, note) if x)
            if written < size:
                score = min(score, 40)
                verdict = "suspect"
                notes = "；".join(x for x in (notes, f"只读到 {written}/{size} 字节") if x)
            if ent.deleted:
                ratio = self._overwrite_ratio(ent)
                if ratio is not None and ratio > 0.5:
                    msg = f"位图显示数据区 {ratio:.0%} 的簇已被占用（可能被覆盖）"
                    notes = "；".join(x for x in (notes, msg) if x)
                    # 内容结构站得住就以内容为准，位图只作风险提示
                    if verdict != "ok":
                        score = min(score, 40)
                        verdict = "suspect"
            if best is None or score > best[0]:
                if best is not None:
                    self._safe_unlink(best[3])
                best = (score, verdict, notes, dest, digest, written)
            else:
                self._safe_unlink(dest)
            if verdict == "ok":
                break

        if best is None:
            if not self._recover_by_carve_hint(ent, "按簇读取失败，改从 FirstCluster 按内容雕刻"):
                self._record(ent, None, 0, "", "skipped", 0, "所有读取方式都失败")
            return

        score, verdict, notes, dest, digest, written = best
        if score < 45:
            self._safe_unlink(dest)
            if self._recover_by_carve_hint(ent, "按簇取到的数据不可信，改按内容结构雕刻"):
                return
            self._record(ent, None, 0, "", "suspect", score, notes or "结构校验未通过")
            return

        final = self._finalize(ent, dest, verdict, notes, digest, written, score)

    def _strategies(self, ent: FileEntry, size: int) -> list[tuple[str, object, str]]:
        """给出候选读取方案：优先最可信的，其次兜底。

        误拔场景下 FAT 往往没刷盘，所以"FAT 链不完整"时不能直接用半截链，
        要先按连续簇取（exFAT 大文件基本都是连续分配的），再用内容校验裁决。
        """
        need = self._clusters_for(size)
        contig = ("连续簇", ("contig", ent.first_cluster), "")
        out: list[tuple[str, object, str]] = []
        if ent.no_fat_chain:
            return [contig]
        chain, problem = self.vol.follow_chain(ent.first_cluster, need)
        if len(chain) >= need and not problem:
            return [("FAT链", ("chain", chain), "")]
        # 链不可靠：连续簇优先，链式读取作为对照
        out.append(("连续簇", ("contig", ent.first_cluster),
                    f"FAT 链不完整（{problem or '簇数不足'}），按连续簇读取"))
        if chain:
            out.append(("FAT链", ("chain", chain), "FAT 链只覆盖部分数据"))
        return out

    def _write_payload(self, dest: str, payload: tuple[str, object], size: int) -> tuple[str, int]:
        """把数据写进临时文件。payload 三种形态：
           ("contig", first_cluster)  从某个簇开始连续读
           ("chain", [cluster...])    按 FAT 链逐簇读
           ("contig_at", offset)      从卷内绝对偏移开始连续读（雕刻兜底用）
        """
        kind, arg = payload
        digest = hashlib.sha256()
        written = 0
        with open(dest, "wb") as fh:
            if kind == "chain":
                remaining = size
                for cluster in arg:                       # type: ignore[union-attr]
                    if remaining <= 0:
                        break
                    data = self.vol.read_cluster(cluster)
                    if not data:
                        break
                    data = data[:remaining]
                    fh.write(data)
                    digest.update(data)
                    written += len(data)
                    remaining -= len(data)
            else:
                remaining = size
                if kind == "contig_at":
                    pos = int(arg)                        # type: ignore[arg-type]
                else:
                    pos = self.vol.cluster_offset(int(arg))   # type: ignore[arg-type]
                while remaining > 0:
                    chunk = self.vol.read(pos, min(COPY_CHUNK, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
                    pos += len(chunk)
        return digest.hexdigest(), written

    def _temp_path(self, ent: FileEntry) -> str:
        os.makedirs(os.path.join(self.out_dir, ".partial"), exist_ok=True)
        base = self._sanitize(os.path.basename(ent.path))[:60]
        return os.path.join(self.out_dir, ".partial",
                            f"{int(time.time() * 1000)}_{abs(hash(ent.path)) % 99999}_{base}")

    def _finalize(self, ent: FileEntry, temp: str, verdict: str, notes: str,
                  digest: str, written: int, score: int) -> None:
        kind = "deleted" if ent.deleted else "live"
        rel = self._rel_path(ent)
        if verdict == "suspect":
            stem, dot, ext = rel.rpartition(".")
            rel = (stem + SUSPECT_TAG + "." + ext) if dot else (rel + SUSPECT_TAG)
        target = self._out("deleted" if ent.deleted else "files", rel)
        if digest in self._hashes:
            self._safe_unlink(temp)
            self._record(ent, None, written, digest, "duplicate", score,
                         f"与 {self._hashes[digest]} 内容完全相同")
            return
        try:
            if self.dry_run:
                self._safe_unlink(temp)
                final_path = target
            else:
                final_path = self._unique_path(target, digest)
                shutil.move(temp, final_path)
        except OSError as exc:
            self._safe_unlink(temp)
            self._record(ent, None, 0, digest, "skipped", score, f"保存失败：{exc}")
            return
        self._hashes[digest] = final_path
        self._record(ent, final_path, written, digest, verdict, score, notes)

    def _unique_path(self, path: str, digest: str) -> str:
        if not os.path.exists(path):
            return path
        stem, dot, ext = path.rpartition(".")
        for i in range(1, 1000):
            cand = f"{stem}_{i}.{ext}" if dot else f"{path}_{i}"
            if not os.path.exists(cand):
                return cand
        return f"{path}_{digest[:8]}"

    def _overwrite_ratio(self, ent: FileEntry) -> float | None:
        """已删除文件的数据区有多少已被现有文件占用（位图视角）。"""
        if not self.vol.valid_cluster(ent.first_cluster):
            return None
        need = min(self._clusters_for(ent.size), 512)
        allocated = 0
        checked = 0
        for i in range(need):
            c = ent.first_cluster + i
            state = self.vol.cluster_allocated(c)
            if state is None:
                return None
            checked += 1
            allocated += 1 if state else 0
        return allocated / checked if checked else None

    def _recover_by_carve_hint(self, ent: FileEntry, why: str) -> bool:
        """从 FirstCluster 开始按内容结构雕刻：数据没坏、只是链断了的救法。"""
        from .signatures import find_dmg_extents, infer_extent
        if not self.vol.valid_cluster(ent.first_cluster):
            return False
        start = self.vol.info.offset + self.vol.cluster_offset(ent.first_cluster)

        if normalize_ext(ent.name) in ("dmg", "img", "raw", "dd"):
            hit = None
            for off, sz, note in find_dmg_extents(self.src, start, min(self.end, start + (ent.size or 0) + 2 * MiB)):
                if off == start:
                    hit = (off, sz, note)
                    break
            if not hit:
                return False
            dest = self._temp_path(ent)
            try:
                digest, written = self._write_payload(dest, ("contig_at", hit[0]), hit[1])
            except (OSError, SourceError):
                self._safe_unlink(dest)
                return False
            score, verdict, vnote = check_written(dest, written, ent.name, self.deep_verify)
            self._finalize(ent, dest, verdict, "；".join(x for x in (why, hit[2], vnote) if x),
                           digest, written, score)
            return True

        hint = None
        allowed = allowed_formats(ent.name)
        if allowed:
            hint = sorted(allowed)[0]
            if hint in ("docx", "xlsx", "pptx"):
                hint = "zip"
            if hint == "epub":
                hint = "zip"
        limit = min(self.end, start + max((ent.size or 0) * 2, 32 * MiB))
        size, conf, note, fmt = infer_extent(self.src, start, limit, hint)
        if not size or conf == "fail":
            return False
        dest = self._temp_path(ent)
        try:
            digest, written = self._write_payload(dest, ("contig_at", start), size)
        except (OSError, SourceError):
            self._safe_unlink(dest)
            return False
        score, verdict, vnote = check_written(dest, written, ent.name, self.deep_verify)
        notes = "；".join(x for x in (why, note, vnote) if x)
        if score < 45:
            self._safe_unlink(dest)
            self._pending_note = notes
            return False
        self._finalize(ent, dest, verdict, notes, digest, written, score)
        return True

    # ------------------------------------------------------------------ 雕刻
    def _carve_phase(self) -> None:
        self.say(f"[3/3] 全盘签名雕刻…（跳过已恢复文件占用的区域）")
        skips = [] if self.carve_all else list(self._claimed)
        range_start = self.start + self.vol.info.heap_offset
        range_end = min(self.end, self.vol.info.offset + self.vol.info.size_bytes)
        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.time()
            frac = 0.7 + 0.3 * (done / total if total else 0)
            self._progress(frac, f"全盘扫描 {done // MiB} / {total // MiB} MB")
            if now - last[0] > 2:
                last[0] = now
                pct = 100 * done / total if total else 0
                self.say(f"      扫描 {done // MiB} MB / {total // MiB} MB ({pct:.0f}%)")

        items = carve_mod.scan(self.src, range_start, range_end,
                               min_size=max(1024, self.min_size),
                               categories=self.types,
                               skip_ranges=skips, progress=progress,
                               should_stop=self._stopped)
        if self._stopped():
            raise Cancelled()
        dmgs = carve_mod.carve_dmg(self.src, range_start, range_end)
        if self.types:
            dmgs = [d for d in dmgs if d.category in self.types]
        items.extend(dmgs)
        self.say(f"      雕刻命中 {len(items)} 个候选")

        for item in items:
            if self._stopped():
                raise Cancelled()
            self._save_carved(item)

    def _save_carved(self, item: carve_mod.Carved) -> None:
        key = carve_key(item.offset)
        if self.state and self.state.has(key, item.size, self.out_dir):
            self.stats.resumed += 1
            return
        dest = self._out(".partial", f"carved_{item.offset:x}_{item.size}")
        try:
            digest, written = self._write_carved(dest, item)
        except (OSError, SourceError) as exc:
            self._safe_unlink(dest)
            self.say(f"      ! 雕刻写盘失败 @{item.offset}：{exc}")
            return
        if digest in self._hashes:
            self._safe_unlink(dest)
            self.stats.duplicate += 1
            self.results.append(Result("carved", f"<{item.offset:#x}>", None, written, digest,
                                       item.fmt, "duplicate", 0,
                                       f"与 {os.path.basename(self._hashes[digest])} 相同",
                                       item.category))
            return
        stem = item.ext if item.fmt == "zip" else item.fmt
        name = f"{stem}_{item.offset:012x}.{item.ext}"
        target = self._out("carved", item.category, name)
        if self.dry_run:
            self._safe_unlink(dest)
            final = target
        else:
            final = self._unique_path(target, digest)
            shutil.move(dest, final)
        self._hashes[digest] = final
        self.stats.carved += 1
        score = 90 if item.conf == "ok" else (65 if item.conf == "crc_bad" else 45)
        self.results.append(Result("carved", f"偏移 {item.offset}（0x{item.offset:x}）",
                                   final, written, digest, item.fmt,
                                   "ok" if item.conf == "ok" else "warn", score, item.note,
                                   item.category))
        if self.state and not self.dry_run:
            try:
                rel = os.path.relpath(final, self.out_dir)
            except ValueError:
                rel = os.path.basename(final)
            self.state.mark(key, size=item.size, out=rel, sha256=digest, bytes=written,
                            status="ok" if item.conf == "ok" else "warn")

    def _write_carved(self, dest: str, item: carve_mod.Carved) -> tuple[str, int]:
        digest = hashlib.sha256()
        written = 0
        remaining = item.size
        pos = item.offset
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = self.src.read(pos, min(COPY_CHUNK, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                remaining -= len(chunk)
                pos += len(chunk)
        return digest.hexdigest(), written

    # ------------------------------------------------------------------ 记录
    def _record(self, ent: FileEntry, out_path: str | None, size: int, digest: str,
                status: str, score: int, note: str) -> None:
        kind = "deleted" if ent.deleted else "live"
        self.results.append(Result(kind, ent.path, out_path, size, digest,
                                   normalize_ext(ent.name) or "-", status, score, note,
                                   category_of(ent.name)))
        self._bump(status, kind, size)
        if self.state and out_path:
            # 只有真正落到盘上的才算数；写失败的（skipped）留给下次重试
            try:
                rel = os.path.relpath(out_path, self.out_dir)
            except ValueError:                 # Windows 上跨盘符
                rel = os.path.basename(out_path)
            self.state.mark(file_key(ent.path), size=ent.size or size, out=rel,
                            status=status, sha256=digest, bytes=size)

    def _bump(self, status: str, kind: str, size: int) -> None:
        if status == "ok":
            self.stats.ok += 1
        elif status == "warn":
            self.stats.warn += 1
        elif status == "suspect":
            self.stats.suspect += 1
        elif status == "duplicate":
            self.stats.duplicate += 1
        else:
            self.stats.skipped += 1
        if kind == "live":
            self.stats.live += 1
        elif kind == "deleted":
            self.stats.deleted += 1
        if status in ("ok", "warn"):
            self.stats.bytes += size

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
