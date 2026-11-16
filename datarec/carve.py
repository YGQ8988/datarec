"""签名雕刻（carving）：不依赖任何文件系统元数据，直接按内容找文件。

用在两种场合：
1) 目录项已经没了/被覆盖了，但数据还在盘上；
2) 对"按目录项恢复出来的文件"做交叉验证（按目录项恢复失败，
   但同一位置能雕出结构完整的文件，说明数据没坏，只是链断了）。

扫描是流式的：一次读 4MB，命中后先推断文件长度，然后直接跳过整个文件，
避免在文件内部再命中一次魔数（比如 mdat 里嵌的 ftyp）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .device import Source, SourceError
from .formats import FMT_EXT
from .signatures import MAX_PRE, SIGS, Sig, infer_extent, sig_confirms, zip_flavor

MiB = 1024 * 1024
CHUNK = 4 * MiB
BY_MAGIC: dict[bytes, list[Sig]] = {}
for _s in SIGS:
    BY_MAGIC.setdefault(_s.magic, []).append(_s)
# 去重后的魔数列表（同一魔数可能有多个变体，比如 ftyp 后面跟不同品牌）
MAGICS: list[bytes] = list(BY_MAGIC)


@dataclass
class Carved:
    offset: int
    size: int
    fmt: str
    ext: str
    category: str
    conf: str
    note: str

    @property
    def key(self) -> tuple[int, int, str]:
        return self.offset, self.size, self.fmt


def scan(src: Source, start: int, end: int,
         min_size: int = 4096,
         max_size: int = 16 * 1024 * MiB,
         categories: set[str] | None = None,
         skip_ranges: list[tuple[int, int]] | None = None,
         progress: Callable[[int, int], None] | None = None,
         should_stop: Callable[[], bool] | None = None) -> list[Carved]:
    """从 start 扫到 end，返回所有雕刻出的文件。

    性能要点：不用"33 个魔数拼成一条正则"的做法（Python re 在二进制数据上会退化成
    逐位置回溯，实测只有 1.5MB/s）。改成每个魔数各做一次 bytes.find（C 层 memchr 加速），
    再把命中位置合并排序，吞吐能到 80MB/s 以上。
    """
    results: list[Carved] = []
    seen: set[tuple[int, int, str]] = set()
    skips = _merge(skip_ranges or [])
    pos = max(0, start)
    while pos < end:
        if should_stop and should_stop():
            break
        if progress:
            progress(pos - start, end - start)
        try:
            chunk = src.read(pos, min(CHUNK, end - pos))
        except SourceError:
            pos += CHUNK
            continue
        if not chunk:
            break

        hits: list[tuple[int, bytes]] = []
        for magic in MAGICS:
            idx = chunk.find(magic)
            while idx >= 0:
                hits.append((idx, magic))
                idx = chunk.find(magic, idx + 1)
        hits.sort()

        cursor = pos                      # 当前扫描位置（绝对偏移）
        advanced = False
        for idx, magic in hits:
            abs_pos = pos + idx
            cand_sig = None
            cand_start = None
            for sig in BY_MAGIC[magic]:
                cand = abs_pos - sig.pre
                # 允许往回退 MAX_PRE：跨块边界的文件（比如 ISO 的 CD001 在 +32KB）
                if cand < start or cand < pos - MAX_PRE or cand < cursor:
                    continue
                if categories and sig.category not in categories:
                    continue
                if not _sig_ok(sig, src, cand):
                    continue
                cand_sig, cand_start = sig, cand
                break
            if cand_sig is None:
                continue
            if _covered(cand_start, skips):
                cursor = max(cursor, abs_pos + 1)
                continue
            size, conf, note, fmt = infer_extent(
                src, cand_start, min(end, cand_start + max_size), cand_sig.fmt)
            if not size or size < min_size:
                cursor = max(cursor, abs_pos + 1)
                continue
            ext, category = _identify(src, fmt, cand_sig, cand_start, size)
            if categories and category not in categories:
                cursor = max(cursor, abs_pos + 1)
                continue
            item = Carved(offset=cand_start, size=size, fmt=fmt, ext=ext,
                          category=category, conf=conf, note=note)
            if item.key not in seen:
                seen.add(item.key)
                results.append(item)
            cursor = cand_start + size      # 跳过整个文件，避免在文件内部重复命中
            advanced = True
            if cursor >= end:
                break

        if advanced and cursor > pos:
            pos = cursor
            continue
        step = len(chunk) - MAX_PRE
        if step <= 0:
            break            # 尾部剩下的字节已被上一块的重叠区覆盖，扫完了
        pos += step
    if progress:
        progress(end - start, end - start)
    return results


def _sig_ok(sig: Sig, src: Source, cand: int) -> bool:
    if not sig.strong or sig.post:
        return sig_confirms(sig, src, cand)
    return True


def _identify(src: Source, fmt: str, sig: Sig, cand: int, size: int) -> tuple[str, str]:
    """把魔数格式细化成具体扩展名（docx / xlsx / pptx / mov …）并给出归类。"""
    if fmt == "zip":
        real_fmt, ext = zip_flavor(src, cand, size)
        return ext, "archives" if real_fmt == "zip" else "documents"
    ext = sig.ext or FMT_EXT.get(fmt, "bin")
    return ext, sig.category


def _merge(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(ranges):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _covered(pos: int, merged: list[tuple[int, int]]) -> bool:
    lo, hi = 0, len(merged) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b = merged[mid]
        if pos < a:
            hi = mid - 1
        elif pos >= b:
            lo = mid + 1
        else:
            return True
    return False


def carve_dmg(src: Source, start: int, end: int) -> list[Carved]:
    """DMG 走 koly trailer 反推，不参与魔数扫描。"""
    from .signatures import find_dmg_extents
    return [Carved(offset=off, size=size, fmt="dmg", ext="dmg",
                   category="diskimages", conf="ok", note=note)
            for off, size, note in find_dmg_extents(src, start, end)]
