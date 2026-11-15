#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 GUI + CLI 打包成单文件 exe（需要先装 PyInstaller）。

用法：
    python build_exe.py

产物：_dist/ass-font-fetch.exe（双击即用，不依赖目标机器的 Python）
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "_dist"
WORK = HERE / "_build"


def main() -> int:
    try:
        import PyInstaller.__main__ as pyi
    except ImportError:
        print("没装 PyInstaller。先执行：")
        print("  python -m pip install pyinstaller")
        return 2

    for extra in ("gui.html", "acgrip_index.json", "sources.json"):
        if not (HERE / extra).exists():
            print(f"缺少文件：{extra}")
            return 2

    if DIST.exists():
        shutil.rmtree(DIST)
    if WORK.exists():
        shutil.rmtree(WORK)

    args = [
        str(HERE / "gui_server.py"),
        "--name", "ass-font-fetch",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--console",
        "--paths", str(HERE),
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--add-data", f"{HERE / 'gui.html'};.",
        "--add-data", f"{HERE / 'acgrip_index.json'};.",
        "--add-data", f"{HERE / 'sources.json'};.",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    print("PyInstaller 参数：", " ".join(args))
    pyi.run(args)

    exe = DIST / "ass-font-fetch.exe"
    if not exe.exists():
        print("构建失败：没有生成 exe")
        return 1
    size = exe.stat().st_size / 1048576
    print(f"\n完成：{exe}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
