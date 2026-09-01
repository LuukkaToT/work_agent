"""容器入口：可选迁移后启动 uvicorn。避免 shell 脚本 CRLF 在 Linux 上踩坑。"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    if os.environ.get("SKIP_MIGRATE", "").strip() != "1":
        subprocess.check_call([sys.executable, "-m", "work_agent.cli", "init-db"])
    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "work_agent.api.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            "1",
        ],
    )


if __name__ == "__main__":
    main()
