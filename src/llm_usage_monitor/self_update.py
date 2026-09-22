"""Update the installed dashboard from the project's GitHub main branch."""

import subprocess
import sys
from pathlib import Path

from llm_usage_monitor.process import trusted_which

_SOURCE = "git+https://github.com/1122-gggggg/llm_show_usage.git@main"


def run_self_update() -> int:
    uv = trusted_which("uv")
    if uv is None:
        print("找不到 uv；請先安裝 uv：https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    print("正在從 GitHub main 更新 llm_show_usage…", flush=True)
    try:
        result = subprocess.run(
            [
                uv,
                "--no-config",
                "tool",
                "install",
                "--force",
                "--refresh",
                "--reinstall-package",
                "llm-usage-monitor",
                _SOURCE,
            ],
            cwd=Path.home(),
            check=False,
        )
    except KeyboardInterrupt:
        print("更新已取消", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"無法啟動更新：{exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print("更新失敗；請查看上方 uv 錯誤訊息。", file=sys.stderr)
        return result.returncode if result.returncode > 0 else 1
    print(
        "更新完成，請重新執行 llu。若找不到指令，請執行 uv tool update-shell 並重開終端。"
    )
    return 0
