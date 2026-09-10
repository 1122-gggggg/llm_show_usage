import json
import subprocess

from llm_usage_monitor.ohmypi import (
    EMPTY_NOTE,
    FAILED_NOTE,
    KNOWN_FAMILIES,
    NOT_INSTALLED_NOTE,
    OhmypiStore,
    build_snapshots,
    parse_omp_payload,
)
from llm_usage_monitor.providers.ohmypi import OhmypiProvider, build_ohmypi_providers


def _limit(
    lid,
    label,
    window_id,
    window_label,
    used_fraction,
    resets_at=1789104885000,
    status="ok",
    shared=False,
):
    return {
        "id": lid,
        "label": label,
        "scope": {"provider": "x", "windowId": window_id, "shared": shared},
        "window": {"id": window_id, "label": window_label, "resetsAt": resets_at},
        "amount": {
            "unit": "percent",
            "remainingFraction": 1.0 - used_fraction,
            "usedFraction": used_fraction,
            "remaining": (1.0 - used_fraction) * 100.0,
            "used": used_fraction * 100.0,
            "limit": 100,
        },
        "status": status,
    }


def _report(provider, email, limits, plan=None):
    metadata = {"email": email}
    if plan:
        metadata["planType"] = plan
    return {
        "provider": provider,
        "fetchedAt": 1789034542538,
        "limits": limits,
        "metadata": metadata,
    }


def test_single_account_uses_plain_labels() -> None:
    payload = {
        "reports": [
            _report(
                "anthropic",
                "alice@gmail.com",
                [
                    _limit("anthropic:5h", "Claude 5 Hour", "5h", "5 Hour", 0.0),
                    _limit("anthropic:7d", "Claude 7 Day", "7d", "7 Day", 0.79),
                ],
            )
        ]
    }
    snapshots = parse_omp_payload(payload)
    assert set(snapshots) == set(KNOWN_FAMILIES)
    snap = snapshots["anthropic"]
    assert snap.name == "OMP Claude"
    assert [window.label for window in snap.quotas] == ["5h", "週"]
    assert [window.used_percent for window in snap.quotas] == [0.0, 79.0]
    assert snap.plan is None
    assert snap.notes == []


def test_multi_account_antigravity_shows_every_account() -> None:
    payload = {
        "reports": [
            _report(
                "google-antigravity",
                "alice@gmail.com",
                [
                    _limit(
                        "google-antigravity:google:default:gemini-weekly",
                        "Usage (Google)",
                        "weekly",
                        "Weekly",
                        0.93,
                    ),
                    _limit(
                        "google-antigravity:google:default:gemini-5h",
                        "Usage (Google)",
                        "5h",
                        "5 Hour",
                        0.0,
                    ),
                ],
            ),
            _report(
                "google-antigravity",
                "bob@gmail.com",
                [
                    _limit(
                        "google-antigravity:google:default:gemini-weekly",
                        "Usage (Google)",
                        "weekly",
                        "Weekly",
                        0.5,
                    ),
                ],
            ),
        ]
    }
    snap = parse_omp_payload(payload)["google-antigravity"]
    assert snap.name == "OMP Antigravity"
    assert [window.label for window in snap.quotas] == [
        "alice·Gemini 5h",
        "alice·Gemini 週",
        "bob·Gemini 週",
    ]
    assert [window.used_percent for window in snap.quotas] == [0.0, 93.0, 50.0]
    assert snap.plan == "共2帳號"


def test_empty_and_invalid_payloads() -> None:
    for bad in ({}, {"reports": []}, {"reports": "nope"}):
        snapshots = build_snapshots(bad)
        assert all(snap.notes == [EMPTY_NOTE] for snap in snapshots.values())
    snapshots = parse_omp_payload({"__error__": FAILED_NOTE})
    assert all(snap.notes == [EMPTY_NOTE] for snap in snapshots.values())
    snapshots = build_snapshots({"__error__": FAILED_NOTE})
    assert all(snap.notes == [FAILED_NOTE] for snap in snapshots.values())
    snapshots = build_snapshots(None)
    assert all(snap.notes == [NOT_INSTALLED_NOTE] for snap in snapshots.values())


def test_shared_3p_limits_merge_into_one_label() -> None:
    payload = {
        "reports": [
            _report(
                "google-antigravity",
                "alice@gmail.com",
                [
                    _limit(
                        "google-antigravity:anthropic:default:3p-weekly",
                        "Usage (Anthropic)",
                        "weekly",
                        "Weekly",
                        0.0,
                        shared=True,
                    ),
                    _limit(
                        "google-antigravity:openai:default:3p-weekly",
                        "Usage (OpenAI)",
                        "weekly",
                        "Weekly",
                        0.0,
                        shared=True,
                    ),
                ],
            )
        ]
    }
    snap = parse_omp_payload(payload)["google-antigravity"]
    assert [window.label for window in snap.quotas] == ["Claude/GPT 週"]


def test_exhausted_note_carries_account_tag() -> None:
    payload = {
        "reports": [
            _report(
                "openai-codex",
                "alice@gmail.com",
                [
                    _limit(
                        "openai-codex:primary",
                        "30 days",
                        "30d",
                        "30 days",
                        1.0,
                        status="exhausted",
                    )
                ],
                plan="free",
            )
        ]
    }
    snap = parse_omp_payload(payload)["openai-codex"]
    assert [window.label for window in snap.quotas] == ["30天"]
    assert snap.plan == "free"
    assert snap.notes == ["alice·30天 已耗盡"]


def test_grok_build_and_credits_labels() -> None:
    payload = {
        "reports": [
            _report(
                "xai-oauth",
                "alice@gmail.com",
                [
                    _limit(
                        "xai-oauth:credits:1w",
                        "SuperGrok Weekly Credits",
                        "1w",
                        "Weekly",
                        0.77,
                    ),
                    _limit(
                        "xai-oauth:product:grokbuild:1w",
                        "Grok Build (Weekly)",
                        "1w",
                        "Weekly",
                        0.77,
                    ),
                ],
            )
        ]
    }
    snap = parse_omp_payload(payload)["xai-oauth"]
    assert [window.label for window in snap.quotas] == ["Build 週", "點數 週"]


def test_used_fallback_without_fraction() -> None:
    report = _report(
        "opencode-go",
        "alice@gmail.com",
        [
            {
                "id": "monthly",
                "label": "Monthly limit",
                "window": {"id": "monthly", "label": "Monthly", "resetsAt": None},
                "amount": {"unit": "percent", "used": 89},
                "status": "warning",
            }
        ],
        plan="OpenCode Go",
    )
    snap = parse_omp_payload({"reports": [report]})["opencode-go"]
    assert [(window.label, window.used_percent) for window in snap.quotas] == [
        ("月", 89.0)
    ]
    assert snap.plan == "OpenCode Go"


def test_unknown_family_surfaced_on_carrier() -> None:
    payload = {
        "reports": [{"provider": "future-provider", "limits": [], "metadata": {}}]
    }
    snapshots = parse_omp_payload(payload)
    assert snapshots["anthropic"].notes == [
        EMPTY_NOTE,
        "omp 另有未收錄來源（future-provider），請更新 llm-usage",
    ]


def test_store_caches_within_ttl() -> None:
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"reports": []}), "")

    store = OhmypiStore(runner=runner, ttl=60.0, binary="/fake/omp")
    first = store.snapshots()
    second = store.snapshots()
    assert first is second
    assert [cmd[:3] for cmd in calls] == [["/fake/omp", "usage", "--json"]]


def test_store_refetches_after_ttl() -> None:
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"reports": []}), "")

    store = OhmypiStore(runner=runner, ttl=0.0, binary="/fake/omp")
    store.snapshots()
    store.snapshots()
    # ttl=0 forces a refetch; both calls still return well-formed snapshots.
    assert set(store.snapshots()) == set(KNOWN_FAMILIES)


def test_store_reports_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr("llm_usage_monitor.ohmypi.trusted_which", lambda _name: None)
    store = OhmypiStore()
    assert store.snapshot_for("anthropic").notes == [NOT_INSTALLED_NOTE]


def test_store_reports_failed_command() -> None:
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    store = OhmypiStore(runner=runner, binary="/fake/omp")
    assert store.snapshot_for("anthropic").notes == [FAILED_NOTE]


def test_providers_share_one_store() -> None:
    providers = build_ohmypi_providers()
    assert [(provider.key, provider.name) for provider in providers] == [
        ("ohmypi", "OMP Claude"),
        ("ohmypi", "OMP Codex"),
        ("ohmypi", "OMP Antigravity"),
        ("ohmypi", "OMP Grok"),
        ("ohmypi", "OMP GO"),
    ]
    stores = {id(provider._store) for provider in providers}
    assert len(stores) == 1


def test_provider_snapshot_uses_store() -> None:
    store = OhmypiStore(binary="/fake/omp")
    provider = OhmypiProvider("xai-oauth", store)
    assert provider.key == "ohmypi"
    assert provider.name == "OMP Grok"
