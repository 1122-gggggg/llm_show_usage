from typing import Protocol

from llm_usage_monitor.model import ProviderSnapshot


class Provider(Protocol):
    name: str

    def snapshot(self) -> ProviderSnapshot: ...
