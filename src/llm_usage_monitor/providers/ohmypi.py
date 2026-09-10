from __future__ import annotations

from llm_usage_monitor.model import ProviderSnapshot
from llm_usage_monitor.ohmypi import (
    _FAMILY_DISPLAY,
    KNOWN_FAMILIES,
    OhmypiStore,
)


class OhmypiProvider:
    """One dashboard row per Oh My Pi provider family.

    All instances share a single :class:`OhmypiStore`, so a refresh cycle
    costs one ``omp usage --json`` call no matter how many families exist.
    Every instance uses the ``ohmypi`` key so ``--providers ohmypi`` keeps
    or drops the whole group at once.
    """

    key = "ohmypi"

    def __init__(self, family: str, store: OhmypiStore | None = None) -> None:
        if family not in KNOWN_FAMILIES:
            raise ValueError(f"不支援的 Oh My Pi 來源: {family}")
        self.family = family
        self.name = f"OMP {_FAMILY_DISPLAY[family]}"
        self._store = store or OhmypiStore()

    def snapshot(self) -> ProviderSnapshot:
        return self._store.snapshot_for(self.family)


def build_ohmypi_providers(store: OhmypiStore | None = None) -> list[OhmypiProvider]:
    shared = store or OhmypiStore()
    return [OhmypiProvider(family, shared) for family in KNOWN_FAMILIES]
