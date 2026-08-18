from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_usage_monitor.aggregate import parse_iso
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow

GetFn = Callable[..., tuple[int, Any]]

CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE = "https://chatgpt.com/backend-api/wham/usage"
GROK_BILLING = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS = "https://cli-chat-proxy.grok.com/v1/settings"
OPENCODE_GO_USAGE = "https://opencode.ai/zen/go/v1/usage"
COPILOT_USER = "https://api.github.com/copilot_internal/user"


LOGIN_NOTE = "請先在該 CLI login"


@dataclass
class QuotaResult:
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: str | None = None
    note: str | None = None


def apply_live(snap: ProviderSnapshot, result: QuotaResult, *, replace: bool) -> ProviderSnapshot:
    stale_notes = {
        LOGIN_NOTE,
        "Claude 配額讀取失敗",
        "Codex 配額讀取失敗",
        "Grok 配額讀取失敗",
        "OpenCode 配額讀取失敗",
        "Copilot 配額讀取失敗",
        "Antigravity 配額讀取失敗",
        "此帳號沒有 Copilot 訂閱",
        "BYOK,無配額資料",
    }
    snap.notes = [note for note in snap.notes if note not in stale_notes]
    if result.windows:
        snap.quotas = result.windows
    elif replace and result.note:
        snap.quotas = []
    if result.plan:
        snap.plan = result.plan
    if result.note:
        snap.notes.append(result.note)
    return snap

def http_get(url: str, headers: dict[str, str], timeout: float = 3.0) -> tuple[int, Any]:
    merged = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) llm-usage-monitor/0.1"}
    merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body: Any
        try:
            body = json.loads(exc.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = str(exc.reason)
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)




def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = parse_iso(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def parse_claude(data: dict) -> tuple[list[QuotaWindow], str | None]:
    windows: list[QuotaWindow] = []
    mapping = (("five_hour", "5h"), ("seven_day", "週"))
    for key, label in mapping:
        slot = data.get(key)
        if not slot:
            continue
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=_as_float(slot.get("utilization")),
                resets_at=_parse_ts(slot.get("resets_at")),
            )
        )
    return windows, None


def parse_codex(data: dict) -> tuple[list[QuotaWindow], str | None]:
    windows: list[QuotaWindow] = []
    rate = data.get("rate_limit") or {}
    for raw in (rate.get("primary_window"), rate.get("secondary_window")):
        if not raw:
            continue
        seconds = int(raw.get("limit_window_seconds") or 0)
        label = "5h" if seconds and seconds <= 86400 else "週"
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=_as_float(raw.get("used_percent")),
                resets_at=_parse_ts(raw.get("reset_at")),
            )
        )
    return windows, data.get("plan_type")


def parse_grok(data: dict) -> tuple[list[QuotaWindow], str | None]:
    config = data.get("config") or {}
    period = config.get("currentPeriod") or {}
    period_type = str(period.get("type") or "")
    label = "週配額" if period_type.endswith("WEEKLY") else "配額"
    percent = config.get("creditUsagePercent")
    if percent is None:
        products = config.get("productUsage") or []
        if products:
            percent = products[0].get("usagePercent")
    window = QuotaWindow(
        label=label,
        used_percent=_as_float(percent),
        resets_at=_parse_ts(period.get("end") or config.get("billingPeriodEnd")),
    )
    plan = data.get("subscriptionTier") or data.get("subscription_tier_display")
    return [window], plan


def parse_opencode(data: dict) -> tuple[list[QuotaWindow], str | None]:
    usage = data.get("usage") or {}
    windows: list[QuotaWindow] = []
    mapping = (("rolling", "5h"), ("weekly", "週"), ("monthly", "月"))
    for key, label in mapping:
        slot = usage.get(key)
        if not slot:
            continue
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=_as_float(slot.get("percent")),
                resets_at=_parse_ts(slot.get("resetsAt")),
            )
        )
    return windows, "GO" if windows else None




def parse_copilot(data: dict) -> tuple[list[QuotaWindow], str | None]:
    snaps = data.get("quota_snapshots") or {}
    premium = snaps.get("premium_interactions") or {}
    plan = data.get("copilot_plan")
    if premium.get("unlimited"):
        return [QuotaWindow(label="Premium", used_percent=0.0, resets_at=None, detail="unlimited")], plan
    remaining_pct = premium.get("percent_remaining")
    used: float | None
    if remaining_pct is not None:
        used = 100.0 - float(remaining_pct)
    else:
        entitlement = float(premium.get("entitlement") or 0)
        left = float(premium.get("remaining") or 0)
        used = 100.0 - (left / entitlement * 100.0) if entitlement else None
    reset = data.get("quota_reset_date_utc") or data.get("quota_reset_date")
    detail = None
    if premium.get("remaining") is not None and premium.get("entitlement") is not None:
        detail = f"{premium.get('remaining')}/{premium.get('entitlement')}"
    return [QuotaWindow(label="Premium", used_percent=used, resets_at=_parse_ts(reset), detail=detail)], plan


def parse_antigravity_cli(text: str) -> tuple[list[QuotaWindow], str | None]:
    windows: list[QuotaWindow] = []
    group_names = {
        "Gemini Models": "Gemini",
        "Claude and GPT models": "Claude/GPT",
    }
    limit_names = {
        "Weekly Limit Remaining": "週",
        "Five Hour Limit Remaining": "5h",
    }
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 4:
            continue
        group, limit, raw_remaining, reset = parts
        try:
            remaining = float(raw_remaining.rstrip("%"))
        except ValueError:
            continue
        label = f"{group_names.get(group, group)} {limit_names.get(limit, limit)}"
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=max(0.0, min(100.0, 100.0 - remaining)),
                resets_at=_parse_ts(reset),
            )
        )
    return windows, "Google AI Pro" if windows else None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None




class QuotaClient:
    def __init__(self, get: GetFn = http_get, ttl: float = 10.0) -> None:
        self._get = get
        self._ttl = ttl
        self._cache: dict[str, tuple[float, QuotaResult]] = {}

    def claude(self, creds_path: Path) -> QuotaResult:
        return self._cached("claude", lambda: self._fetch_claude(creds_path))

    def codex(self, auth_path: Path) -> QuotaResult:
        return self._cached("codex", lambda: self._fetch_codex(auth_path))

    def grok(self, auth_path: Path) -> QuotaResult:
        return self._cached("grok", lambda: self._fetch_grok(auth_path))

    def opencode(self, auth_path: Path) -> QuotaResult:
        return self._cached("opencode", lambda: self._fetch_opencode(auth_path))


    def copilot(self, token: str | None) -> QuotaResult:
        return self._cached("copilot", lambda: self._fetch_copilot(token))


    @classmethod
    def disabled(cls) -> QuotaClient:
        client = cls(get=lambda *_a, **_k: (0, None), ttl=10**9)
        empty = QuotaResult()
        now = time.monotonic()
        for key in ("claude", "codex", "grok", "opencode", "copilot"):
            client._cache[key] = (now, empty)
        return client

    def _cached(self, key: str, fetch: Callable[[], QuotaResult]) -> QuotaResult:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        result = fetch()
        if (
            not result.windows
            and result.note
            and result.note != LOGIN_NOTE
            and hit
            and hit[1].windows
        ):
            result = QuotaResult(
                windows=hit[1].windows,
                plan=hit[1].plan,
                note=result.note,
            )
        self._cache[key] = (now, result)
        return result

    def _fetch_claude(self, creds_path: Path) -> QuotaResult:
        data = _read_json(creds_path)
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        oauth = data.get("claudeAiOauth") or data
        token = oauth.get("accessToken")
        if not token:
            return QuotaResult(note=LOGIN_NOTE)
        status, body = self._get(
            CLAUDE_USAGE,
            {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
            },
        )
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return QuotaResult(note="Claude 配額讀取失敗")
        windows, plan = parse_claude(body)
        return QuotaResult(windows=windows, plan=plan or oauth.get("subscriptionType"))

    def _fetch_codex(self, auth_path: Path) -> QuotaResult:
        data = _read_json(auth_path)
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        tokens = data.get("tokens") or {}
        token = tokens.get("access_token")
        account = tokens.get("account_id")
        if not token or not account:
            return QuotaResult(note=LOGIN_NOTE)
        status, body = self._get(
            CODEX_USAGE,
            {
                "Authorization": f"Bearer {token}",
                "chatgpt-account-id": account,
                "User-Agent": "codex-cli",
                "Content-Type": "application/json",
            },
        )
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return QuotaResult(note="Codex 配額讀取失敗")
        windows, plan = parse_codex(body)
        return QuotaResult(windows=windows, plan=plan)

    def _fetch_grok(self, auth_path: Path) -> QuotaResult:
        data = _read_json(auth_path)
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        entry = next(iter(data.values()), None)
        if not isinstance(entry, dict) or not entry.get("key"):
            return QuotaResult(note=LOGIN_NOTE)
        headers = {
            "Authorization": f"Bearer {entry['key']}",
            "x-xai-token-auth": "xai-grok-cli",
            "Accept": "application/json",
        }
        status, body = self._get(GROK_BILLING, headers)
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return QuotaResult(note="Grok 配額讀取失敗")
        windows, plan = parse_grok(body)
        settings_status, settings = self._get(GROK_SETTINGS, headers)
        if settings_status == 200 and isinstance(settings, dict):
            plan = settings.get("subscription_tier_display") or plan
        return QuotaResult(windows=windows, plan=plan)

    def _fetch_opencode(self, auth_path: Path) -> QuotaResult:
        data = _read_json(auth_path)
        if not data:
            return QuotaResult()
        entry = data.get("opencode") or {}
        key = entry.get("key")
        if not key:
            return QuotaResult()
        status, body = self._get(
            OPENCODE_GO_USAGE,
            {
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return QuotaResult(note="OpenCode 配額讀取失敗")
        windows, plan = parse_opencode(body)
        return QuotaResult(windows=windows, plan=plan)


    def _fetch_copilot(self, token: str | None) -> QuotaResult:
        if not token:
            return QuotaResult(note=LOGIN_NOTE)
        status, body = self._get(
            COPILOT_USER,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.96.0",
            },
        )
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status in (403, 404):
            return QuotaResult(note="此帳號沒有 Copilot 訂閱")
        if status != 200 or not isinstance(body, dict):
            return QuotaResult(note="Copilot 配額讀取失敗")
        windows, plan = parse_copilot(body)
        return QuotaResult(windows=windows, plan=plan)

