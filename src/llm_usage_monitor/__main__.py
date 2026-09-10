from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from llm_usage_monitor.login import default_sources, ensure_sessions
from llm_usage_monitor.providers import build_providers
from llm_usage_monitor.tui import run_live, run_once
from llm_usage_monitor.update import run_updates_command

DEFAULT_PROVIDERS = "claude,codex,grok,opencode,copilot,antigravity,ohmypi"
SUPPORTED_PROVIDERS = frozenset(DEFAULT_PROVIDERS.split(","))
_LOGIN_CONSOLE = Console(stderr=True)


def _menu_print(message: str) -> None:
    if message == "來源狀態":
        _LOGIN_CONSOLE.print()
        _LOGIN_CONSOLE.rule(
            Text("SOURCE LOGIN", style="bold #7aa2f7"),
            style="#414868",
            align="left",
        )
        return
    if not message:
        _LOGIN_CONSOLE.print()
        return
    if message.startswith("✓"):
        text = Text("  ● ", style="#9ece6a")
        text.append(message[1:].strip(), style="#c0caf5")
        _LOGIN_CONSOLE.print(text)
        return
    if message.startswith("○"):
        text = Text("  ○ ", style="#e0af68")
        text.append(message[1:].strip(), style="#a9b1d6")
        _LOGIN_CONSOLE.print(text)
        return
    if message.startswith("["):
        _LOGIN_CONSOLE.print(f"  {message}", style="#7aa2f7", markup=False)
        return
    if message.startswith("正在"):
        _LOGIN_CONSOLE.print(f"  ↗ {message}", style="#e0af68", markup=False)
        return
    style = "#9ece6a" if "登入完成" in message else "#f7768e"
    _LOGIN_CONSOLE.print(f"  {message}", style=style, markup=False)


def _positive_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or not 0.1 <= interval <= 86_400:
        raise argparse.ArgumentTypeError("更新間隔必須介於 0.1 秒與 86400 秒之間")
    return interval


def _provider_list(value: str) -> str:
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("至少要指定一個來源")
    unknown = sorted(set(names) - SUPPORTED_PROVIDERS)
    if unknown:
        available = ",".join(sorted(SUPPORTED_PROVIDERS))
        raise argparse.ArgumentTypeError(
            f"不支援的來源: {','.join(unknown)}；可用來源: {available}"
        )
    return ",".join(names)


def _expanded_path(value: str) -> Path:
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError) as exc:
        raise argparse.ArgumentTypeError(f"無法展開路徑: {value}") from exc


def _live_capable() -> bool:
    console = Console()
    return sys.stdout.isatty() and console.is_terminal and not console.is_dumb_terminal


def _configure_stdio_errors() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="llm-usage")
    parser.add_argument("--interval", type=_positive_interval, default=10)
    parser.add_argument("--providers", type=_provider_list, default=DEFAULT_PROVIDERS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--login", action="store_true", help="開啟來源登入選單")
    parser.add_argument("--yes", action="store_true", help="登入全部未連接來源")
    parser.add_argument(
        "--update", action="store_true", help="一鍵更新所有本機 LLM CLI 後離開"
    )
    parser.add_argument(
        "--update-check",
        action="store_true",
        help="只顯示更新計畫，不執行更新",
    )
    parser.add_argument("--claude-dir", type=_expanded_path)
    parser.add_argument("--codex-dir", type=_expanded_path)
    parser.add_argument("--grok-dir", type=_expanded_path)
    parser.add_argument("--opencode-db", type=_expanded_path)
    parser.add_argument("--opencode-auth", type=_expanded_path)
    return parser.parse_args(argv)


def _select(_prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return ""
    try:
        return _LOGIN_CONSOLE.input(
            "  [bold #7aa2f7]選擇要登入的來源[/] [dim](1,3 / a 全選 / Enter 略過)[/]: "
        )
    except EOFError:
        return ""


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_errors()
    args = parse_args(argv)
    if args.update or args.update_check:
        return run_updates_command(check_only=not args.update)
    wanted = {
        name.strip().lower() for name in args.providers.split(",") if name.strip()
    }
    live_capable = _live_capable()
    interactive = sys.stdin.isatty() and sys.stderr.isatty() and live_capable
    try:
        sources = default_sources(
            claude_dir=args.claude_dir,
            codex_dir=args.codex_dir,
            grok_dir=args.grok_dir,
        )
        if args.login or args.yes or (interactive and not args.once):
            ensure_sessions(
                wanted,
                select=_select,
                select_all=bool(args.yes),
                printer=_menu_print,
                sources=sources,
            )

        providers = [
            provider
            for provider in build_providers(
                claude_dir=args.claude_dir,
                codex_dir=args.codex_dir,
                grok_dir=args.grok_dir,
                opencode_db=args.opencode_db,
                opencode_auth=args.opencode_auth,
            )
            if provider.key in wanted
        ]
        if args.once or not live_capable:
            run_once(providers, args.interval)
        else:
            run_live(
                providers,
                args.interval,
                sources=sources,
                select=_select,
                printer=_menu_print,
            )
        return 0
    except KeyboardInterrupt:
        _LOGIN_CONSOLE.print("\n已取消", style="dim", markup=False)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
