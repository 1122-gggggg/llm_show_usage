from typing import Protocol

from llm_usage_monitor.model import ProviderSnapshot


class Provider(Protocol):
    name: str
    key: str
    def snapshot(self) -> ProviderSnapshot: ...
