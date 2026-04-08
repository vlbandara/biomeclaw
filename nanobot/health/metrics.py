from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Counter:
    value: int = 0

    def inc(self, amount: int = 1) -> None:
        self.value += int(amount)


@dataclass
class Gauge:
    value: int = 0

    def set(self, value: int) -> None:
        self.value = int(value)


spawn_attempts = Counter()
spawn_success = Counter()
spawn_failures = Counter()

instance_running = Gauge()
instance_total = Gauge()

