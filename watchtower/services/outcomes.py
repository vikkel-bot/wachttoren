from __future__ import annotations

from watchtower.domain import EntrySignal, OutcomeEvaluation


class OutcomeTracker:
    def evaluate(
        self,
        signal: EntrySignal,
        entry_price: float,
        future_price: float,
        window: str,
        hit_threshold_pct: float = 0.2,
    ) -> OutcomeEvaluation:
        if entry_price <= 0:
            raise ValueError("entry_price must be greater than zero")
        if future_price <= 0:
            raise ValueError("future_price must be greater than zero")

        raw_return = ((future_price - entry_price) / entry_price) * 100
        if signal.direction == "short":
            raw_return *= -1
        if signal.direction == "neutral":
            raw_return = 0.0

        hit = raw_return >= hit_threshold_pct
        notes = "hit" if hit else "miss"
        return OutcomeEvaluation(
            signal_id=signal.id,
            window=window,
            entry_price=entry_price,
            future_price=future_price,
            return_pct=round(raw_return, 4),
            hit=hit,
            notes=notes,
        )
