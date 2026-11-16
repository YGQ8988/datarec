"""图形界面：让不会敲命令的人也能把数据救回来。

设计原则：
1) **四步一条线**：选盘 → 扫描 → 勾选 → 恢复。每一步做完才解锁下一步，不让人
   在不知道盘里有什么的情况下瞎点。
2) **危险动作有闸门**：往待恢复的卷上写、不卸载就扫、盘还没选就开跑，全都会被拦住
   并给出具体该怎么做的提示。
3) **磁盘操作永远在后台线程**：Tk 不是线程安全的，界面只通过队列接收消息，
   所以扫 1TB 的盘时窗口照样能拖动、能点停止。

界面上所有文案默认中文，不依赖任何第三方库（只用标准库 tkinter）。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import __version__, oslayer
from .device import Source, SourceError
from .exfat import ExFatVolume
from .formats import CATEGORIES, category_of
from .recover import SYSTEM_DIRS, Cancelled, Recoverer
from .report import write_csv, write_markdown
from .state import STATE_NAME, Fingerprint, ResumeState, file_key

MiB = 1024 * 1024

# --------------------------------------------------------------------------- 外观
BG = "#f4f5f7"
PANEL = "#ffffff"
BORDER = "#e3e5e9"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
DANGER = "#dc2626"
WARN = "#b45309"
OK = "#15803d"
SEL_BG = "#dbeafe"

# 界面上展示的类别顺序（"其他"放最后，免得抢注意力）
CATEGORY_ORDER = ["images", "videos", "documents", "archives",
                  "diskimages", "packages", "audio", "other"]


def _pick_font(root) -> tuple[str, int]:
    fams = set(tkfont.families(root))
    size = 12 if oslayer.IS_MACOS else 10
    for name in ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei",
                 "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC",
                 "Source Han Sans SC", "Segoe UI", "Helvetica Neue"):
        if name in fams:
            return name, size
    return "TkDefaultFont", size


def _pick_mono(root) -> str:
    fams = set(tkfont.families(root))
    for name in ("Menlo", "Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                 "Source Code Pro", "Courier New"):
        if name in fams:
            return name
    return "TkFixedFont"


def _human(n: float | None) -> str:
    if n is None:
        return "-"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _open_in_explorer(path: str) -> None:
    try:
        if oslayer.IS_WINDOWS:
            os.startfile(path)                      # type: ignore[attr-defined]
        elif oslayer.IS_MACOS:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def _default_out() -> str:
    home = os.path.expanduser("~")
    for name in ("Desktop", "桌面"):
        cand = os.path.join(home, name)
        if os.path.isdir(cand):
            home = cand
            break
    return os.path.join(home, "救回的数据_" + time.strftime("%m%d_%H%M"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"exFAT 数据恢复  {__version__}")
        self.geometry("1120x980")
        self.minsize(1000, 760)
        self.configure(bg=BG)

        self.font_family, self.font_size = _pick_font(self)
        self.mono_family = _pick_mono(self)
        self._setup_style()

        # ---- 状态
        self.src: Source | None = None
        self.vol: ExFatVolume | None = None
        self.entries: list = []            # 全部文件条目（FileEntry）
        self.filtered: list = []           # 当前筛选结果
        self.selected: set[str] = set()    # 勾选的 path
        self.row_entries: dict[str, object] = {}   # tree iid → entry
        self.queue: queue.Queue = queue.Queue()
        self.cancel_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.out_dir = tk.StringVar(value=_default_out())
        self.source_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.inc_internal = tk.BooleanVar(value=False)
        self.deep_verify = tk.BooleanVar(value=False)
        self.do_carve = tk.BooleanVar(value=True)
        self.only_deleted = tk.BooleanVar(value=False)
        self.show_system = tk.BooleanVar(value=False)   # ._ 资源叉、.fseventsd 这些默认藏起来
        self.min_mb = tk.StringVar(value="0")
        self.cat_vars: dict[str, tk.BooleanVar] = {}

        self._build()
        self._drain_job = self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self.refresh_disks)

        if oslayer.IS_WINDOWS and not oslayer.is_admin():
            self.log("提示：当前不是管理员权限。读物理磁盘需要管理员，"
                     "可点右上角「以管理员身份重启」。", "warn")

    # ------------------------------------------------------------------ 样式
    def _setup_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        f = (self.font_family, self.font_size)
        st.configure(".", background=BG, foreground=TEXT, font=f)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT, font=f)
        st.configure("Card.TLabel", background=PANEL, font=f)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=f)
        st.configure("Hint.TLabel", background=BG, foreground=MUTED,
                     font=(self.font_family, self.font_size - 1))
        st.configure("Title.TLabel", background=BG, foreground=TEXT,
                     font=(self.font_family, self.font_size + 7, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED,
                     font=(self.font_family, self.font_size))
        st.configure("Step.TLabel", background=PANEL, foreground=ACCENT,
                     font=(self.font_family, self.font_size, "bold"))
        st.configure("TButton", font=f, padding=(12, 6))
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     font=(self.font_family, self.font_size, "bold"),
                     borderwidth=0, padding=(18, 8))
        st.map("Accent.TButton",
               background=[("disabled", "#a9bdf0"), ("active", ACCENT_HOVER)])
        st.configure("Ghost.TButton", background=PANEL, foreground=TEXT,
                     borderwidth=1, padding=(10, 5))
        st.map("Ghost.TButton", background=[("active", "#eef0f3")])
        st.configure("TCheckbutton", background=PANEL, font=f)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("Filter.TCheckbutton", background=PANEL,
                     font=(self.font_family, self.font_size - 1))
        st.map("Filter.TCheckbutton", background=[("active", PANEL)])
        st.configure("TEntry", fieldbackground="#ffffff", borderwidth=1, padding=5)
        st.configure("TCombobox", padding=5)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=TEXT, rowheight=26 if oslayer.IS_MACOS else 24,
                     borderwidth=0, font=f)
        st.configure("Treeview.Heading", background="#f0f1f4", foreground=MUTED,
                     relief="flat", font=(self.font_family, self.font_size - 1))
        st.map("Treeview.Heading", background=[("active", "#e7e9ee")])
        st.map("Treeview", background=[("selected", SEL_BG)],
               foreground=[("selected", TEXT)])
        st.configure("Accent.Horizontal.TProgressbar", troughcolor="#e6e8ec",
                     background=ACCENT, borderwidth=0, thickness=10)

    # ------------------------------------------------------------------ 布局
    def _card(self, parent, step: str, title: str) -> ttk.Frame:
        """带 1px 边框和标题的白卡片。"""
        outer = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        head = tk.Frame(outer, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(head, text=step, style="Step.TLabel").pack(side="left")
        tk.Label(head, text=title, bg=PANEL, fg=TEXT,
                 font=(self.font_family, self.font_size + 1, "bold")).pack(
            side="left", padx=(8, 0))
        body = ttk.Frame(outer, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=16, pady=(10, 14))
        outer.body = body                      # type: ignore[attr-defined]
        return outer

    def _build(self) -> None:
        root = ttk.Frame(self, padding=(18, 14, 18, 12))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---------------- 标题
        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="exFAT 数据恢复", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(head, text="只读扫描，绝不写源盘 · 误拔、删除、FAT 表丢失都按内容兜底",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        right = ttk.Frame(head)
        right.grid(row=0, column=1, rowspan=2, sticky="e")
        if oslayer.IS_WINDOWS:
            self.admin_btn = ttk.Button(right, text="以管理员身份重启",
                                        style="Ghost.TButton",
                                        command=self._elevate)
            if not oslayer.is_admin():
                self.admin_btn.pack(side="right")
        self.perm_lbl = ttk.Label(right, text="", style="Hint.TLabel")
        self.perm_lbl.pack(side="right", padx=(0, 8))
        self._refresh_perm_label()

        # ---------------- ① 选盘
        c1 = self._card(root, "①", "选择要恢复的数据源")
        c1.grid(row=1, column=0, sticky="ew")
        b1 = c1.body
        b1.columnconfigure(1, weight=1)
        ttk.Label(b1, text="磁盘 / 分区", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.disk_box = ttk.Combobox(b1, state="readonly", height=14)
        self.disk_box.grid(row=0, column=1, sticky="ew")
        self.disk_box.bind("<<ComboboxSelected>>", lambda e: self._on_pick_disk())
        btns = ttk.Frame(b1, style="Card.TFrame")
        btns.grid(row=0, column=2, sticky="e", padx=(10, 0))
        ttk.Button(btns, text="刷新", style="Ghost.TButton",
                   command=self.refresh_disks).pack(side="left")
        ttk.Button(btns, text="选镜像文件…", style="Ghost.TButton",
                   command=self._pick_image).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(b1, style="Card.TFrame")
        row2.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        row2.columnconfigure(1, weight=1)
        ttk.Label(row2, text="数据源", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.src_entry = ttk.Entry(row2, textvariable=self.source_var)
        self.src_entry.grid(row=0, column=1, sticky="ew")
        self.scan_btn = ttk.Button(row2, text="开始扫描", style="Accent.TButton",
                                   command=self.start_scan)
        self.scan_btn.grid(row=0, column=2, padx=(10, 0))
        self.src_note = ttk.Label(b1, text="选一块盘或一个镜像文件，然后点「开始扫描」",
                                  style="Muted.TLabel")
        self.src_note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---------------- ② 文件列表
        c2 = self._card(root, "②", "看看盘里有什么，勾选要恢复的")
        c2.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        b2 = c2.body
        b2.columnconfigure(0, weight=1)
        b2.rowconfigure(2, weight=1)

        bar = ttk.Frame(b2, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        self.count_lbl = ttk.Label(bar, text="尚未扫描", style="Card.TLabel",
                                   font=(self.font_family, self.font_size, "bold"))
        self.count_lbl.pack(side="left")

        filt = ttk.Frame(b2, style="Card.TFrame")
        filt.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(filt, text="类别", style="Muted.TLabel").pack(side="left")
        for key in CATEGORY_ORDER:
            var = tk.BooleanVar(value=True)
            self.cat_vars[key] = var
            ttk.Checkbutton(filt, text=CATEGORIES.get(key, key), variable=var,
                            style="Filter.TCheckbutton",
                            command=self._apply_filter).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(filt, text="只看已删除", variable=self.only_deleted,
                        style="Filter.TCheckbutton",
                        command=self._apply_filter).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(filt, text="显示系统文件", variable=self.show_system,
                        style="Filter.TCheckbutton",
                        command=self._apply_filter).pack(side="left", padx=(8, 0))
        ttk.Label(filt, text="搜索", style="Muted.TLabel").pack(side="left", padx=(14, 4))
        se = ttk.Entry(filt, textvariable=self.search_var, width=16)
        se.pack(side="left")
        se.bind("<KeyRelease>", lambda e: self._debounced_filter())

        wrap = ttk.Frame(b2, style="Card.TFrame")
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("sel", "state", "cat", "size", "path")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="extended")
        self.tree.heading("sel", text="")
        self.tree.heading("state", text="状态")
        self.tree.heading("cat", text="类别")
        self.tree.heading("size", text="大小")
        self.tree.heading("path", text="路径")
        self.tree.column("sel", width=34, anchor="center", stretch=False)
        self.tree.column("state", width=64, anchor="center", stretch=False)
        self.tree.column("cat", width=76, anchor="center", stretch=False)
        self.tree.column("size", width=92, anchor="e", stretch=False)
        self.tree.column("path", width=520, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.tag_configure("deleted", foreground=WARN)
        self.tree.tag_configure("dir", foreground=MUTED)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Double-1>", self._on_tree_dbl)

        act = ttk.Frame(b2, style="Card.TFrame")
        act.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(act, text="全选", style="Ghost.TButton",
                   command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(act, text="全不选", style="Ghost.TButton",
                   command=lambda: self._select_all(False)).pack(side="left", padx=(6, 0))
        ttk.Button(act, text="只选已删除", style="Ghost.TButton",
                   command=self._select_deleted).pack(side="left", padx=(6, 0))
        self.sel_lbl = ttk.Label(act, text="已选 0 项", style="Muted.TLabel")
        self.sel_lbl.pack(side="left", padx=(14, 0))

        # ---------------- ③ 恢复
        c3 = self._card(root, "③", "恢复")
        c3.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        b3 = c3.body
        b3.columnconfigure(1, weight=1)

        ttk.Label(b3, text="输出到", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(b3, textvariable=self.out_dir).grid(row=0, column=1, sticky="ew")
        ttk.Button(b3, text="换目录…", style="Ghost.TButton",
                   command=self._pick_out).grid(row=0, column=2, padx=(10, 0))
        self.open_btn = ttk.Button(b3, text="打开", style="Ghost.TButton",
                                   state="disabled",
                                   command=lambda: _open_in_explorer(self.out_dir.get()))
        self.open_btn.grid(row=0, column=3, padx=(6, 0))

        opts = ttk.Frame(b3, style="Card.TFrame")
        opts.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(opts, text="深度校验（更准，慢一些）",
                        variable=self.deep_verify).pack(side="left")
        ttk.Checkbutton(opts, text="全盘签名雕刻（捞回目录项已丢失的文件）",
                        variable=self.do_carve).pack(side="left", padx=(16, 0))
        ttk.Label(opts, text="跳过小于", style="Card.TLabel").pack(side="left", padx=(16, 4))
        ttk.Spinbox(opts, from_=0, to=1024, width=5, textvariable=self.min_mb).pack(
            side="left")
        ttk.Label(opts, text="MB 的文件", style="Card.TLabel").pack(side="left", padx=(4, 0))

        run = ttk.Frame(b3, style="Card.TFrame")
        run.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        run.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(run, text="开始恢复", style="Accent.TButton",
                                    state="disabled", command=self.start_recover)
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(run, text="停止", style="Ghost.TButton",
                                   state="disabled", command=self.stop_work)
        self.stop_btn.grid(row=0, column=1, padx=(8, 0))
        self.pbar = ttk.Progressbar(run, style="Accent.Horizontal.TProgressbar",
                                    mode="determinate", maximum=1000)
        self.pbar.grid(row=0, column=2, sticky="ew", padx=(16, 10))
        self.pbar_lbl = ttk.Label(run, text="", style="Muted.TLabel", width=34)
        self.pbar_lbl.grid(row=0, column=3, sticky="e")

        # ---------------- 日志
        logw = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        logw.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.log_text = tk.Text(logw, height=4, bg=PANEL, fg=TEXT, bd=0,
                                relief="flat", wrap="word",
                                font=(self.mono_family, self.font_size - 1),
                                padx=12, pady=8, state="disabled",
                                highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("err", foreground=DANGER)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("muted", foreground=MUTED)

    # ------------------------------------------------------------------ 小工具
    def _refresh_perm_label(self) -> None:
        admin = oslayer.is_admin()
        self.perm_lbl.configure(
            text="管理员权限" if admin else "普通权限",
            foreground=OK if admin else MUTED)
        if oslayer.IS_WINDOWS and getattr(self, "admin_btn", None) is not None:
            if admin:
                self.admin_btn.pack_forget()

    def log(self, msg: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] ", "muted")
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _elevate(self) -> None:
        argv = [sys.executable] + sys.argv
        if not oslayer.relaunch_as_admin(argv):
            messagebox.showinfo("以管理员身份运行",
                                "没能自动提权。请关闭本窗口，右键程序 → 以管理员身份运行。")

    def _set_busy(self, busy: bool, can_start: bool | None = None) -> None:
        self.busy = busy
        st = "disabled" if busy else "normal"
        self.scan_btn.configure(state=st)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        if can_start is not None:
            self.start_btn.configure(state="normal" if (can_start and not busy)
                                     else "disabled")

    def _debounced_filter(self) -> None:
        if hasattr(self, "_filter_job"):
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(280, self._apply_filter)

    # ------------------------------------------------------------------ 选盘
    def refresh_disks(self) -> None:
        self.disk_box.configure(values=[])
        self.disk_box.set("正在枚举磁盘…")
        self.update_idletasks()

        def work():
            try:
                items = oslayer.list_sources(include_all=self.inc_internal.get())
                self.queue.put(("disks", items))
            except Exception as exc:
                self.queue.put(("log", (f"枚举磁盘失败：{exc}", "err")))

        threading.Thread(target=work, daemon=True).start()

    def _on_pick_disk(self) -> None:
        items = getattr(self, "_disk_items", [])
        idx = self.disk_box.current()
        if idx < 0 or idx >= len(items):
            return
        it = items[idx]
        self._set_source(it["path"])
        bits = []
        if it["size"]:
            bits.append(f"容量 {_human(it['size'])}")
        if it["fs"]:
            bits.append(it["fs"])
        if it["mounted"]:
            bits.append("⚠ 正在挂载：" + ", ".join(m.rstrip("\\") for m in it["mounted"]))
        self.src_note.configure(text=" ｜ ".join(bits) or "已选择",
                                foreground=WARN if it["mounted"] else MUTED)

    def _set_source(self, path: str) -> None:
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, path)
        self._close_source()
        self.entries = []
        self.filtered = []
        self.selected.clear()
        self.tree.delete(*self.tree.get_children())
        self.count_lbl.configure(text="尚未扫描")
        self.sel_lbl.configure(text="已选 0 项", foreground=MUTED)
        self.start_btn.configure(state="disabled")

    def _pick_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择镜像文件",
            filetypes=[("磁盘镜像", "*.img *.dd *.raw *.dsk *.bin *.bin *.iso"),
                       ("所有文件", "*.*")])
        if path:
            self.disk_box.set("（镜像文件）")
            self._set_source(path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            self.src_note.configure(text=f"镜像文件 ｜ 大小 {_human(size)}", foreground=MUTED)

    # ------------------------------------------------------------------ 扫描
    def start_scan(self) -> None:
        if self.busy:
            return
        path = self.src_entry.get().strip()
        if not path:
            messagebox.showwarning("还没选数据源", "先选一块磁盘，或点「选镜像文件…」。")
            return
        self._close_source()
        self.entries = []
        self.selected.clear()
        self.tree.delete(*self.tree.get_children())
        self.cancel_evt.clear()
        self._set_busy(True, can_start=False)
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(12)
        self.pbar_lbl.configure(text="正在读取卷结构…")
        self.log(f"打开数据源：{path}")

        def work():
            try:
                src = Source(path)
                vol = ExFatVolume.open(src, offset=None)
                files, dirs, warns = vol.walk(should_stop=self.cancel_evt.is_set)
                self.queue.put(("scanned", (src, vol, files, warns)))
            except SourceError as exc:
                self.queue.put(("error", str(exc)))
            except Exception:
                self.queue.put(("error", traceback.format_exc(limit=3)))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _on_scanned(self, payload) -> None:
        src, vol, files, warns = payload
        self.src, self.vol = src, vol
        self.entries = [f for f in files if not f.is_dir]
        stopped = False
        self.pbar.stop()
        self.pbar.configure(mode="determinate")
        self.pbar["value"] = 0
        if self.cancel_evt.is_set():
            stopped = True                     # 用户中途按了停止，结果不完整
            self.cancel_evt.clear()
            self.pbar_lbl.configure(text="扫描已中断")
        else:
            self.pbar_lbl.configure(text="扫描完成")

        info = vol.info.summary()
        live = sum(1 for f in self.entries if not f.deleted)
        gone = len(self.entries) - live
        self.log(f"卷大小 {info.get('卷大小', '?')}，簇大小 {info.get('簇大小', '?')}，"
                 f"引导扇区在偏移 {info.get('引导扇区偏移', '?')}", "ok")
        self.log(f"找到活动文件 {live} 个，已删除（可恢复）{gone} 个")
        if stopped:
            self.log(f"扫描被中断，只列出了已经扫到的 {len(self.entries)} 个文件。"
                     "再点「开始扫描」会重新完整扫一遍。", "warn")
        if vol.info.volume_flags & 0x2:
            self.log("卷的脏标志位是开着的 —— 上次没有正常卸载，符合误拔特征", "warn")
        for w in warns[:5]:
            self.log("提示：" + w, "warn")

        self._apply_filter()
        self._set_busy(False, can_start=bool(self.entries))
        if not self.entries:
            self.log("这个卷里没有解析到文件条目。可以试试纯内容雕刻（在恢复选项里勾选）。",
                     "warn")

    # ------------------------------------------------------------------ 过滤
    def _is_noise(self, e) -> bool:
        """macOS 资源叉、系统目录里的东西，默认不列出来，免得淹没真正要找的文件。"""
        if self.show_system.get():
            return False
        if e.name.startswith("._"):
            return True
        return any(f"/{d}/" in e.path + "/" or e.path.startswith(d + "/")
                   for d in SYSTEM_DIRS)

    def _apply_filter(self) -> None:
        cats = {k for k, v in self.cat_vars.items() if v.get()}
        kw = self.search_var.get().strip().lower()
        out = []
        for e in self.entries:
            if self.only_deleted.get() and not e.deleted:
                continue
            if self._is_noise(e):
                continue
            if category_of(e.name) not in cats:
                continue
            if kw and kw not in e.path.lower():
                continue
            out.append(e)
        self.filtered = out
        self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_entries.clear()
        limit = 3000
        for i, e in enumerate(self.filtered[:limit]):
            iid = str(i)
            self.row_entries[iid] = e
            cat = CATEGORIES.get(category_of(e.name), "其他")
            tags = ("deleted",) if e.deleted else ()
            self.tree.insert("", "end", iid=iid, tags=tags, values=(
                "☑" if e.path in self.selected else "☐",
                "已删除" if e.deleted else "活动",
                cat, _human(e.size), e.path))
        live = sum(1 for f in self.entries if not f.deleted)
        txt = f"共 {len(self.entries)} 个文件（活动 {live}，已删除 {len(self.entries) - live}）"
        if len(self.filtered) != len(self.entries):
            txt += f"，筛出 {len(self.filtered)} 个"
        if len(self.filtered) > limit:
            txt += f"（列表只显示前 {limit} 条，勾选与恢复仍按全部计算）"
        self.count_lbl.configure(text=txt)
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        n = len(self.selected)
        if n:
            size = sum(e.size for e in self.entries if e.path in self.selected)
            self.sel_lbl.configure(text=f"已选 {n} 项，约 {_human(size)}",
                                   foreground=TEXT)
        else:
            self.sel_lbl.configure(text=f"未勾选 → 将恢复筛出的全部 {len(self.filtered)} 项",
                                   foreground=MUTED)

    # ------------------------------------------------------------------ 勾选
    def _on_tree_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle([iid])
            return "break"

    def _on_space(self, _event=None) -> None:
        sel = list(self.tree.selection())
        if sel:
            self._toggle(sel)
        return "break"

    def _on_tree_dbl(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self._toggle([iid])

    def _toggle(self, iids: list[str]) -> None:
        for iid in iids:
            e = self.row_entries.get(iid)
            if not e:
                continue
            if e.path in self.selected:
                self.selected.discard(e.path)
                self.tree.set(iid, "sel", "☐")
            else:
                self.selected.add(e.path)
                self.tree.set(iid, "sel", "☑")
        self._update_sel_label()

    def _select_all(self, on: bool) -> None:
        for e in self.filtered:
            if on:
                self.selected.add(e.path)
            else:
                self.selected.discard(e.path)
        self._fill_tree()

    def _select_deleted(self) -> None:
        allowed = {e.path for e in self.filtered}
        self.selected = {e.path for e in self.entries
                         if e.deleted and e.path in allowed}
        self._fill_tree()

    # ------------------------------------------------------------------ 输出
    def _pick_out(self) -> None:
        path = filedialog.askdirectory(title="选择恢复文件的保存位置",
                                       initialdir=os.path.dirname(self.out_dir.get()) or "~")
        if path:
            self.out_dir.set(path)

    def _check_out_dir(self, out: str) -> str | None:
        """输出目录不能落在待恢复的卷上，否则等于自己覆盖自己。"""
        real_out = os.path.realpath(os.path.abspath(os.path.expanduser(out)))
        if self.src:
            for m in self.src.mount_points():
                rm = os.path.realpath(m)
                if real_out == rm or real_out.startswith(rm + os.sep):
                    return (f"输出目录在待恢复的那个卷上（{m}）。\n"
                            "往里写文件会覆盖还没救出来的数据，换到本机硬盘上。")
        try:
            os.makedirs(real_out, exist_ok=True)
            probe = os.path.join(real_out, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.unlink(probe)
        except OSError as exc:
            return f"输出目录不可写：{exc}"
        return None

    # ------------------------------------------------------------------ 恢复
    def start_recover(self) -> None:
        if self.busy or not self.vol or not self.src:
            return
        out = self.out_dir.get().strip()
        if not out:
            messagebox.showwarning("还没选输出目录", "选一个本机硬盘上的文件夹来放救回的文件。")
            return
        problem = self._check_out_dir(out)
        if problem:
            messagebox.showerror("输出目录有问题", problem)
            return

        cats = {k for k, v in self.cat_vars.items() if v.get()}
        only = set(self.selected) if self.selected else None
        targeted = [e for e in self.filtered
                    if (only is None or e.path in only) and category_of(e.name) in cats]
        if not targeted:
            messagebox.showinfo("没有可恢复的文件", "当前筛选范围内没有文件，换个筛选条件试试。")
            return
        gone = sum(1 for e in targeted if e.deleted)
        total_size = sum(e.size for e in targeted)

        # ---- 断点续扫：上次没跑完的话，先问一句接着来还是重来
        state = ResumeState.open(out, self._fingerprint())
        if state.reset_note:
            self.log(state.reset_note, "warn")
        already = sum(1 for e in targeted
                      if state.has(file_key(e.path), e.size, out))
        if already:
            ans = messagebox.askyesnocancel(
                "发现上次的进度",
                f"输出目录里记着上次的恢复进度，这批文件里有 {already} 个已经恢复好了。\n\n"
                f"「是」接着上次继续，跳过这 {already} 个\n"
                "「否」全部重来（已有的文件不会被覆盖，会另存成 xxx_1）\n"
                "「取消」先不开始")
            if ans is None:
                return
            if not ans:
                state.reset()
                already = 0
                self.log("已选择全部重来，忽略上次的进度记录")
            else:
                self.log(f"接着上次继续，跳过已完成 {already} 个文件")

        if not messagebox.askokcancel(
                "确认开始",
                f"将恢复 {len(targeted)} 个文件（其中已删除 {gone} 个），"
                f"合计约 {_human(total_size)}。\n\n"
                f"输出到：{out}\n\n"
                "恢复过程只读源盘，不会改动它。开始吗？"):
            return

        try:
            min_bytes = int(float(self.min_mb.get() or 0) * MiB)
        except ValueError:
            min_bytes = 0

        self.cancel_evt.clear()
        self._set_busy(True, can_start=False)
        self.start_btn.configure(state="disabled")
        self.pbar["value"] = 0
        self.pbar_lbl.configure(text="准备中…")
        self.log("─" * 46)
        self.log(f"开始恢复 {len(targeted)} 个文件 → {out}")

        only_paths = {e.path for e in targeted}
        deep = self.deep_verify.get()
        carve = self.do_carve.get()

        def work():
            try:
                rec = Recoverer(
                    self.src, self.vol, out,
                    types=cats or None,
                    min_size=min_bytes,
                    deep_verify=deep,
                    carve=carve,
                    verbose=False,
                    progress=lambda m: self.queue.put(("log", (m, ""))),
                    on_progress=lambda f, label: self.queue.put(("prog", (f, label))),
                    cancel=self.cancel_evt.is_set,
                    only_paths=only_paths,
                    state=state,
                )
                results = rec.run()
                self.queue.put(("finished", (rec, results, out, None)))
            except Cancelled:
                self.queue.put(("finished", (rec, rec.results, out, "cancelled")))
            except SourceError as exc:
                self.queue.put(("error", str(exc)))
            except Exception:
                self.queue.put(("error", traceback.format_exc(limit=4)))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def stop_work(self) -> None:
        if not self.busy:
            return
        self.cancel_evt.set()
        self.log("正在停止…（当前文件写完就停，进度会存下来）", "warn")
        self.stop_btn.configure(state="disabled")

    def _fingerprint(self) -> Fingerprint:
        """当前数据源的指纹，用来判断输出目录里的旧进度是不是这同一个卷的。"""
        info = self.vol.info if self.vol else None
        return Fingerprint(path=self.src.path if self.src else "",
                           size=info.size_bytes if info else 0,
                           serial=info.serial if info else 0,
                           offset=info.offset if info else 0)

    def _on_finished(self, payload) -> None:
        rec, results, out, state = payload
        try:
            write_csv(os.path.join(out, "report.csv"), results)
            write_markdown(os.path.join(out, "report.md"), results, rec.stats,
                           self.vol.info.summary() if self.vol else {},
                           self.src.path if self.src else "", out,
                           {"深度校验": self.deep_verify.get(),
                            "雕刻": self.do_carve.get()})
        except OSError as exc:
            self.log(f"写报告失败：{exc}", "err")

        s = rec.stats
        self.pbar["value"] = 1000
        self.pbar_lbl.configure(text="已停止" if state else "完成")
        self._set_busy(False, can_start=True)
        self.open_btn.configure(state="normal")
        self.log("─" * 46)
        if state == "cancelled":
            self.log("已停止。已恢复的文件和报告都保留在输出目录。", "warn")
            self.log(f"进度已保存（{STATE_NAME}）。下次选同一个输出目录，"
                     "会问你要不要接着上次继续。", "warn")
        if s.resumed:
            self.log(f"其中 {s.resumed} 个文件是上次已经恢复好的，本次直接跳过。")
        self.log(f"恢复完成：完好 {s.ok} 个，基本完好 {s.warn} 个，"
                 f"可疑 {s.suspect} 个，重复 {s.duplicate} 个，跳过 {s.skipped} 个",
                 "ok" if s.ok else "warn")
        self.log(f"共写出 {_human(s.bytes)}，耗时 {s.elapsed:.1f} 秒")
        self.log(f"报告：{os.path.join(out, 'report.md')}")
        if s.suspect:
            self.log(f"有 {s.suspect} 个文件被标为可疑（文件名带「__可疑」），建议逐个打开确认。",
                     "warn")

        msg = (f"完好 {s.ok} 个 / 基本完好 {s.warn} 个 / 可疑 {s.suspect} 个\n"
               f"共 {_human(s.bytes)}，耗时 {s.elapsed:.1f} 秒\n\n"
               "打开输出目录看看？")
        if messagebox.askyesno("恢复完成", msg):
            _open_in_explorer(out)

    # ------------------------------------------------------------------ 队列
    def _drain(self) -> None:
        try:
            for _ in range(200):
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(*payload)
                elif kind == "prog":
                    frac, label = payload
                    self.pbar["value"] = int(frac * 1000)
                    self.pbar_lbl.configure(text=label)
                elif kind == "disks":
                    self._disk_items = payload
                    labels = [it["title"] for it in payload] or ["（没有找到可扫描的磁盘）"]
                    self.disk_box.configure(values=labels)
                    if payload:
                        self.disk_box.current(0)
                        # 只在用户还没自己选过数据源时才自动带上第一块盘，
                        # 不然枚举完磁盘会把手填的镜像路径覆盖掉
                        if not self.source_var.get().strip():
                            self._on_pick_disk()
                    else:
                        self.disk_box.set("没有找到磁盘，可以选镜像文件")
                elif kind == "scanned":
                    self._on_scanned(payload)
                elif kind == "finished":
                    self._on_finished(payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        try:
            self._drain_job = self.after(80, self._drain)
        except tk.TclError:          # 窗口已经销毁，别再把回调挂上去
            pass

    def _on_error(self, text: str) -> None:
        self.pbar.stop()
        self.pbar.configure(mode="determinate")
        self.pbar["value"] = 0
        self.pbar_lbl.configure(text="出错了")
        self._set_busy(False, can_start=bool(self.entries))
        self.log(text, "err")
        first = text.strip().splitlines()[0] if text.strip() else "未知错误"
        messagebox.showerror("出错了", first if len(first) < 300 else first[:300] + "…")

    # ------------------------------------------------------------------ 关闭
    def _close_source(self) -> None:
        if self.src is not None:
            try:
                self.src.close()
            except Exception:
                pass
            self.src = None
            self.vol = None

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askokcancel("正在处理", "还在扫描/恢复，确定要退出吗？"):
                return
            self.cancel_evt.set()
        self._close_source()
        self.destroy()

    def destroy(self) -> None:
        """关窗前把还在排队的 after 回调摘掉，否则 Tk 会报 invalid command。"""
        for attr in ("_drain_job", "_filter_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        super().destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
