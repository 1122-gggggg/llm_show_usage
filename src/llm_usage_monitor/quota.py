from __future__ import annotations

import hashlib
import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
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


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_CREDENTIAL_BYTES = 1024 * 1024
_MAX_CACHE_ENTRIES = 64
_MAX_SNAPSHOT_RETRIES = 3
_CREDENTIAL_CHANGED_NOTE = "憑證在配額查詢期間變更，請重試"
_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "chatgpt-account-id",
    "cookie",
    "cookie2",
    "proxy-authorization",
    "x-api-key",
    "x-xai-token-auth",
}


def _same_origin(left: str, right: str) -> bool:
    try:
        lhs = urllib.parse.urlsplit(left)
        rhs = urllib.parse.urlsplit(right)
        lhs_port = lhs.port
        if lhs_port is None:
            lhs_port = {"http": 80, "https": 443}.get(lhs.scheme.lower())
        rhs_port = rhs.port
        if rhs_port is None:
            rhs_port = {"http": 80, "https": 443}.get(rhs.scheme.lower())
    except ValueError:
        return False
    return (
        lhs.scheme.lower(),
        lhs.hostname,
        lhs_port,
    ) == (
        rhs.scheme.lower(),
        rhs.hostname,
        rhs_port,
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and not _same_origin(
            req.full_url, redirected.full_url
        ):
            for name, _value in tuple(redirected.header_items()):
                if name.lower() in _SENSITIVE_REDIRECT_HEADERS:
                    redirected.remove_header(name)
        return redirected


_HTTP_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


LOGIN_NOTE = "請先在該 CLI login"


@dataclass
class QuotaResult:
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: str | None = None
    note: str | None = None


class _TransientQuotaResult(QuotaResult):
    """Internal marker for failures that may reuse a last-good cache entry."""


@dataclass(frozen=True)
class _CacheEntry:
    identity: object
    checked_at: float
    success_at: float | None
    result: QuotaResult
    transient: bool = False


def apply_live(
    snap: ProviderSnapshot, result: QuotaResult, *, replace: bool
) -> ProviderSnapshot:
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


def _read_body(response: Any) -> bytes:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("response body exceeds limit")
    return raw


def http_get(
    url: str, headers: dict[str, str], timeout: float = 3.0
) -> tuple[int, Any]:
    merged = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) llm-usage-monitor/0.1"
    }
    merged.update(headers)
    try:
        req = urllib.request.Request(url, headers=merged)
        with _HTTP_OPENER.open(req, timeout=timeout) as resp:
            raw = _read_body(resp)
            try:
                return resp.status, json.loads(raw)
            except (RecursionError, ValueError, UnicodeDecodeError):
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body: Any
        try:
            with exc:
                body = json.loads(_read_body(exc))
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            RecursionError,
            ValueError,
        ):
            body = str(exc.reason)
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, exc.__class__.__name__
    except (http.client.HTTPException, TypeError, ValueError) as exc:
        # Invalid header exceptions can include the complete credential value.
        return 0, exc.__class__.__name__


def _parse_ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
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
        if not isinstance(slot, dict) or not slot:
            continue
        used_percent = _as_percent(slot.get("utilization"))
        if used_percent is None:
            continue
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=used_percent,
                resets_at=_parse_ts(slot.get("resets_at")),
            )
        )
    return windows, None


def parse_codex(data: dict) -> tuple[list[QuotaWindow], str | None]:
    windows: list[QuotaWindow] = []
    rate = data.get("rate_limit") or {}
    if not isinstance(rate, dict):
        rate = {}
    for raw in (rate.get("primary_window"), rate.get("secondary_window")):
        if not isinstance(raw, dict) or not raw:
            continue
        seconds = _as_int(raw.get("limit_window_seconds"))
        if seconds is None or seconds <= 0:
            label = "配額"
        else:
            label = "5h" if seconds <= 86400 else "週"
        used_percent = _as_percent(raw.get("used_percent"))
        if used_percent is None:
            continue
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=used_percent,
                resets_at=_parse_ts(raw.get("reset_at")),
            )
        )
    return windows, _clean_string(data.get("plan_type"))


def parse_grok(data: dict) -> tuple[list[QuotaWindow], str | None]:
    config = data.get("config") or {}
    if not isinstance(config, dict):
        config = {}
    period = config.get("currentPeriod") or {}
    if not isinstance(period, dict):
        period = {}
    raw_period_type = period.get("type")
    period_type = raw_period_type if isinstance(raw_period_type, str) else ""
    label = "週配額" if period_type.endswith("WEEKLY") else "配額"
    percent = config.get("creditUsagePercent")
    if percent is None:
        products = config.get("productUsage") or []
        if isinstance(products, list):
            for product in products:
                if (
                    isinstance(product, dict)
                    and product.get("usagePercent") is not None
                ):
                    percent = product["usagePercent"]
                    break
    used_percent = _as_percent(percent)
    plan = _clean_string(data.get("subscriptionTier")) or _clean_string(
        data.get("subscription_tier_display")
    )
    if used_percent is None:
        return [], plan
    window = QuotaWindow(
        label=label,
        used_percent=used_percent,
        resets_at=_parse_ts(period.get("end") or config.get("billingPeriodEnd")),
    )
    return [window], plan


def parse_opencode(data: dict) -> tuple[list[QuotaWindow], str | None]:
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    windows: list[QuotaWindow] = []
    mapping = (("rolling", "5h"), ("weekly", "週"), ("monthly", "月"))
    for key, label in mapping:
        slot = usage.get(key)
        if not isinstance(slot, dict) or not slot:
            continue
        used_percent = _as_percent(slot.get("percent"))
        if used_percent is None:
            continue
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=used_percent,
                resets_at=_parse_ts(slot.get("resetsAt")),
            )
        )
    return windows, "GO" if windows else None


def parse_copilot(data: dict) -> tuple[list[QuotaWindow], str | None]:
    snaps = data.get("quota_snapshots") or {}
    if not isinstance(snaps, dict):
        snaps = {}
    premium = snaps.get("premium_interactions") or {}
    if not isinstance(premium, dict):
        premium = {}
    plan = _clean_string(data.get("copilot_plan"))
    if premium.get("unlimited") is True:
        return [
            QuotaWindow(
                label="Premium",
                used_percent=0.0,
                resets_at=None,
                detail="unlimited",
            )
        ], plan
    remaining_pct = _as_percent(premium.get("percent_remaining"))
    used = 100.0 - remaining_pct if remaining_pct is not None else None
    if used is not None and not math.isfinite(used):
        used = None
    if used is None:
        entitlement = _as_float(premium.get("entitlement"))
        left = _as_float(premium.get("remaining"))
        if entitlement is not None and entitlement > 0 and left is not None:
            candidate = 100.0 - (left / entitlement * 100.0)
            used = (
                candidate
                if math.isfinite(candidate) and 0 <= candidate <= 100
                else None
            )
    if used is None:
        return [], plan
    reset = data.get("quota_reset_date_utc") or data.get("quota_reset_date")
    detail = None
    if premium.get("remaining") is not None and premium.get("entitlement") is not None:
        detail = f"{premium.get('remaining')}/{premium.get('entitlement')}"
    return [
        QuotaWindow(
            label="Premium",
            used_percent=used,
            resets_at=_parse_ts(reset),
            detail=detail,
        )
    ], plan


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
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        group, limit, raw_remaining, reset = (part.strip() for part in parts)
        remaining = _as_percent(raw_remaining.rstrip("%"))
        if remaining is None:
            continue
        label = f"{group_names.get(group, group)} {limit_names.get(limit, limit)}"
        windows.append(
            QuotaWindow(
                label=label,
                used_percent=100.0 - remaining,
                resets_at=_parse_ts(reset),
            )
        )
    return windows, None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_percent(value: Any) -> float | None:
    parsed = _as_float(value)
    return parsed if parsed is not None and 0.0 <= parsed <= 100.0 else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _credential_snapshot(path: Path) -> tuple[object, dict | None]:
    try:
        normalized = str(path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        normalized = str(path.absolute())
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        return (normalized, None), None
    identity = (normalized, len(raw), hashlib.sha256(raw).digest())
    if len(raw) > _MAX_CREDENTIAL_BYTES:
        return identity, None
    try:
        data = json.loads(raw)
    except (UnicodeError, RecursionError, ValueError):
        return identity, None
    return identity, data if isinstance(data, dict) else None


def _clone_result(result: QuotaResult) -> QuotaResult:
    return QuotaResult(
        windows=[
            QuotaWindow(
                label=window.label,
                used_percent=window.used_percent,
                resets_at=window.resets_at,
                detail=window.detail,
            )
            for window in result.windows
        ],
        plan=result.plan,
        note=result.note,
    )


def _token_identity(token: object) -> bytes | None:
    cleaned = _clean_string(token)
    if cleaned is None:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).digest()


def _clean_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_transient_status(status: int) -> bool:
    return status == 0 or status in (408, 425, 429) or 500 <= status < 600


def _fetch_failure(note: str, status: int) -> QuotaResult:
    result_type = (
        _TransientQuotaResult
        if status == 200 or _is_transient_status(status)
        else QuotaResult
    )
    return result_type(note=note)


def _current_windows(windows: list[QuotaWindow]) -> list[QuotaWindow]:
    now = datetime.now(UTC)
    current: list[QuotaWindow] = []
    for window in windows:
        reset = window.resets_at
        if reset is None:
            current.append(window)
            continue
        try:
            aware = (
                reset if reset.utcoffset() is not None else reset.replace(tzinfo=UTC)
            )
            if aware > now:
                current.append(window)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
    return current


class QuotaClient:
    def __init__(
        self,
        get: GetFn = http_get,
        ttl: float = 10.0,
        max_stale: float = 300.0,
    ) -> None:
        self._get = get
        self._ttl = ttl
        self._max_stale = max(0.0, max_stale)
        self._cache: OrderedDict[tuple[str, object], _CacheEntry] = OrderedDict()
        self._cache_guard = threading.Lock()
        self._key_locks: dict[str, Any] = {}
        self._key_locks_guard = threading.Lock()
        self._disabled = False

    def claude(self, creds_path: Path) -> QuotaResult:
        return self._cached(
            "claude",
            lambda: _credential_snapshot(creds_path),
            self._fetch_claude,
        )

    def codex(self, auth_path: Path) -> QuotaResult:
        return self._cached(
            "codex",
            lambda: _credential_snapshot(auth_path),
            self._fetch_codex,
        )

    def grok(self, auth_path: Path) -> QuotaResult:
        return self._cached(
            "grok",
            lambda: _credential_snapshot(auth_path),
            self._fetch_grok,
        )

    def opencode(self, auth_path: Path) -> QuotaResult:
        return self._cached(
            "opencode",
            lambda: _credential_snapshot(auth_path),
            self._fetch_opencode,
        )

    def copilot(self, token: str | None) -> QuotaResult:
        return self._cached(
            "copilot",
            lambda: (_token_identity(token), _clean_string(token)),
            self._fetch_copilot,
        )

    @classmethod
    def disabled(cls) -> QuotaClient:
        client = cls(get=lambda *_a, **_k: (0, None))
        client._disabled = True
        return client

    def _lock_for(self, key: str) -> Any:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _cache_get(self, key: tuple[str, object]) -> _CacheEntry | None:
        with self._cache_guard:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
            return entry

    def _cache_put(self, key: tuple[str, object], entry: _CacheEntry) -> None:
        with self._cache_guard:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > _MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)

    def _cached(
        self,
        key: str,
        snapshot: Callable[[], tuple[object, Any]],
        fetch: Callable[[Any], QuotaResult],
    ) -> QuotaResult:
        if self._disabled:
            return QuotaResult()
        for _attempt in range(_MAX_SNAPSHOT_RETRIES):
            expected_identity, payload = snapshot()
            cache_key = (key, expected_identity)
            with self._lock_for(key):
                current_identity, payload = snapshot()
                if current_identity != expected_identity:
                    continue
                now = time.monotonic()
                hit = self._cache_get(cache_key)
                if hit and now - hit.checked_at < self._ttl:
                    stale_expired = (
                        hit.transient
                        and hit.success_at is not None
                        and now - hit.success_at > self._max_stale
                    )
                    if not stale_expired:
                        if hit.transient:
                            current = _current_windows(hit.result.windows)
                            if current:
                                cached_result = _clone_result(hit.result)
                                cached_result.windows = [
                                    QuotaWindow(
                                        label=window.label,
                                        used_percent=window.used_percent,
                                        resets_at=window.resets_at,
                                        detail=window.detail,
                                    )
                                    for window in current
                                ]
                                return cached_result
                        else:
                            return _clone_result(hit.result)

                result = fetch(payload)
                result_is_transient = isinstance(result, _TransientQuotaResult)
                finished_at = time.monotonic()
                if snapshot()[0] != expected_identity:
                    continue
                success_at = finished_at if result.windows else None
                stale_windows = _current_windows(hit.result.windows) if hit else []
                if (
                    not result.windows
                    and result_is_transient
                    and hit
                    and stale_windows
                    and hit.success_at is not None
                    and finished_at - hit.success_at <= self._max_stale
                ):
                    result = QuotaResult(
                        windows=stale_windows,
                        plan=hit.result.plan,
                        note=result.note,
                    )
                    result_is_transient = True
                    success_at = hit.success_at

                stored = _clone_result(result)
                self._cache_put(
                    cache_key,
                    _CacheEntry(
                        identity=expected_identity,
                        checked_at=finished_at,
                        success_at=success_at,
                        result=stored,
                        transient=result_is_transient,
                    ),
                )
                return _clone_result(stored)
        return QuotaResult(note=_CREDENTIAL_CHANGED_NOTE)

    def _fetch_claude(self, data: dict | None) -> QuotaResult:
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        oauth = data.get("claudeAiOauth") or data
        if not isinstance(oauth, dict):
            return QuotaResult(note=LOGIN_NOTE)
        token = _clean_string(oauth.get("accessToken"))
        if token is None:
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
            return _fetch_failure("Claude 配額讀取失敗", status)
        windows, plan = parse_claude(body)
        return QuotaResult(
            windows=windows,
            plan=plan or _clean_string(oauth.get("subscriptionType")),
        )

    def _fetch_codex(self, data: dict | None) -> QuotaResult:
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        tokens = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            return QuotaResult(note=LOGIN_NOTE)
        token = _clean_string(tokens.get("access_token"))
        account = _clean_string(tokens.get("account_id"))
        if token is None or account is None:
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
            return _fetch_failure("Codex 配額讀取失敗", status)
        windows, plan = parse_codex(body)
        return QuotaResult(windows=windows, plan=plan)

    def _fetch_grok(self, data: dict | None) -> QuotaResult:
        if not data:
            return QuotaResult(note=LOGIN_NOTE)
        entry = next(iter(data.values()), None)
        if not isinstance(entry, dict):
            return QuotaResult(note=LOGIN_NOTE)
        token = _clean_string(entry.get("key"))
        if token is None:
            return QuotaResult(note=LOGIN_NOTE)
        headers = {
            "Authorization": f"Bearer {token}",
            "x-xai-token-auth": "xai-grok-cli",
            "Accept": "application/json",
        }
        status, body = self._get(GROK_BILLING, headers)
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return _fetch_failure("Grok 配額讀取失敗", status)
        windows, plan = parse_grok(body)
        if not plan:
            settings_status, settings = self._get(GROK_SETTINGS, headers)
            if settings_status == 200 and isinstance(settings, dict):
                plan = _clean_string(settings.get("subscription_tier_display")) or plan
        return QuotaResult(windows=windows, plan=plan)

    def _fetch_opencode(self, data: dict | None) -> QuotaResult:
        if not data:
            return QuotaResult()
        entry = data.get("opencode") or {}
        if not isinstance(entry, dict):
            return QuotaResult()
        token = _clean_string(entry.get("key"))
        if token is None:
            return QuotaResult()
        status, body = self._get(
            OPENCODE_GO_USAGE,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        if status == 401:
            return QuotaResult(note=LOGIN_NOTE)
        if status != 200 or not isinstance(body, dict):
            return _fetch_failure("OpenCode 配額讀取失敗", status)
        windows, plan = parse_opencode(body)
        return QuotaResult(windows=windows, plan=plan)

    def _fetch_copilot(self, token: str | None) -> QuotaResult:
        token = _clean_string(token)
        if token is None:
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
            return _fetch_failure("Copilot 配額讀取失敗", status)
        windows, plan = parse_copilot(body)
        return QuotaResult(windows=windows, plan=plan)
