"""落盘后的可信度校验。

只从文件本身取证据：头部魔数、尾部结束标记、必要的结构走查，
再和"文件名声称的类型"对照。

判定分三档：
  ok      头尾都对、结构闭合 —— 可以正常打开
  warn    头对、尾部标记缺失或类型不符，或内容本身没有魔数 —— 能用，但要人工确认
  suspect 认不出内容，或者结构明显不成立 —— 多半是坏数据
"""
from __future__ import annotations

import struct

from .device import Source
from .formats import allowed_formats, normalize_ext, texty
from .signatures import _walk_boxes, sniff_at

# 尾部必须出现的结束标记（在文件最后 8KB 内找）
TAIL_MARKERS = {
    "jpeg": b"\xff\xd9",
    "png": b"IEND",
    "gif": b";",
    "pdf": b"%%EOF",
    "zip": b"PK\x05\x06",
    "rar": b"\x1d\x56\xc4\x3d",      # RAR5 end-of-archive
    "dmg": b"koly",
}

# 内容本身没有固定魔数的扩展名：认不出头不代表坏，只代表要人工确认
GENERIC_BINARY = {
    "img", "bin", "raw", "dat", "dd", "toast", "vhd", "vhdx", "vmdk", "qcow2",
    "sparseimage", "e01", "exe", "msi", "dylib", "so", "o", "a", "dmp", "pak",
}

# 头部魔数即结构证据的容器类
STRUCT_HEADER = {"gpt", "exfatvol", "ntfsvol", "apfs", "hfs", "iso",
                 "mkv", "webm", "tiff", "bmp", "wav", "avi", "webp", "sqlite",
                 "xar", "icns", "flac", "ogg"}


def check_written(path: str, size: int, name: str, deep: bool = False) -> tuple[int, str, str]:
    """返回 (可信度 0-100, 判定, 说明)。"""
    if size <= 0:
        return 0, "suspect", "文件长度为 0"

    notes: list[str] = []
    mismatch = False
    try:
        with Source(path) as src:
            head = src.read(0, 1024)
            fmt, _ = sniff_at(src, 0, head=head)
            expected = allowed_formats(name)
            ext = normalize_ext(name)
            tail_len = min(size, 64 * 1024)
            raw_tail = src.read(size - tail_len, tail_len)
            tail_base = size - len(raw_tail)          # 尾块在文件里的起始偏移
            tail = raw_tail.rstrip(b"\x00")

            # UDIF 格式的 dmg 没有头部魔数，只有尾部 koly trailer
            if fmt == "unknown" and b"koly" in tail[-4096:]:
                fmt = "dmg"

            if fmt == "unknown":
                if texty(name):
                    sample = src.read(0, min(size, 128 * 1024))
                    try:
                        sample.decode("utf-8")
                        return 88, "ok", "纯文本，UTF-8 解码正常"
                    except UnicodeDecodeError:
                        return 45, "warn", "无文件头魔数，且不是有效 UTF-8"
                if ext in GENERIC_BINARY:
                    return 58, "warn", f".{ext} 是通用二进制格式，没有固定魔数；已按目录项声明的长度完整导出，需人工确认"
                return 25, "suspect", "认不出文件头"

            score = 45
            mismatch = bool(expected) and fmt not in expected
            if mismatch:
                score -= 15
                notes.append(f"内容像是 {fmt}，与扩展名 .{ext} 不符（可能是同名旧数据或已被覆盖）")
            elif expected:
                score += 10

            if fmt == "zip":
                # ZIP 用"EOCD 结尾必须正好贴着文件尾"来判定：仅仅存在 EOCD 不够，
                # 被别的文件内容顶替时 EOCD 会落在错的偏移上。
                idx = raw_tail.rfind(b"PK\x05\x06")
                if idx >= 0 and len(raw_tail) >= idx + 22:
                    clen = struct.unpack_from("<H", raw_tail, idx + 20)[0]
                    eocd_end = tail_base + idx + 22 + clen
                    if eocd_end == size:
                        score += 35
                    else:
                        score -= 20
                        notes.append(f"EOCD 结束位置比文件尾差 {size - eocd_end} 字节（内容可能不完整或不是同一个文件）")
                else:
                    score -= 25
                    notes.append("尾部没有 EOCD，ZIP 不完整")
            elif fmt in TAIL_MARKERS:
                if TAIL_MARKERS[fmt] in tail[-8192:]:
                    score += 35
                else:
                    score -= 20
                    notes.append(f"尾部没有结束标记（{fmt} 可能被截断）")
            elif fmt in ("mp4", "mov", "heic", "avif"):
                end, seen, err = _walk_boxes(src, 0, size)
                if end == size and not err:
                    score += 35
                    if fmt in ("mp4", "mov") and not seen.get("moov"):
                        score -= 15
                        notes.append("box 结构完整但没有 moov")
                else:
                    score -= 20
                    notes.append(err or f"box 结构在 {end} 字节处中断（声明 {size}）")
            elif fmt == "xar":
                score += 30
                notes.append("xar 没有尾部锚点，长度以目录项声明为准")
            elif fmt in STRUCT_HEADER:
                score += 30
            else:
                score += 20

            if deep and size <= 512 * 1024 * 1024:
                from .signatures import infer_extent
                got, conf, note, _ = infer_extent(src, 0, size)
                if got == size and conf in ("ok", "crc_bad"):
                    score += 15
                elif got and got != size:
                    score -= 10
                    notes.append(f"重新解析得到 {got} 字节，与 {size} 不一致")
                if note:
                    notes.append(note)
    except Exception as exc:                 # 校验出错不该中断整个恢复
        return 30, "suspect", f"校验时出错：{exc}"

    score = max(0, min(100, score))
    if mismatch:
        # 内容类型和文件名对不上时，哪怕长度对得上也不能算"可用"，
        # 这种情况多半是簇已被别的文件占用（救回来的是别人的数据）。
        score = min(score, 45)
        verdict = "suspect"
    else:
        verdict = "ok" if score >= 80 else ("warn" if score >= 50 else "suspect")
    return score, verdict, "；".join(n for n in notes if n)
