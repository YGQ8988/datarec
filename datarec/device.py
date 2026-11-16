r"""只读数据源。

不管是整块磁盘设备（/dev/rdisk4s1、\\.\PhysicalDrive1）、整盘镜像（.img/.dd）
还是分段镜像，统一用 pread 读。关键纪律：**只以只读方式打开**，并在打开后复核
文件描述符标志，确保任何代码路径都不会写回源盘（写源盘等于把要救的数据再毁一次）。

平台差异全部交给 oslayer：Windows 没有 os.pread 和 fcntl，设备路径也不能过 abspath。
"""
from __future__ import annotations

import errno
import os

from . import oslayer

MiB = 1024 * 1024


class SourceError(RuntimeError):
    pass


class Source:
    def __init__(self, path: str, offset: int = 0):
        self.raw_path = path
        self.path = oslayer.normalize(path)
        self.is_device = oslayer.is_device_path(self.path)

        try:
            self.fd, regular = oslayer.open_readonly(self.path)
        except oslayer.OpenError as exc:
            raise SourceError(exc.detail) from exc
        except OSError as exc:
            raise SourceError(f"打不开 {self.path}：{exc.strerror or exc}") from exc

        self.is_regular = regular

        if not oslayer.verify_readonly(self.fd):        # 理论不可能，保险
            self.close()
            raise SourceError("内部错误：数据源不是以只读方式打开的，已中止")

        self.offset = offset
        self.device_size = self._probe_size()
        self.bytes_read = 0

    # ------------------------------------------------------------------ 基础
    def _probe_size(self) -> int | None:
        if self.is_regular:
            try:
                st = os.stat(self.path)
                return max(0, st.st_size - self.offset)
            except OSError:
                pass
        size = oslayer.file_size(self.fd)
        if size is None:
            return None
        return max(0, size - self.offset)

    def read(self, offset: int, length: int) -> bytes:
        """从卷内 offset 读 length 字节；短读自动补齐，读到 EOF 就返回短的。"""
        if length <= 0:
            return b""
        buf = bytearray()
        pos = offset
        while len(buf) < length:
            want = length - len(buf)
            try:
                chunk = oslayer.pread(self.fd, min(want, 8 * MiB), self.offset + pos)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.ENXIO, errno.EBADF):
                    raise SourceError(
                        f"读取偏移 {pos} 时磁盘报 I/O 错误（坏道）。"
                        "建议先做整盘镜像再做恢复，别继续直接读原盘") from exc
                raise
            if not chunk:
                break
            buf += chunk
            pos += len(chunk)
            self.bytes_read += len(chunk)
        return bytes(buf)

    def read_safe(self, offset: int, length: int) -> tuple[bytes, str | None]:
        """容错读：出错时返回已读到的部分 + 错误说明，用于坏道盘的尽力恢复。"""
        try:
            return self.read(offset, length), None
        except SourceError as exc:
            return b"", str(exc)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ 提示
    def mount_points(self) -> list[str]:
        return oslayer.mount_points_for(self.path) if self.is_device else []


def mount_points_for(path: str) -> list[str]:
    """查这个设备当前挂在哪些挂载点上（不需要先打开设备）。保留旧接口名。"""
    return oslayer.mount_points_for(path)


class Window:
    """顺扫窗口：按需向后扩大缓冲区，供魔数/结构扫描使用。

    只在"从起点顺序往后找结尾"的格式里用（jpeg/png/gif/pdf/zip…），
    不会把整个 4GB 的 mdat 读进内存。
    """

    def __init__(self, src: Source, start: int, limit: int, initial: int = 1 * MiB):
        self.src = src
        self.start = start
        self.limit = max(0, limit)
        self.buf = bytearray()
        self._read(min(initial, self.limit))

    def _read(self, want_len: int) -> None:
        want_len = min(want_len, self.limit)
        if want_len <= len(self.buf):
            return
        chunk = self.src.read(self.start + len(self.buf), want_len - len(self.buf))
        self.buf += chunk
        if len(self.buf) < want_len and not chunk:
            self.limit = len(self.buf)

    def ensure(self, n: int) -> bool:
        """保证缓冲区至少有 n 字节；不够则扩容。返回是否满足。"""
        if n <= len(self.buf):
            return True
        if n > self.limit:
            self._read(self.limit)
            return len(self.buf) >= n if n <= len(self.buf) else False
        # 逐步扩容，避免小文件也读成大块
        target = min(self.limit, max(n, len(self.buf) * 4, 64 * 1024))
        self._read(target)
        return len(self.buf) >= n

    def find(self, needle: bytes, start: int = 0) -> int:
        """在窗口内查找 needle，必要时扩大缓冲；返回相对 start 的偏移，找不到 -1。"""
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

    def find_all(self, needle: bytes, start: int = 0, max_hits: int = 64) -> list[int]:
        hits, pos = [], start
        while len(hits) < max_hits:
            idx = self.find(needle, pos)
            if idx < 0:
                break
            hits.append(idx)
            pos = idx + 1
        return hits
