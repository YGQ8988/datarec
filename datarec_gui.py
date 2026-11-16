#!/usr/bin/env python3
"""图形界面入口：双击运行，或在命令行里 `python datarec_gui.py`。

只依赖标准库 tkinter。打包成 Windows exe 时也用这个文件当入口：
    pyinstaller --noconsole --onefile --name DataRec datarec_gui.py
"""
import os
import sys

# 允许从源码目录直接运行（不必先 pip install）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        from datarec.gui import main as gui_main
    except ImportError as exc:                     # pragma: no cover
        sys.stderr.write(f"缺少图形界面依赖：{exc}\n"
                         "tkinter 是 Python 标准库的一部分；Linux 上需要装 python3-tk。\n")
        return 1
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
