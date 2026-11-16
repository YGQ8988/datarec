#!/usr/bin/env python3
"""生成测试样本文件（图片 / 视频 / 文档 / 镜像 / 安装包 / 压缩包）。

故意不依赖 PIL、ffmpeg：图片用 zlib 手工拼 PNG，视频用结构化 box 拼 MP4/MOV，
文档用 zipfile 拼 OOXML，PDF 手工算 xref 偏移。目的是让恢复程序面对"结构真实"的文件，
而不是一堆随机字节。
"""
import os
import random
import struct
import sys
import zlib
import zipfile

RND = random.Random(20261116)


# ---------------------------------------------------------------- 图片
def make_png(path, w, h, color=(30, 120, 200)):
    """真 PNG：IHDR + IDAT(zlib) + IEND。"""
    raw = b"".join(
        b"\x00" + b"".join(
            bytes(((x * 7 + y * 3) % 256, color[1], (x + y) % 256)) for x in range(w)
        )
        for y in range(h)
    )

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def make_tiff(path, w, h):
    """Little-endian TIFF，单条带未压缩 RGB。"""
    pixels = bytes(RND.randrange(256) for _ in range(w * h * 3))
    entries = []

    def entry(tag, typ, count, value):
        entries.append((tag, typ, count, value))

    entry(256, 4, 1, w)
    entry(257, 4, 1, h)
    entry(258, 3, 3, None)  # BitsPerSample -> 指向 3 个 SHORT
    entry(259, 3, 1, 1)
    entry(262, 3, 1, 2)
    entry(273, 4, 1, None)  # StripOffsets
    entry(277, 3, 1, 3)
    entry(278, 4, 1, h)
    entry(279, 4, 1, len(pixels))
    entry(284, 3, 1, 1)
    ifd_off = 8
    ifd_size = 2 + len(entries) * 12 + 4
    bps_off = ifd_off + ifd_size
    data_off = bps_off + 6
    out = bytearray()
    out += b"II*\x00" + struct.pack("<I", ifd_off)
    out += struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        if tag == 258:
            out += struct.pack("<HHII", tag, typ, count, bps_off)
        elif tag == 273:
            out += struct.pack("<HHII", tag, typ, count, data_off)
        elif typ == 3 and count == 1:
            out += struct.pack("<HHIHH", tag, typ, count, value, 0)
        else:
            out += struct.pack("<HHII", tag, typ, count, value)
    out += struct.pack("<I", 0)
    out += struct.pack("<HHH", 8, 8, 8)
    out += pixels
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


# ---------------------------------------------------------------- 视频
def box(tag, payload):
    return struct.pack(">I", len(payload) + 8) + tag + payload


def make_mp4(path, payload_size, faststart=False, brand=b"isom"):
    ftyp = box(b"ftyp", brand + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")
    mdat_payload = bytes(RND.randrange(256) for _ in range(payload_size))
    mdat = box(b"mdat", mdat_payload)
    mvhd = box(b"mvhd", struct.pack(">IIII", 0, 0, 0, 1000) + bytes(92))
    trak = box(b"trak", box(b"tkhd", bytes(84)) + box(b"mdia", box(b"mdhd", bytes(24))))
    moov = box(b"moov", mvhd + trak)
    data = ftyp + (moov + mdat if faststart else mdat + moov)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def make_heic(path, payload_size):
    ftyp = box(b"ftyp", b"heic" + struct.pack(">I", 0) + b"mif1heicmiaf")
    meta = box(b"meta", struct.pack(">I", 0) + box(b"hdlr", bytes(24)))
    mdat = box(b"mdat", bytes(RND.randrange(256) for _ in range(payload_size)))
    data = ftyp + meta + mdat
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def make_mkv(path, payload_size):
    # 1A 45 DF A3 + 一个未知大小的 EBML 头，后面直接跟随机帧数据
    head = b"\x1a\x45\xdf\xa3" + b"\x9f" + b"\x42\x86\x81\x01" + b"\x42\xf7\x81\x01"
    with open(path, "wb") as f:
        f.write(head + bytes(RND.randrange(256) for _ in range(payload_size)))
    return len(head) + payload_size


# ---------------------------------------------------------------- 文档
def make_pdf(path, pages=2):
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(pages):
        stream = (f"BT /F1 24 Tf 72 700 Td (DataRec page {i + 1}) Tj ET".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + i * 2} 0 R >>").encode())
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def make_ooxml(path, kind):
    """真 OOXML 结构：[Content_Types].xml + _rels + 主部件。"""
    main = {
        "docx": ("word/document.xml",
                 '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                 'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>数据恢复测试文档</w:t>'
                 '</w:r></w:p></w:body></w:document>'),
        "xlsx": ("xl/worksheets/sheet1.xml",
                 '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                 'spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr">'
                 '<is><t>数据恢复</t></is></c></row></sheetData></worksheet>'),
        "pptx": ("ppt/slides/slide1.xml",
                 '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/'
                 'presentationml/2006/main"><p:cSld><p:spTree/></p:cSld></p:sld>'),
    }[kind]
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/>'
          f'<Override PartName="/{main[0]}" ContentType="application/vnd.openxmlformats-'
          f'officedocument.{kind}+xml"/></Types>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   f'Target="{main[0]}"/></Relationships>')
        z.writestr(main[0], main[1])
        prefix = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[kind]
        for i in range(12):  # 撑大一点，跨越多个簇
            z.writestr(f"{prefix}/media/pad{i}.bin", bytes(RND.randrange(256) for _ in range(800)))
    return os.path.getsize(path)


def main(dst):
    os.makedirs(dst, exist_ok=True)
    made = []

    def rec(rel):
        p = os.path.join(dst, rel)
        made.append((rel, os.path.getsize(p)))

    for d in ("photos/sub", "videos", "docs", "images", "packages", "archives"):
        os.makedirs(os.path.join(dst, d), exist_ok=True)

    make_png(os.path.join(dst, "photos/IMG_0001.png"), 320, 240, (200, 30, 60))
    rec("photos/IMG_0001.png")
    make_tiff(os.path.join(dst, "photos/scan.tiff"), 160, 120)
    rec("photos/scan.tiff")
    make_png(os.path.join(dst, "photos/sub/IMG_0002.png"), 200, 150, (10, 160, 90))
    rec("photos/sub/IMG_0002.png")

    make_mp4(os.path.join(dst, "videos/clip_faststart.mp4"), 900_000, faststart=True)
    rec("videos/clip_faststart.mp4")
    make_mp4(os.path.join(dst, "videos/clip_moovend.mov"), 700_000, faststart=False, brand=b"qt  ")
    rec("videos/clip_moovend.mov")
    make_heic(os.path.join(dst, "photos/IMG_0003.heic"), 300_000)
    rec("photos/IMG_0003.heic")
    make_mkv(os.path.join(dst, "videos/screen.mkv"), 400_000)
    rec("videos/screen.mkv")

    make_pdf(os.path.join(dst, "docs/report.pdf"), pages=3)
    rec("docs/report.pdf")
    make_ooxml(os.path.join(dst, "docs/notes.docx"), "docx")
    rec("docs/notes.docx")
    make_ooxml(os.path.join(dst, "docs/sheet.xlsx"), "xlsx")
    rec("docs/sheet.xlsx")
    make_ooxml(os.path.join(dst, "docs/deck.pptx"), "pptx")
    rec("docs/deck.pptx")
    with open(os.path.join(dst, "docs/readme.txt"), "w") as f:
        f.write("普通文本文件，用于验证非魔数类型也能按目录项恢复。\n" * 40)
    rec("docs/readme.txt")

    # 大文件：制造跨大量簇的连续分配，且刻意夹在两个小文件之间
    with open(os.path.join(dst, "images/disk_backup.img"), "wb") as f:
        f.write(bytes(RND.randrange(256) for _ in range(1_400_000)))
    rec("images/disk_backup.img")

    import subprocess
    # 真 zip
    with zipfile.ZipFile(os.path.join(dst, "archives/backup.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for rel, _ in made[:6]:
            z.write(os.path.join(dst, rel), rel)
    rec("archives/backup.zip")

    for rel, size in made:
        print(f"  {size:>10,}  {rel}")
    print(f"共 {len(made)} 个样本")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/DATARECTEST")
