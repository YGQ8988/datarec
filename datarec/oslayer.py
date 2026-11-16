"""跨平台只读设备访问层（macOS / Windows / Linux）。

这一层只做四件事，把平台差异挡在下面，不让它渗进解析和恢复逻辑：

1) 把"数据源"（镜像文件 / 物理磁盘 / 分区）以**只读**方式打开；
2) 提供与平台无关的 pread —— Windows 没有 os.pread，用 seek+read 加锁代替；
3) 取设备容量 —— Windows 走 DeviceIoControl，POSIX 先试 lseek；
4) 枚举可选磁盘并报告哪些卷正被系统挂载（挂载中读取，系统缓存可能把元数据写回盘）。

Windows 上还有一条额外纪律：CreateFileW 只申请 GENERIC_READ，
从 API 层面就不可能写坏源盘，而不是靠"我小心一点"。
"""
from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import threading

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_PREAD_LOCK = threading.Lock()
_HAS_PREAD = hasattr(os, "pread")

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    try:                       # POSIX 才有的 fd 标志复核
        import fcntl as _fcntl
    except ImportError:        # pragma: no cover
        _fcntl = None


# --------------------------------------------------------------------------- 路径
def is_device_path(path: str) -> bool:
    """是不是"设备"而不是普通文件。"""
    if IS_WINDOWS:
        p = path.replace("/", "\\")
        if p.startswith("\\\\") or p.startswith("\\??\\"):
            return True
        return len(p) >= 2 and p[1] == ":" and p[2:].strip("\\") == ""
    return path.startswith("/dev/")


def normalize(path: str) -> str:
    """展开 ~ 和相对路径。设备路径绝不能过 abspath —— 那会把 \\\\.\\PhysicalDrive0 拼坏。"""
    if is_device_path(path):
        return path
    return os.path.abspath(os.path.expanduser(path))


def device_label(path: str) -> str:
    """给人看的短名字。"""
    if IS_WINDOWS:
        return path.rstrip("\\").replace("\\\\.\\", "")
    return os.path.basename(path)


# --------------------------------------------------------------------------- 打开
class OpenError(OSError):
    """带"下一步该做什么"的打开失败。"""

    def __init__(self, message: str, *, err: int | None = None):
        super().__init__(message)
        self.detail = message
        self.err = err


def open_readonly(path: str) -> tuple[int, bool]:
    """以只读方式打开，返回 (fd, 是不是普通文件)。

    普通文件失败抛原样 OSError；设备失败抛 OpenError（含处置建议）。
    """
    if IS_WINDOWS:
        return _win_open(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        if is_device_path(path):
            raise OpenError(_permission_hint(path, exc), err=exc.errno) from exc
        raise
    if stat.S_ISDIR(st.st_mode):
        raise OpenError(f"{path} 是目录。请选择磁盘设备或镜像文件", err=errno.EISDIR)
    regular = stat.S_ISREG(st.st_mode)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if is_device_path(path) or exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY):
            raise OpenError(_permission_hint(path, exc), err=exc.errno) from exc
        raise
    return fd, regular


def verify_readonly(fd: int) -> bool:
    """复核 fd 确实不是可写的。Windows 侧由 GENERIC_READ 保证，恒真。"""
    if IS_WINDOWS or _fcntl is None:
        return True
    try:
        flags = _fcntl.fcntl(fd, _fcntl.F_GETFL)
    except OSError:
        return True
    return not (flags & os.O_RDWR) and not (flags & os.O_WRONLY)


def pread(fd: int, length: int, offset: int) -> bytes:
    """平台无关的 pread。Windows 用 seek+read，加锁保证不与其他读互踩。"""
    if _HAS_PREAD:
        return os.pread(fd, length, offset)
    with _PREAD_LOCK:                       # pragma: no cover - 仅 Windows 走这里
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)


def file_size(fd: int) -> int | None:
    """设备/文件容量。拿不到返回 None，由上层用引导扇区推算。"""
    if IS_WINDOWS:
        return _win_size(fd)
    try:
        end = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)
        return end if end > 0 else None
    except OSError:
        return None


# --------------------------------------------------------------------------- 权限
def _permission_hint(path: str, exc: OSError) -> str:
    """把"打不开"翻译成用户能照做的动作，且分平台。"""
    name = device_label(path)
    base = f"打不开 {path}：{exc.strerror or exc}"
    if exc.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY, errno.EIO):
        return base
    if IS_WINDOWS:
        return "\n".join([
            base,
            "Windows 读物理磁盘需要管理员权限，而且盘不能被独占占用：",
            "  1) 右键程序 → 以管理员身份运行；",
            "  2) 关掉可能占用它的程序（资源管理器窗口、杀毒、同步盘）；",
            "  3) 若是移动硬盘，先安全弹出再重新插上，别让系统持续读写它。",
        ])
    lines = [base]
    mnts = mount_points_for(path)
    if mnts:
        lines.append(f"这个卷还挂载在 {', '.join(mnts)}，系统不允许读原始设备。先卸载：")
        lines.append(f"    diskutil unmountDisk /dev/{name.lstrip('r')}")
    elif os.geteuid() != 0:
        lines.append("读原始磁盘设备需要管理员权限，请用 sudo 运行，例如：")
        lines.append(f"    sudo python3 datarec.py info -s {path}")
    else:
        lines.append("确认设备名是否正确（diskutil list 可列出所有磁盘）")
    return "\n".join(lines)


def is_admin() -> bool:
    if IS_WINDOWS:
        return _win_is_admin()
    return hasattr(os, "geteuid") and os.geteuid() == 0


def relaunch_as_admin(argv: list[str]) -> bool:
    """Windows：弹 UAC 以管理员身份重启自己。macOS 返回 False（由用户 sudo）。"""
    if not IS_WINDOWS:
        return False
    try:
        h = _shell32.ShellExecuteW(None, "runas", argv[0],
                                   subprocess.list2cmdline(argv[1:]), None, 1)
        return bool(h) and int(h) > 32
    except Exception:
        return False


# --------------------------------------------------------------------------- 挂载
def mount_points_for(path: str) -> list[str]:
    """这个设备当前挂在哪些挂载点/盘符。不需要先打开设备。"""
    if IS_WINDOWS:
        disk = _win_disk_number(path)
        if disk is None:
            return []
        return [v["root"] for v in _win_volumes() if v.get("disk") == disk]
    if IS_MACOS:
        return _posix_mounts(path, use_mount_cmd=True)
    return _posix_mounts(path, use_mount_cmd=False)


def _posix_mounts(path: str, *, use_mount_cmd: bool) -> list[str]:
    targets = [path, os.path.realpath(path)]
    base = os.path.basename(path)
    if base.startswith("rdisk"):
        targets.append("/dev/" + base[1:])
    elif base.startswith("disk"):
        targets.append("/dev/r" + base)
    hits: list[str] = []
    if not use_mount_cmd:
        try:
            with open("/proc/mounts", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in targets:
                        hits.append(parts[1])
            if hits:
                return hits
        except OSError:
            pass
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return hits
    for line in out.splitlines():
        if " on " not in line:
            continue
        dev, rest = line.split(" on ", 1)
        mnt = rest.split(" (")[0]
        # 整盘 /dev/disk4 也要能匹配到它的分区 /dev/disk4s1
        if dev in targets or any(dev == t or dev.startswith(t + "s")
                                 or dev.startswith(t + "p") for t in targets):
            hits.append(mnt)
    return hits


# --------------------------------------------------------------------------- 枚举
def list_sources(include_all: bool = False) -> list[dict]:
    """列出可扫描的磁盘与分区，**外接/可移动的排在前面**（那是用户要找的盘）。

    每项：path（可直接传给 -s）、title（给人看）、size、fs、
    kind（disk/part）、parent、mounted（挂载点列表）、detail、internal。
    """
    if IS_WINDOWS:
        return _win_list(include_all)
    if IS_MACOS:
        return _mac_list(include_all)
    return _linux_list(include_all)


def human_size(n: int | None) -> str:
    if not n:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} TB"


# =========================================================================== Windows
if IS_WINDOWS:  # pragma: no cover - 仅在 Windows 上编译执行
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    _IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
    _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    # 每个 W 版 API 都必须声明 argtypes：不声明时 ctypes 会把 str 当 char* 传，参数就废了
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    _k32.GetLogicalDriveStringsW.restype = wintypes.DWORD
    _k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    _k32.GetVolumeInformationW.restype = wintypes.BOOL
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT
    _shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    _shell32.ShellExecuteW.restype = ctypes.c_void_p

    class _LENGTH_INFO(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_longlong)]

    class _DISK_EXTENT(ctypes.Structure):
        _fields_ = [("DiskNumber", wintypes.DWORD),
                    ("StartingOffset", ctypes.c_longlong),
                    ("ExtentLength", ctypes.c_longlong)]

    class _VOLUME_DISK_EXTENTS(ctypes.Structure):
        _fields_ = [("NumberOfDiskExtents", wintypes.DWORD),
                    ("Extents", _DISK_EXTENT * 16)]

    _WIN_ERRNO = {2: errno.ENOENT, 3: errno.ENOENT, 5: errno.EACCES,
                  19: errno.EROFS, 21: errno.EBUSY, 32: errno.EBUSY,
                  33: errno.EBUSY, 55: errno.EBUSY, 87: errno.EINVAL}

    def _raise_win(err: int, path: str) -> None:
        raise OSError(_WIN_ERRNO.get(err, errno.EIO),
                      ctypes.FormatError(err).strip() or f"错误码 {err}", path)

    def _win_open_raw(path: str, read: bool = True) -> int:
        h = _k32.CreateFileW(path, _GENERIC_READ if read else 0,
                             _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                             _OPEN_EXISTING, 0, None)
        if not h or h == _INVALID_HANDLE:
            _raise_win(ctypes.get_last_error(), path)
        return int(h)

    def _win_open(path: str) -> tuple[int, bool]:
        regular = os.path.isfile(path)
        try:
            h = _win_open_raw(path, read=True)
        except OSError as exc:
            raise OpenError(_permission_hint(path, exc), err=exc.errno) from exc
        try:
            fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
        except OSError:
            _k32.CloseHandle(wintypes.HANDLE(h))
            raise
        return fd, regular

    def _win_size(fd: int) -> int | None:
        h = msvcrt.get_osfhandle(fd)
        info = _LENGTH_INFO()
        ret = wintypes.DWORD(0)
        if _k32.DeviceIoControl(h, _IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                ctypes.byref(info), ctypes.sizeof(info),
                                ctypes.byref(ret), None):
            return int(info.Length)
        buf = ctypes.create_string_buffer(256)
        if _k32.DeviceIoControl(h, _IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 0,
                                buf, 256, ctypes.byref(ret), None) and ret.value >= 32:
            # DISK_GEOMETRY 占 24 字节，紧跟着就是 DiskSize(LARGE_INTEGER)
            return int(ctypes.c_longlong.from_buffer(buf, 24).value)
        return None

    def _win_disk_number(path: str) -> int | None:
        """这个卷/磁盘属于第几号物理盘（用来判断"同一块盘上还有别的分区挂着"）。"""
        low = path.lower().replace("/", "\\")
        if "physicaldrive" in low:
            try:
                return int(low.rstrip("\\").split("physicaldrive")[1])
            except (IndexError, ValueError):
                return None
        target = path if path.startswith("\\\\") else "\\\\.\\" + path.rstrip("\\")
        try:
            h = _win_open_raw(target, read=False)
        except OSError:
            return None
        try:
            ext = _VOLUME_DISK_EXTENTS()
            ret = wintypes.DWORD(0)
            ok = _k32.DeviceIoControl(h, _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 0,
                                      ctypes.byref(ext), ctypes.sizeof(ext),
                                      ctypes.byref(ret), None)
            if ok and ext.NumberOfDiskExtents >= 1:
                return int(ext.Extents[0].DiskNumber)
        finally:
            _k32.CloseHandle(wintypes.HANDLE(h))
        return None

    def _win_volumes() -> list[dict]:
        n = _k32.GetLogicalDriveStringsW(0, None)
        if not n:
            return []
        buf = ctypes.create_unicode_buffer(n + 1)
        _k32.GetLogicalDriveStringsW(n + 1, buf)
        out = []
        for root in (s for s in buf[:].split("\x00") if s.strip()):
            name_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(261)
            serial, maxlen, flags = (wintypes.DWORD(0) for _ in range(3))
            ok = _k32.GetVolumeInformationW(root, name_buf, 261, ctypes.byref(serial),
                                            ctypes.byref(maxlen), ctypes.byref(flags),
                                            fs_buf, 261)
            size = None
            try:
                fd, _ = _win_open(root)
                try:
                    size = _win_size(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            out.append({
                "root": root,
                "label": name_buf.value if ok else "",
                "fs": fs_buf.value if ok else "",
                "size": size,
                "drive_type": int(_k32.GetDriveTypeW(root)),
                "disk": _win_disk_number(root),
            })
        return out

    def _win_is_admin() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _win_list(include_all: bool = False) -> list[dict]:
        by_disk: dict[int, list[dict]] = {}
        for v in _win_volumes():
            if v["disk"] is None:
                continue
            if v["drive_type"] not in (2, 3) and not include_all:
                continue                       # 跳过光驱 / 网络盘 / RAM 盘
            by_disk.setdefault(v["disk"], []).append(v)

        items: list[dict] = []
        for disk in sorted(by_disk):
            dev = "\\\\.\\PhysicalDrive" + str(disk)
            size = None
            try:
                h = _win_open_raw(dev, read=False)
                try:
                    fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
                    try:
                        size = _win_size(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    _k32.CloseHandle(wintypes.HANDLE(h))
            except OSError:
                continue                       # 这一号盘不存在
            vs = by_disk[disk]
            removable = any(v["drive_type"] == 2 for v in vs)
            fss = "/".join(sorted({v["fs"] for v in vs if v["fs"]}))
            labels = ", ".join(v["label"] or v["root"].rstrip("\\") for v in vs)
            items.append({
                "path": dev,
                "title": f"磁盘 {disk} · {human_size(size)} · {fss or '未知'}",
                "size": size,
                "fs": fss,
                "kind": "disk",
                "parent": None,
                "mounted": [v["root"] for v in vs],
                "detail": ("可移动磁盘" if removable else "内置磁盘") + f"，{labels}",
                "internal": not removable,
            })
            for v in sorted(vs, key=lambda x: x["root"]):
                letter = v["root"].rstrip("\\")
                items.append({
                    "path": "\\\\.\\" + letter,
                    "title": f"  └ 分区 {letter} · {human_size(v['size'])} · {v['fs'] or '未知'}",
                    "size": v["size"],
                    "fs": v["fs"],
                    "kind": "part",
                    "parent": dev,
                    "mounted": [v["root"]],
                    "detail": v["label"],
                    "internal": not removable,
                })
        return _ext_first(items)

# =========================================================================== macOS
elif IS_MACOS:
    import plistlib

    def _mac_info(dev_id: str) -> dict:
        try:
            raw = subprocess.run(["diskutil", "info", "-plist", dev_id],
                                 capture_output=True, timeout=10).stdout
            return plistlib.loads(raw)
        except Exception:
            return {}

    def _mac_list(include_all: bool = False) -> list[dict]:
        try:
            raw = subprocess.run(["diskutil", "list", "-plist"],
                                 capture_output=True, timeout=20).stdout
            data = plistlib.loads(raw)
        except Exception:
            return []

        items: list[dict] = []
        for entry in data.get("AllDisksAndPartitions", []):
            dev_id = entry.get("DeviceIdentifier", "")
            if not dev_id:
                continue
            meta = _mac_info(dev_id)
            internal = bool(meta.get("Internal", False))
            if internal and not include_all:
                continue                       # 默认不列本机系统盘，避免误选
            content = entry.get("Content", "")
            total = entry.get("Size")
            whole_label = "外接" if not internal else "内置"

            parts = entry.get("Partitions") or []
            items.append({
                "path": f"/dev/r{dev_id}",
                "title": f"整盘 {dev_id} · {human_size(total)} · 整盘扫描",
                "size": total,
                "fs": content,
                "kind": "disk",
                "parent": None,
                "mounted": [],
                "detail": f"{whole_label}磁盘，按分区表自动定位 exFAT 卷",
                "internal": internal,
            })
            for p in parts:
                pid = p.get("DeviceIdentifier", "")
                pinfo = _mac_info(pid) if parts else {}
                fs = (pinfo.get("FilesystemName") or pinfo.get("FilesystemType")
                      or p.get("Content", ""))
                mnt = pinfo.get("MountPoint") or ""
                items.append({
                    "path": f"/dev/r{pid}",
                    "title": f"  └ 分区 {pid} · {human_size(p.get('Size'))} · {fs or '未知'}",
                    "size": p.get("Size"),
                    "fs": fs,
                    "kind": "part",
                    "parent": f"/dev/r{dev_id}",
                    "mounted": [mnt] if mnt else [],
                    "detail": p.get("VolumeName", ""),
                    "internal": internal,
                })
            if not parts:
                vols = entry.get("APFSVolumes") or []
                items.append({
                    "path": f"/dev/r{dev_id}",
                    "title": f"  └ 卷组 {dev_id} · {human_size(total)} · {content or '未知'}",
                    "size": total,
                    "fs": content,
                    "kind": "part",
                    "parent": f"/dev/r{dev_id}",
                    "mounted": [v.get("MountPoint", "") for v in vols if v.get("MountPoint")],
                    "detail": ", ".join(v.get("VolumeName", "") for v in vols if v.get("VolumeName")),
                    "internal": internal,
                })
        return _ext_first(items)

# =========================================================================== Linux
else:
    def _linux_list(include_all: bool = False) -> list[dict]:
        items: list[dict] = []
        base = "/sys/block"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return items
        for name in names:
            if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
                continue
            try:
                with open(f"{base}/{name}/size") as fh:
                    size = int(fh.read().strip()) * 512
                with open(f"{base}/{name}/removable") as fh:
                    removable = fh.read().strip() == "1"
            except OSError:
                continue
            if not removable and not include_all:
                continue
            dev = f"/dev/{name}"
            items.append({
                "path": dev,
                "title": f"整盘 {name} · {human_size(size)} · 整盘扫描",
                "size": size, "fs": "", "kind": "disk", "parent": None,
                "mounted": [], "detail": "可移动磁盘" if removable else "内置磁盘",
                "internal": not removable,
            })
            try:
                parts = sorted(p for p in os.listdir(f"{base}/{name}") if p.startswith(name))
            except OSError:
                parts = []
            for p in parts:
                try:
                    with open(f"{base}/{name}/{p}/size") as fh:
                        psize = int(fh.read().strip()) * 512
                except OSError:
                    psize = None
                items.append({
                    "path": f"/dev/{p}",
                    "title": f"  └ 分区 {p} · {human_size(psize)}",
                    "size": psize, "fs": "", "kind": "part", "parent": dev,
                    "mounted": mount_points_for(f"/dev/{p}"),
                    "detail": "", "internal": not removable,
                })
        return _ext_first(items)


def _ext_first(items: list[dict]) -> list[dict]:
    """外接盘排前面：那是要救数据的盘，内置盘放最后防误选。"""
    return sorted(items, key=lambda x: (x.get("internal", False), x.get("kind") != "disk"))
