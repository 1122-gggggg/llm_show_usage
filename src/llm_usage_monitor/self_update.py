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
    # Keep a running uv tool's interpreter and environment intact. Recreating
    # that environment with `uv tool install` locks its Scripts dir on Windows.
    if (Path(sys.prefix) / "uv-receipt.toml").is_file():
        install_args = ["pip", "install", "--python", sys.executable]
    else:
        # A checkout / regular virtualenv installs the user-facing uv tool,
        # rather than replacing the development environment's own package.
        install_args = ["tool", "install", "--force"]
    command = [
        uv,
        "--no-config",
        *install_args,
        "--refresh",
        "--reinstall-package",
        "llm-usage-monitor",
        _SOURCE,
    ]
    print("正在從 GitHub main 更新，請稍候…", flush=True)
    try:
        result = subprocess.run(command, cwd=Path.home(), check=False)
    except KeyboardInterrupt:
        print("更新已取消。", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"無法啟動更新：{exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print("更新失敗；請查看上方 uv 錯誤訊息。", file=sys.stderr)
        return result.returncode if result.returncode > 0 else 1
    print("更新完成！執行 llu 即可開啟儀表板。", flush=True)
    return 0
