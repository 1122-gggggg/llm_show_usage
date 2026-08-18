from pathlib import Path

from llm_usage_monitor.providers.antigravity import AntigravityProvider
from llm_usage_monitor.providers.base import Provider
from llm_usage_monitor.providers.claude import ClaudeProvider
from llm_usage_monitor.providers.codex import CodexProvider
from llm_usage_monitor.providers.copilot import CopilotProvider
from llm_usage_monitor.providers.grok import GrokProvider
from llm_usage_monitor.providers.opencode import OpenCodeProvider
from llm_usage_monitor.quota import QuotaClient


def build_providers(
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    grok_dir: Path | None = None,
    opencode_db: Path | None = None,
    quota: QuotaClient | None = None,
) -> list[Provider]:
    live = quota or QuotaClient(ttl=10)
    return [
        ClaudeProvider(claude_dir, live),
        CodexProvider(codex_dir, live),
        GrokProvider(grok_dir, live),
        OpenCodeProvider(opencode_db, live),
        CopilotProvider(live),
        AntigravityProvider(),
    ]
