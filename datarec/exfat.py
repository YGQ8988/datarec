"""exFAT 解析：引导扇区、FAT、分配位图、目录项（含已删除项）。

为什么要自己解析而不用系统挂载：
1) 误拔之后，卷的 FAT 链和分配位图往往没刷盘，系统挂载会"能看到目录、点进去是空的"
   或者文件大小对不上。直接解析目录项能拿到原始 FirstCluster / DataLength，
   再用连续簇兜底，比系统挂载可靠得多。
2) 删除标记（目录项 in-use 位清零）只有原始解析才能看到。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .device import Source, SourceError

EXFAT_TAG = b"EXFAT   "
MAX_DIR_BYTES = 8 * 1024 * 1024      # 单个目录最多读 8MB（实际目录远小于此）
FAT_EOC = 0xFFFFFFF8


# --------------------------------------------------------------------------- 记录结构
@dataclass
class FileEntry:
    name: str = ""
    dir_path: str = "/"
    is_dir: bool = False
    deleted: bool = False
    first_cluster: int = 0
    size: int = 0                 # DataLength
    valid_size: int = 0           # ValidDataLength
    no_fat_chain: bool = False
    attributes: int = 0
    mtime: datetime | None = None
    ctime: datetime | None = None
    entry_offset: int = 0         # 卷内偏移（报告用）
    set_checksum_ok: bool = True
    secondaries: int = 0
    note: str = ""

    @property
    def path(self) -> str:
        if self.dir_path.endswith("/"):
            return self.dir_path + self.name
        return self.dir_path + "/" + self.name

    @property
    def clusters_needed(self) -> int:
        return 0


def _decode_ts(raw: int) -> datetime | None:
    """exFAT 32 位时间戳：1980 年起，秒精度 2 秒。"""
    if raw == 0:
        return None
    sec = (raw & 0x1F) * 2
    minute = (raw >> 5) & 0x3F
    hour = (raw >> 11) & 0x1F
    day = (raw >> 16) & 0x1F
    month = (raw >> 21) & 0x0F
    year = ((raw >> 25) & 0x7F) + 1980
    try:
        return datetime(year, month, day, hour, minute, min(sec, 59))
    except ValueError:
        return None


def _set_checksum_any(buf: bytes, entries: int) -> tuple[bool, bool]:
    """校验目录项集合。

    返回 (原样是否匹配, 按"删除前"形态是否匹配)。
    删除操作只把 in-use 位清掉（0x85→0x05、0xC0→0x40、0xC1→0x41），
    并不会重算校验和，所以已删除条目必须先把这些位置还原再算。
    """
    stored = struct.unpack_from("<H", buf, 2)[0]
    raw_ok = _set_checked(buf, entries, stored)
    norm = bytearray(buf)
    if norm[0] == 0x05:
        norm[0] = 0x85
    for k in range(1, entries):
        t = norm[k * 32]
        if not t & 0x80 and (t & 0x7F) in (0x40, 0x41):
            norm[k * 32] = t | 0x80
    norm_ok = _set_checked(bytes(norm), entries, stored)
    return raw_ok, norm_ok


def _set_checked(buf: bytes, entries: int, stored: int) -> bool:
    csum = 0
    for i in range(entries * 32):
        if i in (2, 3):
            continue
        csum = (((csum << 15) | (csum >> 1)) & 0xFFFF) + buf[i]
        csum &= 0xFFFF
    return csum == stored


def _looks_like_dir_cluster(data: bytes) -> bool:
    """一个簇是否真的装着目录项：必须在 32 字节对齐处存在校验和能对上的目录项集合。

    这个判断是防止"把相邻文件数据当目录项解析、凭空造出幽灵文件"的关键。
    """
    slots = [data[i:i + 32] for i in range(0, len(data) - 31, 32)]
    for i, raw in enumerate(slots):
        if raw[0] not in (0x85, 0x05):
            continue
        sec = raw[1]
        if not 2 <= sec <= 18 or i + 1 + sec > len(slots):
            continue
        if slots[i + 1][0] not in (0xC0, 0x40):
            continue
        raw_ok, norm_ok = _set_checksum_any(b"".join(slots[i:i + 1 + sec]), 1 + sec)
        if raw_ok or norm_ok:
            return True
    return False


# --------------------------------------------------------------------------- 卷定位
def locate_volume(src: Source) -> int:
    """返回 exFAT 引导扇区在数据源内的偏移（处理整盘镜像 / 分区镜像两种情况）。"""
    if _looks_like_boot(src, 0):
        return 0

    # MBR 分区表
    mbr = src.read(0, 512)
    if len(mbr) == 512 and mbr[510:512] == b"\x55\xaa":
        for i in range(4):
            off = 446 + i * 16
            ptype = mbr[off + 4]
            lba = struct.unpack_from("<I", mbr, off + 8)[0]
            if ptype in (0x07, 0x0B, 0x0C, 0xEE, 0x83) and lba:
                cand = lba * 512
                if _looks_like_boot(src, cand):
                    return cand
        # GPT
        hdr = src.read(512, 512)
        if hdr[:8] == b"EFI PART":
            ent_lba = struct.unpack_from("<Q", hdr, 72)[0]
            ent_num = struct.unpack_from("<I", hdr, 80)[0]
            ent_size = struct.unpack_from("<I", hdr, 84)[0]
            if 128 <= ent_size <= 4096 and 0 < ent_num <= 256:
                table = src.read(ent_lba * 512, ent_size * min(ent_num, 128))
                for i in range(min(ent_num, 128)):
                    base = i * ent_size
                    part = table[base:base + ent_size]
                    if len(part) < 128 or part[:16] == b"\x00" * 16:
                        continue
                    first = struct.unpack_from("<Q", part, 32)[0]
                    cand = first * 512
                    if cand and _looks_like_boot(src, cand):
                        return cand

    # 兜底：前 64 个扇区里找（某些镜像的引导扇区不在 0 也不在分区表里）
    for sector in range(1, 64):
        if _looks_like_boot(src, sector * 512):
            return sector * 512
    raise SourceError("这个数据源里没有找到 exFAT 引导扇区，可能不是 exFAT 卷，或卷头已损坏")


def _looks_like_boot(src: Source, offset: int) -> bool:
    try:
        head = src.read(offset, 512)
    except SourceError:
        return False
    if len(head) < 512 or head[3:11] != EXFAT_TAG:
        return False
    bps = head[108]
    spc = head[109]
    if not (9 <= bps <= 12) or spc > 25 - bps:
        return False
    cluster_count = struct.unpack_from("<I", head, 92)[0]
    return cluster_count > 0


# --------------------------------------------------------------------------- 卷对象
@dataclass
class VolumeInfo:
    offset: int
    volume_length_sectors: int
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    cluster_count: int
    fat_offset: int
    fat_length: int
    heap_offset: int
    root_cluster: int
    serial: int
    revision: str
    volume_flags: int
    num_fats: int
    percent_in_use: int

    @property
    def size_bytes(self) -> int:
        return self.volume_length_sectors * self.bytes_per_sector

    def summary(self) -> dict:
        return {
            "引导扇区偏移": self.offset,
            "卷大小": _human(self.size_bytes),
            "扇区大小": self.bytes_per_sector,
            "簇大小": _human(self.cluster_size),
            "簇总数": self.cluster_count,
            "FAT 位置": f"{self.fat_offset} (长度 {_human(self.fat_length)})",
            "数据区偏移": self.heap_offset,
            "根目录簇": self.root_cluster,
            "卷序列号": hex(self.serial),
            "版本": self.revision,
            "FAT 数": self.num_fats,
            "脏标志": "是（未干净卸载）" if self.volume_flags & 0x2 else "否",
            "位图记录占用率": f"{self.percent_in_use}%",
        }


def _human(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= step
    return f"{val:.1f} TB"


class ExFatVolume:
    def __init__(self, src: Source, info: VolumeInfo):
        self.src = src
        self.info = info
        self.cs = info.cluster_size
        self._fat_cache: dict[int, bytes] = {}
        self._bitmap: bytes | None = None
        self._bitmap_cluster: int | None = None
        self._fat_whole: bytes | None = None

    # ---------------------------------------------------------------- 构造
    @classmethod
    def open(cls, src: Source, offset: int | None = None) -> "ExFatVolume":
        if offset is None:
            offset = locate_volume(src)
        boot = src.read(offset, 512)
        if len(boot) < 512 or boot[3:11] != EXFAT_TAG:
            raise SourceError("引导扇区不合法：找不到 'EXFAT   ' 标记")
        bps = 1 << boot[108]
        spc = 1 << boot[109]
        rev = boot[104] | (boot[105] << 8)
        info = VolumeInfo(
            offset=offset,
            volume_length_sectors=struct.unpack_from("<Q", boot, 72)[0],
            bytes_per_sector=bps,
            sectors_per_cluster=spc,
            cluster_size=bps * spc,
            cluster_count=struct.unpack_from("<I", boot, 92)[0],
            fat_offset=struct.unpack_from("<I", boot, 80)[0] * bps,
            fat_length=struct.unpack_from("<I", boot, 84)[0] * bps,
            heap_offset=struct.unpack_from("<I", boot, 88)[0] * bps,
            root_cluster=struct.unpack_from("<I", boot, 96)[0],
            serial=struct.unpack_from("<I", boot, 100)[0],
            revision=f"{rev >> 8}.{rev & 0xFF}",
            volume_flags=struct.unpack_from("<H", boot, 106)[0],
            num_fats=boot[110],
            percent_in_use=boot[112],
        )
        if info.cluster_count <= 0 or info.cluster_size <= 0:
            raise SourceError("引导扇区里的簇信息不合法，卷头可能已损坏")
        return cls(src, info)

    # ---------------------------------------------------------------- 读
    def read(self, offset: int, length: int) -> bytes:
        return self.src.read(self.info.offset + offset, length)

    def cluster_offset(self, cluster: int) -> int:
        return self.info.heap_offset + (cluster - 2) * self.cs

    def valid_cluster(self, cluster: int) -> bool:
        return 2 <= cluster < self.info.cluster_count + 2

    def read_cluster(self, cluster: int) -> bytes:
        if not self.valid_cluster(cluster):
            return b""
        return self.read(self.cluster_offset(cluster), self.cs)

    # ---------------------------------------------------------------- FAT
    def _load_fat(self) -> bytes:
        if self._fat_whole is None:
            size = self.info.fat_length
            # 大卷的 FAT 可能上百 MB，只按需分页
            if size <= 32 * 1024 * 1024:
                self._fat_whole = self.read(self.info.fat_offset, size)
        return self._fat_whole or b""

    def fat_entry(self, cluster: int) -> int | None:
        """返回簇的 FAT 值；读不到返回 None。"""
        if not self.valid_cluster(cluster):
            return None
        index = cluster - 2
        whole = self._load_fat()
        if whole:
            pos = index * 4
            if pos + 4 > len(whole):
                return None
            return struct.unpack_from("<I", whole, pos)[0]
        page_size = 2 * 1024 * 1024
        page = index * 4 // page_size
        if page not in self._fat_cache and len(self._fat_cache) < 8:
            self._fat_cache[page] = self.read(self.info.fat_offset + page * page_size, page_size)
        buf = self._fat_cache.get(page, b"")
        pos = index * 4 - page * page_size
        if pos + 4 > len(buf):
            return None
        return struct.unpack_from("<I", buf, pos)[0]

    def follow_chain(self, first: int, limit: int) -> tuple[list[int], str | None]:
        """沿 FAT 链走，返回 (簇列表, 异常说明)。

        误拔场景下 FAT 经常是空/半截的，这里只负责如实报告，
        是否改用连续簇兜底由上层决定。
        """
        chain: list[int] = []
        seen: set[int] = set()
        cur = first
        problem = None
        while len(chain) < limit:
            if not self.valid_cluster(cur):
                problem = f"链在第 {len(chain)} 个簇处指向非法簇号 {cur}"
                break
            if cur in seen:
                problem = f"FAT 链在簇 {cur} 处成环"
                break
            seen.add(cur)
            chain.append(cur)
            nxt = self.fat_entry(cur)
            if nxt is None:
                problem = f"FAT 表读不到簇 {cur} 的表项"
                break
            if nxt >= FAT_EOC:
                if len(chain) < limit:
                    problem = f"FAT 链提前结束（{len(chain)}/{limit} 簇）"
                break
            if nxt == 0:
                problem = f"簇 {cur} 的 FAT 表项是 0（典型未刷盘痕迹）"
                break
            cur = nxt
        return chain, problem

    # ---------------------------------------------------------------- 位图
    def bitmap(self) -> bytes:
        """分配位图：用来判断"这个簇现在是否被别人占着"。"""
        if self._bitmap is not None:
            return self._bitmap
        self._bitmap = b""
        root = self.read_directory(self._root_spec())
        for entry in root:
            # 0x81 = Allocation Bitmap 主项
            if entry[0] == 0x81:
                first = struct.unpack_from("<I", entry, 20)[0]
                length = struct.unpack_from("<Q", entry, 24)[0]
                clusters = (length + self.cs - 1) // self.cs
                chunks = []
                for c in range(first, first + clusters):
                    chunks.append(self.read_cluster(c))
                self._bitmap = b"".join(chunks)
                self._bitmap_cluster = first
                break
        return self._bitmap

    def cluster_allocated(self, cluster: int) -> bool | None:
        bm = self.bitmap()
        if not bm:
            return None
        index = cluster - 2
        byte = index // 8
        if byte >= len(bm):
            return None
        return bool(bm[byte] >> (index % 8) & 1)

    # ---------------------------------------------------------------- 目录
    def _root_spec(self) -> "FileEntry":
        return FileEntry(name="/", dir_path="/", is_dir=True,
                         first_cluster=self.info.root_cluster,
                         size=MAX_DIR_BYTES, no_fat_chain=False)

    def read_directory(self, entry: FileEntry, entries_out: list[bytes] | None = None) -> list[bytes]:
        """把目录的 32 字节槽读成一个列表。

        策略：优先跟 FAT 链；链不给力就按"连续簇"往后兜底（exFAT 目录绝大多数是连续的）。
        关键是要知道**什么时候停**：每往后读一个簇就先判断它"像不像目录簇"
        （32 字节对齐位置上存在校验和能对上的目录项集合），不像就停。
        否则会把相邻文件的数据当成目录项，凭空造出一堆幽灵文件。
        """
        if not self.valid_cluster(entry.first_cluster):
            return []
        cs = self.cs
        want = entry.size if 0 < entry.size < MAX_DIR_BYTES else MAX_DIR_BYTES
        max_clusters = max(1, min(want // cs + 2, MAX_DIR_BYTES // cs))
        slots: list[bytes] = []
        cluster = entry.first_cluster
        seen: set[int] = set()
        index = 0
        while index < max_clusters and self.valid_cluster(cluster) and cluster not in seen:
            data = self.read_cluster(cluster)
            if not data:
                break
            if index > 0 and not _looks_like_dir_cluster(data):
                break
            seen.add(cluster)
            slots.extend(data[i:i + 32] for i in range(0, len(data) - 31, 32))
            index += 1
            nxt = self.fat_entry(cluster)
            if nxt is not None and self.valid_cluster(nxt) and nxt not in seen:
                cluster = nxt
            else:
                cluster = entry.first_cluster + index  # 连续兜底
        return slots

    def iter_dir_entries(self, entry: FileEntry, dir_path: str,
                         include_deleted: bool = True,
                         scan_past_end: bool = True) -> list[FileEntry]:
        """解析目录项集合，返回文件/子目录列表（活动项 + 已删除项）。"""
        slots = self.read_directory(entry)
        out: list[FileEntry] = []
        base_offset = self.cluster_offset(entry.first_cluster)
        i = 0
        total = len(slots)
        while i < total:
            raw = slots[i]
            etype = raw[0]
            if etype == 0x00:
                if scan_past_end:
                    i += 1
                    continue
                break
            if etype & 0x7F != 0x05:
                # 非文件主项：位图(0x81)/upcase(0x82) 的 SecondaryCount 在偏移 1；
                # 卷标(0x83) 的偏移 1 是字符数，不带从属项，必须只跳 1 个槽，
                # 否则会把紧跟其后的真实目录项整段吞掉（踩过的坑）。
                if etype in (0x81, 0x82, 0xE0, 0xE1):
                    i += 1 + raw[1]
                else:
                    i += 1
                continue
            entry, consumed = self._parse_set(slots, i, dir_path, base_offset)
            if entry is not None and (include_deleted or not entry.deleted):
                out.append(entry)
            i += max(1, consumed)
        return out

    def _parse_set(self, slots: list[bytes], i: int, dir_path: str,
                   base_offset: int) -> tuple[FileEntry | None, int]:
        raw = slots[i]
        etype = raw[0]
        deleted = etype == 0x05
        sec_count = raw[1]
        if not 2 <= sec_count <= 18:
            return None, 1
        if i + 1 + sec_count > len(slots):
            return None, 1

        stream = slots[i + 1]
        if stream[0] not in (0xC0, 0x40):        # 0xC0=Stream(在用) / 0x40=已删除
            return None, 1

        set_bytes = b"".join(slots[i:i + 1 + sec_count])
        raw_ok, norm_ok = _set_checksum_any(set_bytes, 1 + sec_count)
        checksum_ok = raw_ok or norm_ok

        flags = stream[1]
        name_len = stream[3]
        first_cluster = struct.unpack_from("<I", stream, 20)[0]
        data_len = struct.unpack_from("<Q", stream, 24)[0]
        valid_len = struct.unpack_from("<Q", stream, 8)[0]

        # 文件名：每个 0xC1/0x41 项带 15 个 UTF-16 字符（跳过 2 字节项头）
        chars: list[str] = []
        got_name_entries = 0
        for k in range(2, 1 + sec_count):
            nm = slots[i + k]
            if nm[0] in (0xC1, 0x41):
                got_name_entries += 1
                chars.append(nm[2:32].decode("utf-16-le", "replace"))
        name = "".join(chars)
        name = name[:name_len].rstrip("\x00").rstrip()

        note = ""
        if not checksum_ok:
            note = "目录项校验和不符（该条目可能已被部分覆盖）"
        if not name:
            name = f"未命名_簇{first_cluster}"
            note = (note + "；" if note else "") + "文件名项已损坏，按簇号命名"

        attributes = struct.unpack_from("<H", raw, 4)[0]
        entry = FileEntry(
            name=name,
            dir_path=dir_path,
            is_dir=bool(attributes & 0x10),
            deleted=deleted,
            first_cluster=first_cluster,
            size=data_len,
            valid_size=valid_len,
            no_fat_chain=bool(flags & 0x02),
            attributes=attributes,
            mtime=_decode_ts(struct.unpack_from("<I", raw, 10)[0]),
            ctime=_decode_ts(struct.unpack_from("<I", raw, 8)[0]),
            entry_offset=base_offset + i * 32,
            set_checksum_ok=checksum_ok,
            secondaries=sec_count,
            note=note,
        )
        return entry, 1 + sec_count

    # ---------------------------------------------------------------- 遍历
    def walk(self, include_deleted: bool = True, max_depth: int = 32,
             skip_system: bool = True,
             should_stop: Callable[[], bool] | None = None
             ) -> tuple[list[FileEntry], list[FileEntry], list[str]]:
        """遍历整棵树。返回 (文件列表, 目录列表, 告警列表)。

        should_stop 返回 True 时提前收工，返回已经扫到的部分并附一条告警。
        大目录树上中断要能用，所以检查点放在每个目录出栈处。
        """
        files: list[FileEntry] = []
        dirs: list[FileEntry] = []
        warnings: list[str] = []
        seen: set[int] = set()
        stack: list[tuple[FileEntry, str, int]] = [(self._root_spec(), "/", 0)]
        while stack:
            if should_stop is not None:
                try:
                    if should_stop():
                        warnings.append(f"扫描被中断，结果不完整（已扫 {len(files)} 个文件）")
                        break
                except Exception:
                    pass
            d_entry, path, depth = stack.pop()
            if depth > max_depth:
                warnings.append(f"目录层级超过 {max_depth}，跳过 {path}")
                continue
            try:
                entries = self.iter_dir_entries(d_entry, path, include_deleted=include_deleted)
            except SourceError as exc:
                warnings.append(f"读取目录 {path} 失败：{exc}")
                continue
            if d_entry.first_cluster != self.info.root_cluster:
                if d_entry.first_cluster in seen and path != "/":
                    warnings.append(f"目录 {path} 指向已遍历过的簇，跳过（防环）")
                    continue
                seen.add(d_entry.first_cluster)
            for ent in entries:
                if skip_system and ent.name.startswith("._") and not ent.note:
                    # AppleDouble 资源叉：留在列表里备查，但不参与"主文件缺失"判断
                    ent.note = "AppleDouble 资源叉（macOS 元数据）"
                if ent.is_dir:
                    if ent.name in (".", ".."):
                        continue
                    dirs.append(ent)
                    sub = path.rstrip("/") + "/" + ent.name
                    stack.append((ent, sub, depth + 1))
                else:
                    files.append(ent)
        return files, dirs, warnings

    # ---------------------------------------------------------------- 深度扫描
    def scan_all_dir_slots(self, limit_clusters: int | None = None) -> list[FileEntry]:
        """整卷扫描目录项：目录链断了、父目录丢了的时候，靠这个把条目捞回来。

        只看"长得像文件主项 + 流项 + 名字项且校验和能对上"的 32 字节对齐位置，
        所以误报率很低。
        """
        cs = self.cs
        per = cs // 32
        results: list[FileEntry] = []
        seen: set[tuple] = set()
        total = self.info.cluster_count
        if limit_clusters:
            total = min(total, limit_clusters)
        for idx in range(total):
            cluster = idx + 2
            try:
                data = self.read_cluster(cluster)
            except SourceError:
                continue
            if b"\x85" not in data and b"\x05" not in data:
                continue
            slots = [data[i:i + 32] for i in range(0, len(data) - 31, 32)]
            for i, raw in enumerate(slots):
                if raw[0] not in (0x85, 0x05):
                    continue
                if not 2 <= raw[1] <= 18:
                    continue
                ent, consumed = self._parse_set(slots, i, "/", 0)
                if ent is None or not ent.set_checksum_ok:
                    continue
                if ent.is_dir or ent.size == 0 or not self.valid_cluster(ent.first_cluster):
                    continue
                key = (ent.name, ent.first_cluster, ent.size)
                if key in seen:
                    continue
                seen.add(key)
                ent.note = (ent.note + "；" if ent.note else "") + "整卷扫描发现（原目录可能已丢失）"
                results.append(ent)
        return results
