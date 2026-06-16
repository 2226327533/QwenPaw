# -*- coding: utf-8 -*-
# freeze_entry.py — PyInstaller 专用入口
# 用绝对导入避免 relative import 问题
import os
import sys

# 确保打包后的包在 sys.path 中
if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(sys.executable)
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from qwenpaw.cli.main import cli  # noqa: E402  pylint: disable=C0413

if __name__ == "__main__":
    cli()  # pylint: disable=E1120
