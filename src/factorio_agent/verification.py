"""Require sampled, sustained machine output instead of stock or build counts."""
from __future__ import annotations

import math


class FlowWindow:
    def __init__(self, rates: dict[str, float], *, minutes: int = 5, max_gap_ticks: int = 600):
        if not rates or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in rates.values()):
            raise ValueError("positive finite production rates are required")
        if type(minutes) is not int or minutes < 1 or type(max_gap_ticks) is not int or not 1 <= max_gap_ticks <= 600:
            raise ValueError("invalid verification duration or sample spacing")
        self.rates = dict(rates)
        self.minutes, self.max_gap_ticks = minutes, max_gap_ticks
        self.world_id = None
        self.samples: list[dict] = []
        self.reset_reason = None

    def add(self, *, world_id: str, tick: int, produced: dict[str, float],
            consumed: dict[str, float], supplies_healthy: bool) -> dict:
        """Counters must be machine-only output and normal downstream consumption.

        Callers obtain these from the live engine; inventory stock is not input.
        Negative/reset counters, missing samples and supply faults invalidate the
        continuous observation window. A new uninterrupted window can recover.
        """
        if not isinstance(world_id, str) or not world_id or type(tick) is not int or tick < 0:
            raise ValueError("world identity and integer game tick are required")
        for counts in (produced, consumed):
            if any(type(counts.get(item)) not in (int, float) or not math.isfinite(counts[item]) or counts[item] < 0
                   for item in self.rates):
                raise ValueError("every target requires a finite monotonic counter")
        reset = None
        if self.world_id not in (None, world_id):
            reset = "world_changed"
        elif self.samples:
            previous = self.samples[-1]
            if tick <= previous["tick"]:
                reset = "tick_rollback_or_duplicate"
            elif tick - previous["tick"] > self.max_gap_ticks:
                reset = "observation_gap"
            elif any(produced[item] < previous["produced"][item] or consumed[item] < previous["consumed"][item]
                     for item in self.rates):
                reset = "counter_reset"
        if not supplies_healthy:
            reset = "supply_fault"
        if reset:
            self.samples.clear()
            self.reset_reason = reset
        self.world_id = world_id
        if supplies_healthy:
            self.samples.append({"tick": tick, "produced": dict(produced), "consumed": dict(consumed)})
        # Retain a complete window and at most one leading boundary sample.
        # Irregular sampling can overshoot each minute boundary by one sample.
        # Retain enough real observations for all intervals without interpolation.
        retention = self.minutes * (3600 + self.max_gap_ticks)
        while len(self.samples) > 2 and self.samples[1]["tick"] <= tick - retention:
            self.samples.pop(0)
        return self.result()

    def result(self) -> dict:
        base = {"world_id": self.world_id, "required_minutes": self.minutes,
                "reset_reason": self.reset_reason, "target_rates_per_minute": self.rates}
        if len(self.samples) < 2 or self.samples[-1]["tick"] - self.samples[0]["tick"] < self.minutes * 3600:
            return {**base, "status": "sampling", "sample_count": len(self.samples),
                    "observed_ticks": self.samples[-1]["tick"] - self.samples[0]["tick"] if self.samples else 0}
        intervals = []
        end = self.samples[-1]
        # Use actual sampled interval lengths; never interpolate unobserved counts.
        for _ in range(self.minutes):
            candidates = [s for s in self.samples if s["tick"] <= end["tick"] - 3600]
            if not candidates:
                break
            start = candidates[-1]
            elapsed = end["tick"] - start["tick"]
            rates = {kind: {item: (end[kind][item] - start[kind][item]) * 3600 / elapsed
                            for item in self.rates} for kind in ("produced", "consumed")}
            passed = all(rates[kind][item] + 1e-9 >= target for item, target in self.rates.items()
                         for kind in ("produced", "consumed"))
            intervals.append({"from_tick": start["tick"], "to_tick": end["tick"], "rates_per_minute": rates,
                              "passed": passed})
            end = start
        intervals.reverse()
        passed = len(intervals) >= self.minutes and all(row["passed"] for row in intervals)
        return {**base, "status": "succeeded" if passed else "insufficient_throughput",
                "intervals": intervals, "from_tick": intervals[0]["from_tick"] if intervals else None,
                "to_tick": self.samples[-1]["tick"]}
