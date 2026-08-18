from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from llm_usage_monitor.login import ensure_sessions
from llm_usage_monitor.providers import build_providers
from llm_usage_monitor.tui import run_live, run_once

DEFAULT_PROVIDERS = "claude,codex,grok,opencode,copilot,antigravity"
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="llm-usage")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--providers", default=DEFAULT_PROVIDERS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--login", action="store_true", help="開啟來源登入選單")
    parser.add_argument("--yes", action="store_true", help="登入全部未連接來源")
    parser.add_argument("--claude-dir", type=Path)
    parser.add_argument("--codex-dir", type=Path)
    parser.add_argument("--grok-dir", type=Path)
    parser.add_argument("--opencode-db", type=Path)
    return parser.parse_args(argv)


def _select(_prompt: str) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        return _LOGIN_CONSOLE.input(
            "  [bold #7aa2f7]選擇要登入的來源[/]"
            " [dim](1,3 / a 全選 / Enter 略過)[/]: "
        )
    except EOFError:
        return ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wanted = {name.strip().lower() for name in args.providers.split(",") if name.strip()}
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if args.login or (interactive and not args.once):
        ensure_sessions(
            wanted,
            select=_select,
            select_all=bool(args.yes),
            printer=_menu_print,
        )

    providers = [
        provider
        for provider in build_providers(
            claude_dir=args.claude_dir,
            codex_dir=args.codex_dir,
            grok_dir=args.grok_dir,
            opencode_db=args.opencode_db,
        )
        if provider.name.lower().replace(" ", "") in wanted
        or (provider.name == "OpenCode" and "opencode" in wanted)
        or (provider.name == "Copilot" and "copilot" in wanted)
        or (provider.name == "Antigravity" and "antigravity" in wanted)
    ]
    if args.once:
        run_once(providers, args.interval)
    else:
        run_live(providers, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
