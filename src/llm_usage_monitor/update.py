"""一鍵更新所有本機 LLM CLI。

每個工具都沿用它自己的官方更新指令，不猜套件管理器、不寫入額外狀態。
未安裝的工具會標示略過，不影響其他工具。
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.table import Table

from llm_usage_monitor.process import trusted_which

_COMMAND_TIMEOUT = 300.0
_VERSION_TIMEOUT = 15.0
_MAX_OUTPUT_CHARS = 2000
_MAX_WORKERS = 4


@dataclass(frozen=True)
class UpdateSpec:
    key: str
    display: str
    binaries: tuple[str, ...]
    args: tuple[str, ...]
    hint: str = ""


UPDATE_SPECS: tuple[UpdateSpec, ...] = (
    UpdateSpec(
        key="claude",
        display="Claude Code",
        binaries=("claude",),
        args=("update",),
        hint="手動更新請見 https://code.claude.com",
    ),
    UpdateSpec(
        key="codex",
        display="Codex",
        binaries=("codex",),
        args=("update",),
    ),
    UpdateSpec(
        key="grok",
        display="Grok",
        binaries=("grok",),
        args=("update",),
    ),
    UpdateSpec(
        key="opencode",
        display="OpenCode",
        binaries=("opencode",),
        args=("upgrade",),
    ),
    UpdateSpec(
        key="copilot",
        display="GitHub CLI 擴充",
        binaries=("gh",),
        args=("extension", "upgrade", "--all"),
        hint="gh 本體請用系統套件管理器更新",
    ),
    UpdateSpec(
        key="antigravity",
        display="Antigravity",
        binaries=("agy",),
        args=("update",),
    ),
    UpdateSpec(
        key="ohmypi",
        display="Oh My Pi",
        binaries=("omp",),
        args=("update",),
    ),
)


@dataclass
class UpdateResult:
    spec: UpdateSpec
    binary: str | None = None
    status: str = "skipped"
    version_before: str | None = None
    version_after: str | None = None
    detail: str = ""
    elapsed: float = 0.0


def _short_text(value: object, *, max_chars: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_chars:
        text = f"{text[: max_chars - 1].rstrip()}…"
    return text


def _probe_version(
    binary: str, *, runner: Any, timeout: float = _VERSION_TIMEOUT
) -> str | None:
    try:
        completed = runner(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = _short_text(f"{completed.stdout or ''} {completed.stderr or ''}".strip())
    return output or None


def run_update(
    spec: UpdateSpec, *, runner: Any = subprocess.run, timeout: float = _COMMAND_TIMEOUT
) -> UpdateResult:
    """Run a single tool update; never raises on tool failure."""
    result = UpdateResult(spec=spec)
    binary: str | None = None
    for name in spec.binaries:
        binary = trusted_which(name)
        if binary:
            break
    result.binary = binary
    if binary is None:
        result.status = "skipped"
        result.detail = spec.hint or "未安裝，已略過"
        return result
    started = time.monotonic()
    result.version_before = _probe_version(binary, runner=runner)
    try:
        completed = runner(
            [binary, *spec.args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result.elapsed = time.monotonic() - started
        result.status = "failed"
        result.detail = f"更新逾時（>{timeout:g}s）"
        return result
    except OSError as exc:
        result.elapsed = time.monotonic() - started
        result.status = "failed"
        result.detail = _short_text(f"{exc.__class__.__name__}: {exc}")
        return result
    result.elapsed = time.monotonic() - started
    result.version_after = _probe_version(binary, runner=runner)
    if completed.returncode == 0:
        if (
            result.version_before
            and result.version_after
            and result.version_before != result.version_after
        ):
            result.status = "updated"
        else:
            result.status = "ok"
        result.detail = _tail_output(completed.stdout) or "已是最新"
    else:
        result.status = "failed"
        tail = _tail_output(completed.stderr) or _tail_output(completed.stdout)
        result.detail = tail or f"exit {completed.returncode}"
        if spec.hint:
            result.detail = f"{result.detail}；{spec.hint}"
    return result


def _tail_output(value: object, *, lines: int = 3) -> str:
    text = str(value or "").strip().replace("\r\n", "\n")
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return _short_text(tail, max_chars=_MAX_OUTPUT_CHARS)


def run_all_updates(
    specs: tuple[UpdateSpec, ...] = UPDATE_SPECS,
    *,
    runner: Any = subprocess.run,
    timeout: float = _COMMAND_TIMEOUT,
) -> list[UpdateResult]:
    """Update every installed tool in parallel; result order matches specs."""
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(specs) or 1)) as pool:
        return list(
            pool.map(
                lambda spec: run_update(spec, runner=runner, timeout=timeout), specs
            )
        )


_STATUS_STYLE = {
    "updated": "bold #9ece6a",
    "ok": "#9ece6a",
    "skipped": "dim #565f89",
    "failed": "bold #f7768e",
    "dry": "#7aa2f7",
}

_STATUS_TEXT = {
    "updated": "已更新",
    "ok": "已是最新",
    "skipped": "略過",
    "failed": "失敗",
    "dry": "待執行",
}


def describe_plan(
    specs: tuple[UpdateSpec, ...] = UPDATE_SPECS,
) -> list[UpdateResult]:
    """Resolve binaries without running anything (for --update-check)."""
    resolved: list[UpdateResult] = []
    for spec in specs:
        binary: str | None = None
        for name in spec.binaries:
            binary = trusted_which(name)
            if binary:
                break
        resolved.append(
            UpdateResult(
                spec=spec,
                binary=binary,
                status="dry",
                detail="" if binary else (spec.hint or "未安裝"),
            )
        )
    return resolved


def build_plan_table(resolved: list[UpdateResult]) -> Table:
    table = Table(box=None, expand=True, pad_edge=False, show_edge=False)
    table.add_column("工具", no_wrap=True)
    table.add_column("指令", no_wrap=False)
    table.add_column("狀態", no_wrap=True)
    for result in resolved:
        spec = result.spec
        if result.binary is None:
            command = f"{spec.binaries[0]} {' '.join(spec.args)}"
            status = "未安裝"
        else:
            command = f"{result.binary} {' '.join(spec.args)}".strip()
            status = "可執行"
        table.add_row(spec.display, command, status)
    return table


def _version_change(result: UpdateResult) -> str:
    before, after = result.version_before, result.version_after
    if before and after:
        return before if before == after else f"{before} → {after}"
    return after or before or "—"


def build_result_table(results: list[UpdateResult]) -> Table:
    table = Table(box=None, expand=True, pad_edge=False, show_edge=False)
    table.add_column("工具", no_wrap=True)
    table.add_column("結果", no_wrap=True)
    table.add_column("版本", no_wrap=False)
    table.add_column("備註", no_wrap=False)
    for result in results:
        style = _STATUS_STYLE.get(result.status, "")
        table.add_row(
            result.spec.display,
            f"[{style}]{_STATUS_TEXT.get(result.status, result.status)}[/]"
            if style
            else _STATUS_TEXT.get(result.status, result.status),
            _version_change(result),
            result.detail,
        )
    return table


def run_updates_command(
    *,
    check_only: bool = False,
    specs: tuple[UpdateSpec, ...] = UPDATE_SPECS,
    runner: Any = subprocess.run,
    console: Console | None = None,
) -> int:
    """Entry point for ``llm-usage --update``; returns a process exit code."""
    active = console or Console()
    if check_only:
        active.print(build_plan_table(describe_plan(specs)))
        return 0
    results = run_all_updates(specs, runner=runner)
    active.print(build_result_table(results))
    failed = sum(result.status == "failed" for result in results)
    if failed:
        active.print(f"{failed} 個工具更新失敗", style="bold #f7768e")
        return 1
    updated = sum(result.status == "updated" for result in results)
    skipped = sum(result.status == "skipped" for result in results)
    active.print(
        f"完成：{updated} 個已更新，{len(results) - updated - skipped - failed} 個已是最新，"
        f"{skipped} 個未安裝略過",
        style="#9ece6a",
    )
    return 0
