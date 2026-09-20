"""Cross-sectional relative strength.

`strategy.py` answers "is this symbol in an uptrend?" one symbol at a time.
That question has a blind spot: in a broad rally most of the index passes the
test, so the agent buys whichever happens to trigger first rather than
whichever is actually leading. This module answers the question the
single-symbol view cannot — "compared to the rest of the universe, how strong
is this one?" — and the engine can use it to ration its position slots to the
leaders.

Cross-sectional momentum is the one classical factor that was *designed* for a
universe this size: the literature runs it on hundreds of names, not on five
correlated coins, which is where the crypto version of this agent could never
give it a fair hearing. Whether eighty-seven names are enough to change that
verdict is measured in the README, not assumed here.

Everything here is a pure function of sessions already closed, like the rest of
the signal path.
"""

from __future__ import annotations

from .config import Settings
from .strategy import Analysis


def momentum_score(
    a: Analysis, i: int, lookback: int, vol_normalise: bool = True
) -> float | None:
    """Return over `lookback` sessions, optionally divided by current volatility.

    Without the normalisation the ranking is a volatility contest: the widest
    coin in the universe tops it in every rally and bottoms it in every dip,
    which is a measure of how much it moves, not of how well. Dividing by ATR%
    asks the more useful question — how much ground did this cover per unit of
    the risk it made you carry?
    """
    if i < 0:
        i += len(a.bars)
    j = i - lookback
    if j < 0 or i >= len(a.closes):
        return None

    past, now = a.closes[j], a.closes[i]
    if past <= 0:
        return None
    raw = now / past - 1.0

    if not vol_normalise:
        return raw
    atr_pct = a.atr_pct(i)
    if atr_pct <= 0:
        return None
    return raw / atr_pct


def score_universe(views: dict, settings: Settings) -> dict[str, float]:
    """Score every symbol with enough history to be scored."""
    scores: dict[str, float] = {}
    for symbol, view in views.items():
        score = momentum_score(
            view.analysis,
            view.index,
            settings.rs_lookback,
            settings.rs_vol_normalise,
        )
        if score is not None:
            scores[symbol] = score
    return scores


def rank_universe(views: dict, settings: Settings) -> dict[str, int]:
    """Map each symbol to its rank, 1 being the strongest.

    Ties are broken by symbol name so the ranking is reproducible: a backtest
    that reorders on a tie is a backtest that cannot be compared with itself.
    """
    scores = score_universe(views, settings)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {symbol: position for position, (symbol, _) in enumerate(ordered, 1)}


def passes(symbol: str, ranks: dict[str, int], settings: Settings) -> tuple[bool, str]:
    """Is `symbol` strong enough, relative to its peers, to be worth a slot?

    A symbol that could not be scored is let through rather than blocked: the
    single-symbol gates in `strategy.py` have already vetted it, and silently
    refusing to trade a symbol because its history is short is a failure mode
    that hides itself.
    """
    if settings.rs_top_k <= 0:
        return True, ""
    position = ranks.get(symbol)
    if position is None:
        return True, "unranked"
    if position <= settings.rs_top_k:
        return True, f"RS rank {position}/{len(ranks)}"
    return False, f"RS rank {position}/{len(ranks)}, outside top {settings.rs_top_k}"
