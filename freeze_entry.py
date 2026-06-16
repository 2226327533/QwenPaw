# freeze_entry.py — PyInstaller 专用入口
# 用绝对导入避免 relative import 问题
import sys
import os

# 确保打包后的包在 sys.path 中
app_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# 绝对导入 — 使用 click CLI 入口
from qwenpaw.cli.main import cli

if __name__ == '__main__':
    cli()