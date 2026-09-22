"""Update the installed dashboard from the project's GitHub main branch."""

import os
import sys
from pathlib import Path

from llm_usage_monitor.process import trusted_which

_SOURCE = "git+https://github.com/1122-gggggg/llm_show_usage.git@main"


def run_self_update() -> int:
    uv = trusted_which("uv")
    if uv is None:
        print("找不到 uv；請先安裝 uv：https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    print(
        "正在交由 uv 從 GitHub main 更新；完成後請重新執行 llu。",
        flush=True,
    )
    try:
        # Replacing this process releases the tool environment's executable on
        # Windows before uv removes it; a waiting subprocess would keep it locked.
        os.execv(
            uv,
            [
                uv,
                "--no-config",
                "--directory",
                str(Path.home()),
                "tool",
                "install",
                "--force",
                "--refresh",
                "--reinstall-package",
                "llm-usage-monitor",
                _SOURCE,
            ],
        )
    except OSError as exc:
        print(f"無法啟動更新：{exc}", file=sys.stderr)
        return 1
