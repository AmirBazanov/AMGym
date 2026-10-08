"""In-memory LLM usage counters for /llm: per route today (local TIMEZONE day), Claude cost this month, the last
route that answered. Reset on restart; nothing here holds content, only numbers and route names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from gymbot.llm.claude import Usage


@dataclass
class Counter:
    calls: int = 0  # requests that got an HTTP answer with usage
    failures: int = 0  # failed attempts (errors, unusable answers)
    input: int = 0  # prompt tokens, cache included
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0


@dataclass
class LastAnswer:
    route: str
    at: datetime  # UTC


@dataclass
class LLMStats:
    tz: ZoneInfo
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    started: datetime = field(init=False)
    day: date = field(init=False)
    month: tuple[int, int] = field(init=False)
    today: dict[str, Counter] = field(default_factory=dict)  # route name -> counter
    month_cost: dict[str, float] = field(default_factory=dict)  # provider -> USD this month
    last: LastAnswer | None = None
    last_usage: Usage | None = None

    def __post_init__(self) -> None:
        self.started = self.now()
        local = self.started.astimezone(self.tz).date()
        self.day, self.month = local, (local.year, local.month)

    def _roll(self) -> None:
        local = self.now().astimezone(self.tz).date()
        if local != self.day:
            self.day, self.today = local, {}
        if (local.year, local.month) != self.month:
            self.month, self.month_cost = (local.year, local.month), {}

    def counter(self, route: str) -> Counter:
        self._roll()
        return self.today.setdefault(route, Counter())

    def record(self, route: str, provider: str, usage: Usage) -> None:
        c = self.counter(route)
        c.calls += 1
        c.input += usage.prompt
        c.output += usage.output
        c.cache_read += usage.cache_read
        c.cache_write += usage.cache_write
        c.cost += usage.cost
        self.month_cost[provider] = self.month_cost.get(provider, 0.0) + usage.cost
        self.last_usage = usage

    def failure(self, route: str) -> None:
        self.counter(route).failures += 1

    def answered(self, route: str) -> None:
        self.last = LastAnswer(route, self.now())

    def snapshot(self) -> tuple[date, dict[str, Counter], dict[str, float]]:
        self._roll()
        return self.day, dict(self.today), dict(self.month_cost)
