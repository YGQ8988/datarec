"""魔数识别 + 长度推断 + 结构自校验。

雕刻(carving)能不能用，全看"结尾在哪"。本模块对每类文件都做真正的结构解析：
JPEG 走 marker 段、PNG/GIF 走块链、MP4/HEIC 走 box 树、ZIP 用中央目录反推起点、
PDF 找 %%EOF、DMG 靠 koly trailer 反推，而不是"往后找下一个魔数"这种拍脑袋办法。

同一个函数还被复用做"恢复后自校验"：把已经落盘的文件再解析一遍，
结构能走到声明的长度，才认为这次恢复可信。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .device import Source

MiB = 1024 * 1024
WINDOW_CAP = 128 * MiB        # 顺扫窗口的最大占用；超过就改用分块扫描器


@dataclass(frozen=True)
class Sig:
    fmt: str
    magic: bytes
    pre: int = 0            # 魔数在文件内的偏移（ftyp 在 +4，CD001 在 +32769）
    post: str = ""          # 附加判定：ftyp 品牌 / RIFF 子类型 / BMP 合理性
    ext: str = ""
    category: str = "other"
    extent: str = ""        # 长度推断函数名
    strong: bool = True     # 魔数本身是否足够独特


SIGS: list[Sig] = [
    # ---- 图片
    Sig("jpeg", b"\xff\xd8\xff", 0, "", "jpg", "images", "jpeg"),
    Sig("png", b"\x89PNG\r\n\x1a\n", 0, "", "png", "images", "png"),
    Sig("gif", b"GIF89a", 0, "", "gif", "images", "gif"),
    Sig("gif", b"GIF87a", 0, "", "gif", "images", "gif"),
    Sig("tiff", b"II*\x00", 0, "", "tiff", "images", "tiff"),
    Sig("tiff", b"MM\x00*", 0, "", "tiff", "images", "tiff"),
    Sig("bmp", b"BM", 0, "bmp", "bmp", "images", "bmp", strong=False),
    Sig("webp", b"RIFF", 0, "webp", "webp", "images", "riff"),
    Sig("icns", b"icns", 0, "", "icns", "images", "icns"),
    Sig("psd", b"8BPS", 0, "", "psd", "images", "psd"),
    Sig("heic", b"ftyp", 4, "heic", "heic", "images", "mp4"),
    Sig("avif", b"ftyp", 4, "avif", "avif", "images", "mp4"),
    # ---- 视频 / 音频
    Sig("mp4", b"ftyp", 4, "mp4", "mp4", "videos", "mp4"),
    Sig("mov", b"ftyp", 4, "mov", "mov", "videos", "mp4"),
    Sig("mkv", b"\x1aE\xdf\xa3", 0, "", "mkv", "videos", "ebml"),
    Sig("avi", b"RIFF", 0, "avi", "avi", "videos", "riff"),
    Sig("wav", b"RIFF", 0, "wav", "wav", "audio", "riff"),
    Sig("flac", b"fLaC", 0, "", "flac", "audio", "flac"),
    Sig("ogg", b"OggS", 0, "", "ogg", "audio", "boundary"),
    Sig("mp3", b"ID3", 0, "", "mp3", "audio", "mp3tag", strong=False),
    # ---- 文档 / 数据
    Sig("pdf", b"%PDF-", 0, "", "pdf", "documents", "pdf"),
    Sig("zip", b"PK\x03\x04", 0, "", "zip", "archives", "zip"),
    Sig("sqlite", b"SQLite format 3\x00", 0, "", "db", "documents", "sqlite"),
    # ---- 压缩包
    Sig("rar", b"Rar!\x1a\x07", 0, "", "rar", "archives", "rar"),
    Sig("7z", b"7z\xbc\xaf\x27\x1c", 0, "", "7z", "archives", "7z"),
    Sig("gzip", b"\x1f\x8b\x08", 0, "", "gz", "archives", "boundary", strong=False),
    Sig("tar", b"ustar", 257, "", "tar", "archives", "tar"),
    # ---- 镜像 / 安装包
    Sig("iso", b"CD001", 0x8001, "", "iso", "diskimages", "iso"),
    Sig("gpt", b"EFI PART", 512, "", "img", "diskimages", "gpt"),
    Sig("exfatvol", b"EXFAT   ", 3, "", "img", "diskimages", "exfatvol", strong=False),
    Sig("ntfsvol", b"NTFS    ", 3, "", "img", "diskimages", "ntfsvol", strong=False),
    Sig("apfs", b"NXSB", 32, "", "img", "diskimages", "apfs", strong=False),
    Sig("xar", b"xar!", 0, "", "pkg", "packages", "xar"),
]

MAX_PRE = max(s.pre + len(s.magic) for s in SIGS)


# --------------------------------------------------------------------------- 品牌 / 子类型
def classify_ftyp(brand4: bytes) -> str | None:
    b = brand4.strip()
    if b in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"mif1", b"msf1"):
        return "heic"
    if b in (b"avif", b"avis"):
        return "avif"
    if b == b"qt  ":
        return "mov"
    if b in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1",
             b"dash", b"M4V ", b"M4A ", b"M4P ", b"3gp4", b"3gp5", b"3gp6", b"f4v "):
        return "mp4"
    return None


def ftyp_kind(head: bytes) -> str | None:
    """看 major brand 和兼容品牌列表，判定 mp4 / mov / heic / avif。"""
    kind = classify_ftyp(head[8:12]) if len(head) >= 12 else None
    if kind:
        return kind
    if len(head) >= 20:
        return classify_ftyp(head[16:20])
    return None


def classify_riff(sub4: bytes) -> str | None:
    return {b"WEBP": "webp", b"AVI ": "avi", b"WAVE": "wav"}.get(sub4)


# --------------------------------------------------------------------------- 格式嗅探
def sniff_at(src: Source, start: int, head: bytes | None = None) -> tuple[str, str]:
    """看文件头判断格式。

    默认读 4KB：GPT 的魔数在 +512、APFS 在 +32、tar 在 +257，
    只读 512 字节会把它们全漏掉（踩过的坑）。
    """
    if head is None or len(head) < 1024:
        head = src.read(start, 4096)
    if len(head) < 16:
        return "unknown", "数据不足"
    for sig in SIGS:
        if not head.startswith(sig.magic, sig.pre):
            continue
        if sig.post == "bmp" and not _plausible_bmp(head):
            continue
        if sig.post in ("webp", "avi", "wav") and classify_riff(head[8:12]) != sig.post:
            continue
        if sig.post in ("mp4", "mov", "heic", "avif") and ftyp_kind(head) != sig.post:
            continue
        if sig.fmt == "mp3" and head[3] not in (2, 3, 4):
            continue
        return sig.fmt, ""
    # ISO9660：魔数在 32KB 处，单独补一次读取
    if src.read(start + 0x8000, 6) == b"\x01CD001":
        return "iso", ""
    return "unknown", ""


def _plausible_bmp(head: bytes) -> bool:
    if len(head) < 30:
        return False
    fsize = struct.unpack_from("<I", head, 2)[0]
    data_off = struct.unpack_from("<I", head, 10)[0]
    dib = struct.unpack_from("<I", head, 14)[0]
    return (fsize > 26 and data_off >= 26 and dib in (12, 40, 52, 56, 64, 108, 124)
            and head[6:10] == b"\x00\x00\x00\x00")


def sig_confirms(sig: Sig, src: Source, start: int) -> bool:
    """扫描时对弱魔数做二次确认，压低误报。"""
    head = src.read(start, 64)
    if len(head) < 16:
        return False
    if sig.post == "bmp":
        return _plausible_bmp(head)
    if sig.post in ("webp", "avi", "wav"):
        return classify_riff(head[8:12]) == sig.post
    if sig.post in ("mp4", "mov", "heic", "avif"):
        return ftyp_kind(head) == sig.post
    if sig.fmt == "mp3":
        return head[3] in (2, 3, 4)
    if sig.fmt == "iso":
        return src.read(start + 0x8000, 1) == b"\x01"
    if sig.fmt == "tar":
        return src.read(start + 257, 5) == b"ustar"
    return True


# --------------------------------------------------------------------------- 长度推断入口
def infer_extent(src: Source, start: int, limit: int, fmt_hint: str | None = None
                 ) -> tuple[int | None, str, str, str]:
    """推断从 start 开始的文件长度。

    返回 (长度, 结论, 说明, 格式)。结论：
      ok / crc_bad / partial / fail
    """
    fmt = fmt_hint
    note = ""
    if not fmt:
        fmt, note = sniff_at(src, start)
    if fmt in (None, "unknown"):
        return None, "fail", note or "认不出格式", "unknown"
    func = _EXTENT.get(fmt)
    if func is None:
        return None, "fail", f"{fmt} 没有长度推断器", fmt
    try:
        size, conf, extra = func(src, start, limit)
    except Exception as exc:      # 单个怪文件不该中断整盘扫描
        return None, "fail", f"解析异常：{exc}", fmt
    note = "；".join(x for x in (note, extra) if x)
    return size, conf, note, fmt


def zip_flavor(src: Source, start: int, size: int) -> tuple[str, str]:
    """ZIP 家族细分：docx / xlsx / pptx / epub / 普通 zip。"""
    if size <= 0:
        return "zip", "zip"
    # 只在文件自身范围内找中央目录里的部件名，越界读到邻居文件会误判成 docx
    tail_len = min(size, 64 * 1024)
    tail = src.read(start + size - tail_len, tail_len)
    head = src.read(start, min(size, 4096))
    names = head + tail
    # 先按主部件精确定位，再看目录前缀，避免"文档里带个 word/ 子目录"就判成 docx
    if b"word/document.xml" in names:
        return "docx", "docx"
    if b"xl/workbook.xml" in names:
        return "xlsx", "xlsx"
    if b"ppt/presentation.xml" in names:
        return "pptx", "pptx"
    if b"[Content_Types].xml" in names:
        # 退回看目录前缀，但只在"恰好命中一种"时才下结论
        hits = [k for k, prefix in (("docx", b"word/"), ("xlsx", b"xl/"), ("pptx", b"ppt/"))
                if prefix in names]
        if len(hits) == 1:
            return hits[0], hits[0]
        return "zip", "zip"
    if b"mimetype" in names and b"application/epub+zip" in names:
        return "epub", "epub"
    return "zip", "zip"


# --------------------------------------------------------------------------- 顺扫窗口
class _Win:
    """按需扩大的只读窗口，附绝对偏移语义。"""

    def __init__(self, src: Source, start: int, limit: int, initial: int = 256 * 1024):
        self.src = src
        self.start = start
        self.limit = max(0, limit)
        self.buf = bytearray()
        self._read(min(initial, self.limit))

    def _read(self, target: int) -> None:
        target = min(target, self.limit)
        if target <= len(self.buf):
            return
        chunk = self.src.read(self.start + len(self.buf), target - len(self.buf))
        self.buf += chunk
        if not chunk:
            self.limit = len(self.buf)

    def need(self, n: int) -> bool:
        if n <= len(self.buf):
            return True
        if n > self.limit:
            self._read(self.limit)
            return len(self.buf) >= n
        self._read(min(self.limit, max(n, len(self.buf) * 4, 128 * 1024)))
        return len(self.buf) >= n

    def find(self, needle: bytes, start: int = 0) -> int:
        pos = start
        while True:
            idx = self.buf.find(needle, pos)
            if idx >= 0:
                return idx
            if len(self.buf) >= self.limit:
                return -1
            pos = max(0, len(self.buf) - len(needle) + 1)
            before = len(self.buf)
            self._read(min(self.limit, max(before * 2, before + MiB)))
            if len(self.buf) == before:
                return -1


class _Chunks:
    """分块扫描器：内存恒定，用来在超大文件里找一个字节串的所有出现位置。

    每块之间保留 overlap 字节，避免跨块的匹配被漏掉。
    """

    def __init__(self, src: Source, start: int, limit: int, chunk: int = 4 * MiB,
                 overlap: int = 4096):
        self.src = src
        self.start = start
        self.limit = limit
        self.chunk = chunk
        self.overlap = overlap

    def offsets(self, needle: bytes, max_hits: int = 100000):
        pos = 0
        hits: list[int] = []
        while pos < self.limit and len(hits) < max_hits:
            buf = self.src.read(self.start + pos, min(self.chunk + self.overlap, self.limit - pos))
            if not buf:
                return hits
            base = 0
            while True:
                idx = buf.find(needle, base)
                if idx < 0:
                    break
                hits.append(pos + idx)
                base = idx + 1
                if len(hits) >= max_hits:
                    break
            if len(buf) <= self.overlap:
                return hits
            pos += len(buf) - self.overlap
        return hits


# --------------------------------------------------------------------------- JPEG
def _extent_jpeg(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    limit = min(limit, start + WINDOW_CAP) if limit - start > WINDOW_CAP else limit
    w = _Win(src, start, limit, initial=512 * 1024)
    if not w.need(4) or bytes(w.buf[0:2]) != b"\xff\xd8":
        return None, "fail", "不是 JPEG 起始标记"
    pos = 2
    saw_sof = saw_sos = False
    for _ in range(500000):
        if not w.need(pos + 2):
            return None, "fail", "找到 EOI 前数据就结束了（文件被截断）"
        if w.buf[pos] != 0xFF:
            nxt = w.buf.find(b"\xff", pos)
            if nxt < 0:
                before = len(w.buf)
                w.need(min(w.limit, before + 4 * MiB))
                if len(w.buf) == before:
                    return None, "fail", "找不到下一个标记"
                pos = max(0, before - 1)
                continue
            pos = nxt
            continue
        marker = w.buf[pos + 1]
        if marker == 0xD9:                        # EOI
            return pos + 2, ("ok" if (saw_sof and saw_sos) else "partial"), ""
        if marker in (0xD8, 0x01, 0x00) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if not w.need(pos + 4):
            return None, "fail", "段头被截断"
        seglen = struct.unpack_from(">H", w.buf, pos + 2)[0]
        if seglen < 2:
            pos += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            saw_sof = True
        seg_end = pos + 2 + seglen
        if not w.need(seg_end):
            return None, "fail", "JPEG 段长度越界"
        if marker == 0xDA:                        # SOS，后面是熵编码数据
            saw_sos = True
            p = seg_end
            while True:
                if not w.need(p + 2):
                    return None, "fail", "SOS 之后到数据尾都没等到 EOI"
                i = w.buf.find(b"\xff", p)
                if i < 0:
                    before = len(w.buf)
                    w.need(min(w.limit, before + 4 * MiB))
                    if len(w.buf) == before:
                        return None, "fail", "SOS 之后到数据尾都没等到 EOI"
                    p = max(0, before - 1)
                    continue
                if i + 1 >= len(w.buf):
                    p = i
                    continue
                nxt = w.buf[i + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    p = i + 2
                elif nxt == 0xFF:
                    p = i + 1
                else:
                    pos = i
                    break
            continue
        pos = seg_end
    return None, "fail", "JPEG 段落数异常，疑似不是真正的 JPEG"


# --------------------------------------------------------------------------- PNG
def _extent_png(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    if limit - start > 512 * MiB:
        limit = start + 512 * MiB
    w = _Win(src, start, limit, initial=256 * 1024)
    if not w.need(33):
        return None, "fail", "PNG 头不完整"
    pos = 8
    crc_bad = False
    first = True
    for _ in range(100000):
        if not w.need(pos + 8):
            return None, "fail", "没有 IEND，文件可能被截断"
        length = struct.unpack_from(">I", w.buf, pos)[0]
        ctype = bytes(w.buf[pos + 4:pos + 8])
        end = pos + 12 + length
        if first and ctype != b"IHDR":
            return None, "fail", "首个块不是 IHDR"
        first = False
        if length > 512 * MiB or end <= pos:
            return None, "fail", f"块长度异常({length})"
        if not w.need(end):
            return None, "fail", "块越界，文件可能被截断"
        stored = struct.unpack_from(">I", w.buf, end - 4)[0]
        if zlib.crc32(bytes(w.buf[pos + 4:end - 4])) & 0xFFFFFFFF != stored:
            crc_bad = True
        if ctype == b"IEND":
            return end, ("crc_bad" if crc_bad else "ok"), ""
        pos = end
    return None, "fail", "PNG 块过多"


# --------------------------------------------------------------------------- GIF
def _extent_gif(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    limit = min(limit, start + 256 * MiB)
    w = _Win(src, start, limit, initial=256 * 1024)
    if not w.need(13):
        return None, "fail", "GIF 头不完整"
    gct = w.buf[10]
    pos = 13
    if gct & 0x80:
        pos += 3 * (2 << (gct & 0x07))
    for _ in range(2000000):
        if not w.need(pos + 1):
            return None, "fail", "没有 trailer(0x3B)"
        block = w.buf[pos]
        if block == 0x3B:
            return pos + 1, "ok", ""
        if block == 0x2C:
            if not w.need(pos + 10):
                return None, "fail", "图像描述符被截断"
            lflags = w.buf[pos + 9]
            pos += 10
            if lflags & 0x80:
                pos += 3 * (2 << (lflags & 0x07))
            pos += 1                      # LZW minimum code size
            while True:
                if not w.need(pos + 1):
                    return None, "fail", "LZW 子块被截断"
                size = w.buf[pos]
                pos += 1 + size
                if size == 0:
                    break
            continue
        if block == 0x21:
            if not w.need(pos + 2):
                return None, "fail", "扩展块被截断"
            pos += 2
            while True:
                if not w.need(pos + 1):
                    return None, "fail", "扩展子块被截断"
                size = w.buf[pos]
                pos += 1 + size
                if size == 0:
                    break
            continue
        return None, "fail", f"未知块类型 0x{block:02x}"
    return None, "fail", "GIF 块过多"


# --------------------------------------------------------------------------- TIFF
def _extent_tiff(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 16)
    if len(head) < 8:
        return None, "fail", "TIFF 头不完整"
    endian = "<" if head[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", head, 2)[0] != 42:
        return None, "fail", "TIFF 魔数不对"
    ifd = struct.unpack_from(endian + "I", head, 4)[0]
    max_end = ifd + 2
    type_size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    seen: set[int] = set()
    cur = ifd
    for _ in range(64):
        if cur == 0 or cur in seen:
            break
        seen.add(cur)
        ifd_head = src.read(start + cur, 2)
        if len(ifd_head) < 2:
            return None, "fail", "IFD 越界"
        count = struct.unpack_from(endian + "H", ifd_head, 0)[0]
        if count > 4096:
            return None, "fail", "IFD 项数异常"
        body = src.read(start + cur + 2, count * 12 + 4)
        if len(body) < count * 12 + 4:
            return None, "fail", "IFD 被截断"
        offsets: list[int] = []
        counts: list[int] = []
        for i in range(count):
            tag, typ = struct.unpack_from(endian + "HH", body, i * 12)
            num = struct.unpack_from(endian + "I", body, i * 12 + 4)[0]
            tsize = type_size.get(typ, 1) * max(1, num)
            if typ == 3 and num == 1:
                vals = [struct.unpack_from(endian + "H", body, i * 12 + 8)[0]]
            elif tsize <= 4:
                vals = [struct.unpack_from(endian + "I", body, i * 12 + 8)[0]]
            else:
                voff = struct.unpack_from(endian + "I", body, i * 12 + 8)[0]
                raw = src.read(start + voff, min(tsize, 4))
                vals = [struct.unpack_from(endian + ("H" if typ == 3 else "I"), raw, 0)[0]] if len(raw) >= 4 else []
                max_end = max(max_end, voff + tsize)
            if tag in (273, 324):
                offsets.extend(vals)
            elif tag in (279, 325):
                counts.extend(vals)
        for off, cnt in zip(offsets, counts):
            if cnt < 100 * 1024 * 1024:
                max_end = max(max_end, off + cnt)
        nxt = struct.unpack_from(endian + "I", body, count * 12)[0]
        cur = nxt
    if max_end <= 8:
        return None, "fail", "TIFF 结构里没有图像数据"
    return max_end, "ok", ""


# --------------------------------------------------------------------------- MP4 / HEIC
def _walk_boxes(src: Source, start: int, limit: int) -> tuple[int, dict, str]:
    pos = start
    seen: dict[str, int] = {}
    for _ in range(20000):
        if pos + 8 > limit:
            return pos, seen, ""
        hdr = src.read(pos, 16)
        if len(hdr) < 8:
            return pos, seen, "box 头被截断"
        size = struct.unpack_from(">I", hdr, 0)[0]
        typ = hdr[4:8]
        hsize = 8
        if size == 1:
            if len(hdr) < 16:
                return pos, seen, "64 位 box 头被截断"
            size = struct.unpack_from(">Q", hdr, 8)[0]
            hsize = 16
        elif size == 0:
            if not all(32 <= b < 127 for b in typ):
                return pos, seen, ""        # 后面已经不是 box 了，属于正常结束
            return pos, seen, "末尾 box 声明'直到文件尾'，尾部长度不可判定"
        if size < hsize or size > limit - pos:
            return pos, seen, f"{typ!r} box 长度非法({size})"
        if not all(32 <= b < 127 for b in typ):
            return pos, seen, "box 类型里出现非 ASCII 字节"
        name = typ.decode("ascii", "replace")
        seen[name] = seen.get(name, 0) + 1
        pos += size
    return pos, seen, "box 层数过多"


def _extent_mp4(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    end, seen, err = _walk_boxes(src, start, limit)
    size = end - start
    if size <= 8:
        return None, "fail", err or "box 结构为空"
    is_image_container = bool(seen.get("meta"))
    has_moov = bool(seen.get("moov")) or is_image_container
    has_mdat = bool(seen.get("mdat")) or bool(seen.get("idat"))
    conf = "ok" if (has_moov and has_mdat and not err) else "partial"
    notes = []
    if err:
        notes.append(err)
    if not has_moov:
        notes.append("没有 moov（文件可能被截断）")
    if not has_mdat:
        notes.append("没有 mdat")
    return size, conf, "；".join(notes)


# --------------------------------------------------------------------------- PDF
def _extent_pdf(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    limit = min(limit, start + 2 * 1024 * MiB)          # 单文件搜索窗口上限
    hits = _Chunks(src, start, limit).offsets(b"%%EOF", max_hits=2000)
    if not hits:
        return None, "fail", "没有 %%EOF，PDF 不完整"
    chosen = hits[0] + 5
    for h in hits[1:]:
        gap_len = h - chosen
        if gap_len > 8 * MiB:
            break
        gap = src.read(start + chosen, gap_len)
        if b"%PDF-" in gap:
            break
        if b" obj" in gap or b"xref" in gap or b"startxref" in gap:
            chosen = h + 5
    # 吃掉结尾换行
    for _ in range(2):
        if src.read(start + chosen, 1) in (b"\n", b"\r"):
            chosen += 1
    head = src.read(start, min(chosen, 1 * MiB))
    conf = "ok" if b" obj" in head else "partial"
    return chosen, conf, ""


# --------------------------------------------------------------------------- ZIP
def _extent_zip(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    limit = min(limit, start + 4 * 1024 * MiB)          # 超过 4GB 的 zip 极罕见
    hits = _Chunks(src, start, limit, chunk=2 * MiB).offsets(b"PK\x05\x06", max_hits=4096)
    if not hits:
        return None, "fail", "没有 EOCD，ZIP 不完整（可能被截断）"
    last_err = "EOCD 结构不合法"
    for off in hits:
        eocd = src.read(start + off, 22)
        if len(eocd) < 22:
            continue
        cd_size = struct.unpack_from("<I", eocd, 12)[0]
        cd_off = struct.unpack_from("<I", eocd, 16)[0]
        comment_len = struct.unpack_from("<H", eocd, 20)[0]
        end = off + 22 + comment_len
        expected = off - cd_off - cd_size        # 用中央目录反推 zip 起点
        if off >= cd_off + cd_size and -65536 < expected < 65536:
            if expected == 0:
                return end, "ok", ""
            return end, "partial", f"中央目录偏移差 {expected} 字节（可能起点不精确）"
        last_err = f"EOCD 里中央目录偏移不合理（cd_off={cd_off}, cd_size={cd_size}, eocd={off}）"
    return None, "fail", last_err


# --------------------------------------------------------------------------- 其他容器
def _extent_ebml(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 32)
    if len(head) < 12:
        return None, "fail", "EBML 头不完整"
    pos = 4
    first = head[pos]
    if first == 0:
        return None, "fail", "EBML 长度非法"
    length, mask = 1, 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(head):
        return None, "fail", "EBML 长度字段异常"
    value = first & (mask - 1)
    unknown = value == (mask - 1)
    for i in range(1, length):
        value = (value << 8) | head[pos + i]
        unknown = unknown and head[pos + i] == 0xFF
    if unknown:
        return _boundary(src, start, limit, "EBML 段长度未知（流式写入），按边界裁剪",
                         window=2 * 1024 * MiB)
    total = pos + length + value
    if total < 512:
        return _boundary(src, start, limit, "EBML 头长度过小，按边界裁剪",
                         window=2 * 1024 * MiB)
    if total > limit:
        return None, "fail", "EBML 声明长度不合法"
    doctype = src.read(start + pos + length, min(value, 8192))
    return total, "ok", "webm" if b"webm" in doctype else ""


def _extent_riff(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 12)
    if len(head) < 12:
        return None, "fail", "RIFF 头不完整"
    size = struct.unpack_from("<I", head, 4)[0]
    total = 8 + size
    if total < 12 or total > limit:
        return None, "fail", f"RIFF 声明长度不合法({total})"
    return total, "ok", ""


def _extent_7z(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 32)
    if len(head) < 32:
        return None, "fail", "7z 头不完整"
    next_off = struct.unpack_from("<Q", head, 12)[0]
    next_size = struct.unpack_from("<Q", head, 20)[0]
    total = 32 + next_off + next_size
    if total > limit or total < 32:
        return None, "fail", "7z 声明长度不合法"
    return total, "ok", ""


def _extent_rar(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 8)
    if len(head) >= 7 and head[6] == 1:      # RAR5
        return _boundary(src, start, limit, "RAR5 头结构复杂，按边界裁剪")
    pos = start + 7
    known = {0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x7A, 0x7B}
    for idx in range(20000):
        hdr = src.read(pos, 7)
        if len(hdr) < 7:
            return None, "fail", "RAR 块头被截断"
        _crc, typ, flags, hsize = struct.unpack("<HBHH", hdr)
        if hsize < 7 or (idx == 0 and typ not in known):
            return None, "fail", "RAR 块头不合法"
        add = 0
        if flags & 0x8000 and hsize >= 11:
            add = struct.unpack_from("<I", src.read(pos, hsize), 7)[0]
        pos += hsize + add
        if typ == 0x7B:
            return pos - start, "ok", ""
        if pos > limit:
            return None, "fail", "RAR 块越界"
    return None, "fail", "RAR 块过多"


def _extent_iso(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start + 0x8000, 96)
    if len(head) < 96:
        return None, "fail", "ISO 卷描述符不完整"
    blocks = struct.unpack_from("<I", head, 80)[0]
    if blocks == 0:
        return None, "fail", "ISO 块数为 0"
    total = blocks * 2048
    if total > limit:
        return _boundary(src, start, limit, "ISO 声明长度超出范围，按边界裁剪")
    return total, "ok", ""


def _extent_gpt(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    """GPT 磁盘镜像：主头在 LBA1，备份头在最后一个 LBA，能用它反推整盘长度。"""
    head = src.read(start + 512, 92)
    if len(head) < 92 or head[:8] != b"EFI PART":
        return None, "fail", "没有 GPT 头"
    my_lba = struct.unpack_from("<Q", head, 24)[0]
    alt_lba = struct.unpack_from("<Q", head, 32)[0]
    if my_lba != 1:
        return None, "fail", f"这不是主 GPT 头（my_lba={my_lba}）"
    total = (alt_lba + 1) * 512
    if total < 1024 * 1024 or total > limit:
        return None, "fail", "GPT 声明的整盘长度不合理"
    backup = src.read(start + alt_lba * 512, 8)
    conf = "ok" if backup == b"EFI PART" else "partial"
    note = "" if conf == "ok" else "备分 GPT 头不在预期位置，长度可能有偏差"
    return total, conf, note


def _extent_exfatvol(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    """镜像文件里套着一整个 exFAT 卷（磁盘备份很常见）。"""
    boot = src.read(start, 512)
    if len(boot) < 512 or boot[3:11] != b"EXFAT   ":
        return None, "fail", "不是 exFAT 引导扇区"
    bps = 1 << boot[108] if 9 <= boot[108] <= 12 else 0
    sectors = struct.unpack_from("<Q", boot, 72)[0]
    if not bps or sectors < 16:
        return None, "fail", "exFAT 引导扇区参数异常"
    total = sectors * bps
    if total > limit:
        return None, "fail", "exFAT 卷长度超出可扫描范围"
    return total, "ok", "内含一个 exFAT 卷"


def _extent_ntfsvol(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    boot = src.read(start, 512)
    if len(boot) < 512 or boot[3:11] != b"NTFS    ":
        return None, "fail", "不是 NTFS 引导扇区"
    bps = struct.unpack_from("<H", boot, 11)[0]
    sectors = struct.unpack_from("<Q", boot, 40)[0]
    if bps not in (512, 1024, 2048, 4096) or sectors < 16:
        return None, "fail", "NTFS 引导扇区参数异常"
    return sectors * bps, "ok", "内含一个 NTFS 卷"


def _extent_apfs(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start + 32, 16)
    if len(head) < 16 or head[:4] != b"NXSB":
        return None, "fail", "不是 APFS 容器头"
    block_size = struct.unpack_from("<I", head, 4)[0]
    block_count = struct.unpack_from("<Q", head, 8)[0]
    if not (4096 <= block_size <= 65536) or block_count == 0:
        return None, "fail", "APFS 参数异常"
    total = block_size * block_count
    if total > limit:
        return None, "fail", "APFS 容器长度超出可扫描范围"
    return total, "ok", "内含一个 APFS 容器"


def _extent_hfs(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    vh = src.read(start + 1024, 512)
    if len(vh) < 512 or vh[:2] != b"H+":
        return None, "fail", "不是 HFS+ 卷头"
    block_size = struct.unpack_from(">I", vh, 40)[0]
    total_blocks = struct.unpack_from(">I", vh, 44)[0]
    if not (512 <= block_size <= 1024 * 1024) or total_blocks == 0:
        return None, "fail", "HFS+ 卷头参数异常"
    total = block_size * total_blocks
    if total > limit:
        return None, "fail", "HFS+ 卷长度超出可扫描范围"
    return total, "ok", "内含一个 HFS+ 卷"


def _extent_xar(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 28)
    if len(head) < 28:
        return None, "fail", "xar 头不完整"
    hsize = struct.unpack_from(">H", head, 4)[0]
    toc_c = struct.unpack_from(">Q", head, 8)[0]
    if hsize < 28 or toc_c > limit:
        return None, "fail", "xar 头字段异常"
    return _boundary(src, start, limit,
                     f"xar：头+toc 已确认 {hsize + toc_c} 字节，堆区按边界裁剪")


def _extent_icns(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    pos = start + 8
    for _ in range(2000):
        hdr = src.read(pos, 8)
        if len(hdr) < 8:
            return None, "fail", "ICNS 图标块被截断"
        size = struct.unpack_from(">I", hdr, 4)[0]
        if size < 8:
            return None, "fail", "ICNS 块长度非法"
        pos += size
        if pos - start > limit:
            return None, "fail", "ICNS 块越界"
    return pos - start, "ok", ""


def _extent_bmp(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 32)
    if len(head) < 32 or not _plausible_bmp(head):
        return None, "fail", "BMP 头不合理"
    size = struct.unpack_from("<I", head, 2)[0]
    if size <= limit and size > 26:
        return size, "ok", ""
    return _boundary(src, start, limit, "BMP 头部声明长度不可信，按边界裁剪")


def _extent_psd(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    return _boundary(src, start, limit, "PSD 长度只能按边界裁剪")


def _extent_mp3(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 10)
    if len(head) < 10:
        return None, "fail", "ID3 头不完整"
    size = (((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14)
            | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F))
    return _boundary(src, start, limit, f"ID3v2 头 {10 + size} 字节，音频帧按边界裁剪")


def _extent_gzip(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    """gzip 魔数只有 3 字节，误命中很多。先做头部自检，再试解压一小段确认。"""
    head = src.read(start, 10)
    if len(head) < 10 or head[:3] != b"\x1f\x8b\x08":
        return None, "fail", "不是 gzip 头"
    if head[8] not in (0, 2, 4) or head[9] > 13:
        return None, "fail", "gzip 头部字段不合理（多半是误命中）"
    sample = src.read(start + 10, min(256 * 1024, max(1024, limit - start - 10)))
    d = zlib.decompressobj(-15)
    try:
        d.decompress(sample, 1 << 20)
    except zlib.error:
        return None, "fail", "deflate 数据不成立（误命中）"
    return _boundary(src, start, limit, "gzip 长度按边界裁剪", window=64 * 1024 * 1024)


def _extent_flac(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    return _boundary(src, start, limit, "FLAC 帧长度需逐帧解析，按边界裁剪")


def _extent_sqlite(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    head = src.read(start, 32)
    if len(head) < 32:
        return None, "fail", "SQLite 头不完整"
    page_size = struct.unpack_from(">H", head, 16)[0]
    if page_size == 1:
        page_size = 65536
    pages = struct.unpack_from(">I", head, 28)[0]
    if not (512 <= page_size <= 65536) or pages == 0:
        return None, "fail", "SQLite 页参数异常"
    return page_size * pages, "ok", ""


def _tar_header_ok(hdr: bytes) -> bool:
    """tar 头自带 8 字节八进制校验和，误命中 'ustar' 时靠它立刻否掉。"""
    stored = hdr[148:156].replace(b"\x00", b" ").strip()
    if not stored:
        return False
    try:
        want = int(stored, 8)
    except ValueError:
        return False
    unsigned = sum(hdr[:148]) + 256 + sum(hdr[156:512])
    signed = (sum(b if b < 128 else b - 256 for b in hdr[:148]) + 256
              + sum(b if b < 128 else b - 256 for b in hdr[156:512]))
    return want in (unsigned, signed)


def _extent_tar(src: Source, start: int, limit: int) -> tuple[int | None, str, str]:
    pos = start
    for i in range(20000):
        hdr = src.read(pos, 512)
        if len(hdr) < 512:
            return None, "fail", "tar 头被截断"
        if hdr == b"\x00" * 512:
            return pos + 1024 - start, "ok", ""
        if not _tar_header_ok(hdr):
            return None, "fail", "tar 头校验和不符（不是真正的 tar）"
        try:
            size = int(hdr[124:136].replace(b"\x00", b" ").strip() or b"0", 8)
        except ValueError:
            return None, "fail", "tar size 字段无法解析"
        pos += 512 + ((size + 511) // 512) * 512
        if pos - start > limit:
            return None, "fail", "tar 块越界"
    return None, "fail", "tar 块过多"


def _boundary(src: Source, start: int, limit: int, note: str,
              window: int = 256 * MiB) -> tuple[int | None, str, str]:
    """万能兜底：找下一个文件头，再把尾部 0 填充裁掉。"""
    nxt = next_signature_offset(src, start + 64, min(limit, start + window))
    end = nxt if nxt else limit
    end = _trim_zeros(src, start, end)
    if end <= start + 16:
        return None, "fail", "边界裁剪后长度为 0"
    return end - start, "partial", note


def _trim_zeros(src: Source, start: int, end: int) -> int:
    """从尾部往前删掉 0 填充（簇尾、未写区域都是 0）。"""
    block = 64 * 1024
    while end - start > block:
        tail = src.read(end - block, block)
        if not tail:
            break
        stripped = tail.rstrip(b"\x00")
        if len(stripped) == len(tail):
            break
        if not stripped:
            end -= block
            continue
        return end - block + len(stripped)
    tail = src.read(max(start, end - 4096), min(4096, end - start))
    stripped = tail.rstrip(b"\x00")
    return max(start + 1, end - (len(tail) - len(stripped)))


def next_signature_offset(src: Source, start: int, limit: int) -> int | None:
    """找 start 之后第一个"真的像新文件"的位置。"""
    step = 512 * 1024
    pos = start
    while pos < limit:
        chunk = src.read(pos, min(step, limit - pos))
        if not chunk:
            return None
        cands: list[int] = []
        for sig in SIGS:
            if not sig.strong:              # 弱魔数（BM/ID3/gzip 等）不配当边界
                continue
            idx = chunk.find(sig.magic)
            while idx >= 0:
                abs_pos = pos + idx - sig.pre
                if abs_pos >= start:
                    cands.append(abs_pos)
                idx = chunk.find(sig.magic, idx + 1)
        for cand in sorted(cands):
            if sniff_at(src, cand)[0] != "unknown":
                return cand
        pos += max(1, len(chunk) - MAX_PRE)
    return None


_EXTENT = {
    "jpeg": _extent_jpeg, "png": _extent_png, "gif": _extent_gif, "tiff": _extent_tiff,
    "bmp": _extent_bmp, "psd": _extent_psd, "icns": _extent_icns,
    "mp4": _extent_mp4, "mov": _extent_mp4, "heic": _extent_mp4, "avif": _extent_mp4,
    "mkv": _extent_ebml, "webm": _extent_ebml,
    "avi": _extent_riff, "wav": _extent_riff, "webp": _extent_riff,
    "pdf": _extent_pdf, "zip": _extent_zip, "7z": _extent_7z, "rar": _extent_rar,
    "iso": _extent_iso, "xar": _extent_xar, "gpt": _extent_gpt,
    "exfatvol": _extent_exfatvol, "ntfsvol": _extent_ntfsvol,
    "apfs": _extent_apfs, "hfs": _extent_hfs, "mp3": _extent_mp3, "flac": _extent_flac,
    "sqlite": _extent_sqlite, "tar": _extent_tar,
    "ogg": lambda s, o, l: _boundary(s, o, l, "OGG 页链未逐页解析，按边界裁剪"),
    "gzip": _extent_gzip,
}


# --------------------------------------------------------------------------- DMG
def find_dmg_extents(src: Source, scan_start: int, scan_end: int,
                     max_hits: int = 500) -> list[tuple[int, int, str]]:
    """DMG 结尾有 512 字节 koly trailer，从 trailer 反推文件起点。

    返回 [(起点, 长度, 说明)]，只处理 UDIF（绝大多数 .dmg）。
    """
    out: list[tuple[int, int, str]] = []
    step = 4 * MiB
    pos = max(scan_start, 0)
    seen: set[tuple[int, int]] = set()
    while pos < scan_end and len(out) < max_hits:
        chunk = src.read(pos, min(step + 512, scan_end - pos))
        if not chunk:
            break
        idx = chunk.find(b"koly")
        while idx >= 0:
            trailer_off = pos + idx
            trader = src.read(trailer_off, 512)
            if len(trader) == 512:
                hit = _dmg_from_trailer(src, trailer_off, trader, scan_start)
                if hit and (hit[0], hit[1]) not in seen:
                    seen.add((hit[0], hit[1]))
                    out.append(hit)
            idx = chunk.find(b"koly", idx + 1)
        pos += step
    return out


def _dmg_from_trailer(src: Source, trailer_off: int, trailer: bytes,
                      scan_start: int) -> tuple[int, int, str] | None:
    try:
        version = struct.unpack_from(">I", trailer, 4)[0]
        data_off = struct.unpack_from(">Q", trailer, 24)[0]
        data_len = struct.unpack_from(">Q", trailer, 32)[0]
        rsrc_len = struct.unpack_from(">Q", trailer, 48)[0]
        xml_off = struct.unpack_from(">Q", trailer, 216)[0]
        xml_len = struct.unpack_from(">Q", trailer, 224)[0]
    except struct.error:
        return None
    if version not in (1, 2, 3, 4) or data_len == 0:
        return None
    total = 512 + xml_len + rsrc_len + data_len
    start = trailer_off + 512 - total
    if start < scan_start or start < 0 or total <= 512:
        return None
    probe_off = xml_off if xml_off else (data_len + rsrc_len)
    probe = src.read(start + probe_off, 16)
    if not (probe.startswith(b"<?xml") or probe.startswith(b"bplist")):
        return None
    notes = [f"UDIF v{version}"]
    head = src.read(start + data_off, 4)
    if head[:2] in (b"H+", b"NX", b"PM", b"ER"):
        notes.append("内含 HFS/APFS 卷")
    elif head[:2] in (b"\x1f\x8b", b"BZ") or head[:1] == b"x":
        notes.append("数据区已压缩")
    return start, total, "；".join(notes)
