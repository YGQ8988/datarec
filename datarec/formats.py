"""格式字典：扩展名 → 类别 / 魔数格式 → 期望扩展名。

恢复出来的文件名通常还在（目录项里），但断链/被覆盖的文件只能靠魔数判断类型，
所以两类映射都要维护。
"""
from __future__ import annotations

import os

# 类别：恢复结果按这个分目录，也是 --types 过滤的取值
CATEGORIES = {
    "images": "图片",
    "videos": "视频",
    "audio": "音频",
    "documents": "文档",
    "archives": "压缩包",
    "diskimages": "镜像文件",
    "packages": "安装包",
    "other": "其他",
}

_EXT_CATEGORY = {
    # 图片
    **{e: "images" for e in (
        "jpg jpeg jpe jfif png gif bmp tif tiff heic heif avif webp raw cr2 cr3 nef arw dng "
        "orf rw2 raf srw pef icns psd xcf svg ico").split()},
    # 视频
    **{e: "videos" for e in (
        "mp4 mov m4v mkv avi wmv flv webm mpg mpeg m2ts mts ts 3gp ogv rmvb vob").split()},
    # 音频
    **{e: "audio" for e in "mp3 wav aiff aif flac m4a aac ogg wma opus ape".split()},
    # 文档
    **{e: "documents" for e in (
        "pdf doc docx xls xlsx ppt pptx pages numbers key txt md rtf csv tsv json xml html "
        "htm epub mobi azw3 odt ods odp log ini plist yml yaml tex").split()},
    # 压缩包
    **{e: "archives" for e in "zip rar 7z tar gz bz2 xz tgz tbz2 cab lz4 zst".split()},
    # 镜像文件
    **{e: "diskimages" for e in (
        "dmg iso img toast sparseimage sparsebundle vhd vhdx vmdk qcow2 bin cue nrg "
        "dd raw e01").split()},
    # 安装包
    **{e: "packages" for e in "pkg mpkg xar apk ipa deb rpm exe msi dmgpart app msix".split()},
}

# 格式 id → 默认扩展名（雕刻出来的文件用这个命名）
FMT_EXT = {
    "jpeg": "jpg", "png": "png", "gif": "gif", "bmp": "bmp", "tiff": "tiff",
    "heic": "heic", "webp": "webp", "avif": "avif", "icns": "icns", "psd": "psd",
    "mp4": "mp4", "mov": "mov", "m4v": "m4v", "mkv": "mkv", "webm": "webm", "avi": "avi",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "aiff": "aiff", "ogg": "ogg",
    "pdf": "pdf", "zip": "zip", "docx": "docx", "xlsx": "xlsx", "pptx": "pptx",
    "epub": "epub", "rar": "rar", "7z": "7z", "tar": "tar", "gzip": "gz",
    "dmg": "dmg", "iso": "iso", "xar": "pkg", "sqlite": "db",
    "gpt": "img", "exfatvol": "img", "ntfsvol": "img", "apfs": "img", "hfs": "img",
}

# 扩展名 → 允许的格式 id（用于校验"名字说是 jpg，内容真的是 jpg 吗"）
_EXT_FMT = {
    "jpg": {"jpeg"}, "jpeg": {"jpeg"}, "jpe": {"jpeg"}, "jfif": {"jpeg"},
    "png": {"png"}, "gif": {"gif"}, "bmp": {"bmp"}, "tif": {"tiff"}, "tiff": {"tiff"},
    "heic": {"heic"}, "heif": {"heic"}, "webp": {"webp"}, "avif": {"avif", "heic"},
    "icns": {"icns"}, "psd": {"psd"},
    "mp4": {"mp4"}, "m4v": {"mp4"}, "mov": {"mov", "mp4"},
    "mkv": {"mkv"}, "webm": {"webm"}, "avi": {"avi"}, "iso": {"iso"},
    "mp3": {"mp3"}, "wav": {"wav"}, "flac": {"flac"}, "aiff": {"aiff"}, "aif": {"aiff"},
    "ogg": {"ogg"},
    "pdf": {"pdf"}, "zip": {"zip"}, "docx": {"zip", "docx"}, "xlsx": {"zip", "xlsx"},
    "pptx": {"zip", "pptx"}, "epub": {"zip", "epub"},
    "rar": {"rar"}, "7z": {"7z"}, "tar": {"tar"},
    "dmg": {"dmg", "gpt", "apfs", "hfs", "exfatvol", "ntfsvol"},
    "img": {"iso", "dmg", "gpt", "apfs", "hfs", "exfatvol", "ntfsvol"},
    "raw": {"iso", "dmg", "gpt", "apfs", "hfs", "exfatvol", "ntfsvol"},
    "dd": {"iso", "dmg", "gpt", "apfs", "hfs", "exfatvol", "ntfsvol"},
    "toast": {"iso", "hfs", "gpt"},
    "vhd": {"gpt", "ntfsvol", "exfatvol"},
    "vhdx": {"gpt", "ntfsvol", "exfatvol"},
    "vmdk": {"gpt", "ntfsvol", "exfatvol"},
    "qcow2": {"gpt", "ntfsvol", "exfatvol"},
    "pkg": {"xar", "zip"}, "xar": {"xar"},
    "db": {"sqlite"}, "sqlite": {"sqlite"},
}


def normalize_ext(name: str) -> str:
    """'a.JPG ' → 'jpg'；没有扩展名 → ''。"""
    base = os.path.basename(name).strip().rstrip(".")
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].lower()


def category_of(name: str) -> str:
    ext = normalize_ext(name)
    if not ext:
        return "other"
    return _EXT_CATEGORY.get(ext, "other")


def allowed_formats(name: str) -> set[str]:
    """按扩展名给出的、可接受的魔数格式集合。空集合表示不做魔数约束。"""
    ext = normalize_ext(name)
    return set(_EXT_FMT.get(ext, set()))


def texty(name: str) -> bool:
    """内容本身没有强魔数的纯文本类，校验策略不同。"""
    return normalize_ext(name) in {
        "txt", "md", "log", "csv", "tsv", "json", "xml", "html", "htm", "ini",
        "plist", "yml", "yaml", "rtf", "tex", "srt", "vtt", "cfg", "conf",
    }
