"""Double-click after installing 1294_依赖.txt using Python 3.11 or later."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name('1294_目录工具.py')), run_name='__main__')
