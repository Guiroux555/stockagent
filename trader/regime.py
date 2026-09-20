"""Market-wide regime: is it a market to be long in at all?

`strategy.py` already refuses to buy a name trading below its own EMA 200.
That is a per-symbol question, and it has a blind spot: in a market where
everything is bleeding, the one stock still above its average is usually the
one about to catch up, not the one that will keep going. This module asks the
question that no single symbol can answer — is the index itself healthy, and
how much of the universe is — and the engine uses it to stand aside entirely.

It gates entries only. Exits are never blocked: an agent that cannot close a
losing position in a bear market is exactly the wrong way round.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass
class MarketRegime:
    risk_on: bool
    breadth: float
    """Fraction of the *tradable* universe trading above its own long-term
    average. The anchor is excluded: it is the market, so counting it as one
    more healthy name would double-count the thing being measured."""
    reason: str


def assess(views: dict, settings: Settings) -> MarketRegime:
    scored = 0
    healthy = 0
    anchor_ok: bool | None = None
    tradable = set(settings.universe)

    for symbol, view in views.items():
        a, i = view.analysis, view.index
        if not a.ready(i):
            continue
        above = a.closes[i] > a.ema_regime[i]
        if symbol in tradable:
            scored += 1
            healthy += int(above)
        if symbol == settings.market_anchor:
            anchor_ok = above

    breadth = healthy / scored if scored else 1.0

    if settings.market_anchor:
        if anchor_ok is None:
            # Configured but absent from the data. Saying so beats gating on a
            # condition that is silently never evaluated.
            return MarketRegime(
                True,
                breadth,
                f"anchor {settings.market_anchor} has no data —"
                " market gate not applied",
            )
        if not anchor_ok:
            return MarketRegime(
                False, breadth, f"{settings.market_anchor} below its EMA200"
            )

    if settings.min_breadth > 0 and breadth < settings.min_breadth:
        return MarketRegime(
            False,
            breadth,
            f"only {healthy}/{scored} of the universe above its EMA200"
            f" ({breadth:.0%} < {settings.min_breadth:.0%})",
        )

    return MarketRegime(True, breadth, f"breadth {breadth:.0%}")
