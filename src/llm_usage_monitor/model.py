from dataclasses import dataclass, field, replace
from datetime import datetime


@dataclass
class QuotaWindow:
    label: str
    used_percent: float | None
    resets_at: datetime | None
    detail: str | None = None


@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cached_input: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cached_input + self.cache_write + self.output

    def add(self, other: "TokenTotals") -> None:
        self.input += other.input
        self.output += other.output
        self.cached_input += other.cached_input
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning


@dataclass
class ProviderSnapshot:
    name: str
    plan: str | None
    quotas: list[QuotaWindow] = field(default_factory=list)
    today: TokenTotals = field(default_factory=TokenTotals)
    week: TokenTotals = field(default_factory=TokenTotals)
    sessions_today: int = 0
    last_event: datetime | None = None
    notes: list[str] = field(default_factory=list)
    by_model_today: dict[str, TokenTotals] = field(default_factory=dict)
    cost_today: float | None = None


def clone_snapshot(snapshot: ProviderSnapshot) -> ProviderSnapshot:
    return replace(
        snapshot,
        quotas=[replace(window) for window in snapshot.quotas],
        today=replace(snapshot.today),
        week=replace(snapshot.week),
        notes=list(snapshot.notes),
        by_model_today={
            model: replace(totals) for model, totals in snapshot.by_model_today.items()
        },
    )
