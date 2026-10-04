"""`python -m service` 入口：把命令行返回值转成进程退出码。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
