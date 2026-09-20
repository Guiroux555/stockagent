"""Short- and medium-term trend collection, per name, per sector, and market-wide.

`strategy.py` asks one question about one name on one horizon: has it closed
above the high of the previous twenty sessions? That is a good trigger and a
poor description. It cannot say whether the name has been climbing for six
months or for six days, whether its sector is leading or bleeding, or whether
the market is broadening or narrowing — and those are the questions a human
looking at the same screen would ask first.

This module answers them, over a ladder of horizons:

    1 week   5 sessions     what just happened
    1 month  21 sessions    the short term
    3 months 63 sessions    the medium term
    6 months 126 sessions   the medium term, confirmed
    12 months 252 sessions  context, not a signal

Every horizon is reported both raw and divided by the name's own ATR%. The
normalisation matters more than it looks: without it a ranking of trends is a
ranking of volatility, and the widest name in the universe leads every rally
and every selloff. Divided by ATR%, the question becomes "how much ground per
unit of risk carried", which is the one worth asking.

Everything here is a pure function of sessions already closed. Nothing in this
module trades; `engine.py` decides whether any of it is allowed to gate an
entry, and by default none of it is — see the README for what was measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .strategy import Analysis

HORIZONS: tuple[tuple[str, int], ...] = (
    ("1w", 5),
    ("1m", 21),
    ("3m", 63),
    ("6m", 126),
    ("12m", 252),
)

SHORT: tuple[str, ...] = ("1w", "1m")
MEDIUM: tuple[str, ...] = ("3m", "6m")


@dataclass
class Trend:
    """One name, seen across every horizon at once."""

    symbol: str
    price: float
    atr_pct: float
    returns: dict[str, float] = field(default_factory=dict)
    """Raw return over each horizon."""
    risk_adjusted: dict[str, float] = field(default_factory=dict)
    """The same return divided by ATR%, so names of different temperaments can
    be compared."""
    above_regime: bool = False
    ema_stack: bool = False
    """Fast above slow above trend above regime: every horizon of the moving
    average ladder pointing the same way. Rare, and the cleanest description of
    an established trend this codebase has."""
    off_high: float = 0.0
    """Drawdown from the highest close of the last 252 sessions."""

    def _mean(self, keys: tuple[str, ...]) -> float:
        vals = [self.risk_adjusted[k] for k in keys if k in self.risk_adjusted]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def short(self) -> float:
        return self._mean(SHORT)

    @property
    def medium(self) -> float:
        return self._mean(MEDIUM)

    @property
    def aligned(self) -> bool:
        """Short and medium term pulling the same way.

        A name that is up on the quarter and down on the month is not in an
        uptrend, it is in a pullback — which may be an opportunity or the
        beginning of the end, and this flag does not pretend to know which."""
        return self.short > 0 and self.medium > 0

    @property
    def label(self) -> str:
        if self.short > 0 and self.medium > 0:
            return "rising"
        if self.short <= 0 and self.medium <= 0:
            return "falling"
        if self.medium > 0:
            return "pullback"
        return "rebound"


@dataclass
class SectorTrend:
    """One sector, equal-weighted across the members with enough history."""

    name: str
    members: int
    returns: dict[str, float] = field(default_factory=dict)
    risk_adjusted: dict[str, float] = field(default_factory=dict)
    breadth: float = 0.0
    """Fraction of the sector trading above its own EMA 200."""

    def _mean(self, keys: tuple[str, ...]) -> float:
        vals = [self.risk_adjusted[k] for k in keys if k in self.risk_adjusted]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def short(self) -> float:
        return self._mean(SHORT)

    @property
    def medium(self) -> float:
        return self._mean(MEDIUM)


@dataclass
class MarketTrend:
    """The whole universe at once."""

    sectors: list[SectorTrend] = field(default_factory=list)
    breadth: float = 0.0
    breadth_short: float = 0.0
    """Fraction of the universe up over the short horizons — a faster reading
    than the EMA 200 breadth, and the one that turns first."""
    rising: int = 0
    falling: int = 0

    @property
    def leaders(self) -> list[SectorTrend]:
        return sorted(self.sectors, key=lambda s: -s.medium)


def _horizon_return(a: Analysis, i: int, span: int) -> float | None:
    j = i - span
    if j < 0 or i >= len(a.closes):
        return None
    past, now = a.closes[j], a.closes[i]
    if past <= 0:
        return None
    return now / past - 1.0


def trend_of(a: Analysis, symbol: str, settings: Settings, i: int = -1) -> Trend | None:
    """Every horizon for one name, or None if it has too little history.

    Returning None rather than a partially filled record is deliberate: a name
    that cannot be measured on the medium term must not be ranked against one
    that can, and silently scoring it as zero is how that happens.
    """
    if i < 0:
        i += len(a.bars)
    if not a.ready(i):
        return None

    atr_pct = a.atr_pct(i)
    trend = Trend(
        symbol=symbol,
        price=a.closes[i],
        atr_pct=atr_pct,
        above_regime=a.closes[i] > a.ema_regime[i],
        ema_stack=bool(
            a.ema_fast[i] > a.ema_slow[i] > a.ema_trend[i] > a.ema_regime[i]
        ),
    )
    for name, span in HORIZONS:
        raw = _horizon_return(a, i, span)
        if raw is None:
            continue
        trend.returns[name] = raw
        trend.risk_adjusted[name] = raw / atr_pct if atr_pct > 0 else 0.0

    window = a.closes[max(0, i - 252) : i + 1]
    high = max(window) if window else a.closes[i]
    trend.off_high = (1 - a.closes[i] / high) if high > 0 else 0.0

    # A name with no medium-term reading is not a trend, it is a fragment.
    if not any(k in trend.returns for k in MEDIUM):
        return None
    return trend


def collect(views: dict, settings: Settings) -> dict[str, Trend]:
    """Trends for every tradable name that has enough history."""
    tradable = set(settings.universe)
    out: dict[str, Trend] = {}
    for symbol, view in views.items():
        if symbol not in tradable:
            continue
        trend = trend_of(view.analysis, symbol, settings, view.index)
        if trend is not None:
            out[symbol] = trend
    return out


def by_sector(trends: dict[str, Trend], settings: Settings) -> list[SectorTrend]:
    """Equal-weight the names of each sector.

    Equal weight, not cap weight: the point of the split is to measure how
    independently the sectors move, and cap weighting would quietly turn the
    technology sector into a reading of four companies.
    """
    of = settings.sector_of
    grouped: dict[str, list[Trend]] = {}
    for symbol, trend in trends.items():
        sector = of.get(symbol)
        if sector:
            grouped.setdefault(sector, []).append(trend)

    out: list[SectorTrend] = []
    for name, _members in settings.sectors:
        rows = grouped.get(name)
        if not rows:
            continue
        sector = SectorTrend(name=name, members=len(rows))
        for horizon, _span in HORIZONS:
            raw = [t.returns[horizon] for t in rows if horizon in t.returns]
            adj = [t.risk_adjusted[horizon] for t in rows if horizon in t.risk_adjusted]
            if raw:
                sector.returns[horizon] = sum(raw) / len(raw)
                sector.risk_adjusted[horizon] = sum(adj) / len(adj)
        sector.breadth = sum(1 for t in rows if t.above_regime) / len(rows)
        out.append(sector)
    return out


def market(trends: dict[str, Trend], settings: Settings) -> MarketTrend:
    if not trends:
        return MarketTrend()
    rows = list(trends.values())
    return MarketTrend(
        sectors=by_sector(trends, settings),
        breadth=sum(1 for t in rows if t.above_regime) / len(rows),
        breadth_short=sum(1 for t in rows if t.short > 0) / len(rows),
        rising=sum(1 for t in rows if t.label == "rising"),
        falling=sum(1 for t in rows if t.label == "falling"),
    )


def sector_ranks(trends: dict[str, Trend], settings: Settings) -> dict[str, int]:
    """Map each sector to its rank on the medium term, 1 being the strongest.

    Ties break on the name so the ranking is reproducible: a backtest that
    reorders on a tie is a backtest that cannot be compared with itself.
    """
    sectors = by_sector(trends, settings)
    ordered = sorted(sectors, key=lambda s: (-s.medium, s.name))
    return {s.name: rank for rank, s in enumerate(ordered, 1)}


def passes(
    symbol: str,
    trends: dict[str, Trend],
    ranks: dict[str, int],
    settings: Settings,
) -> tuple[bool, str]:
    """Do the trend gates let this name through?

    Both gates are off by default. A name that cannot be measured is let
    through rather than blocked: the single-name gates in `strategy.py` have
    already vetted it, and silently refusing to trade something because its
    history is short is a failure mode that hides itself.
    """
    notes: list[str] = []
    trend = trends.get(symbol)

    if settings.min_medium_trend != 0.0:
        if trend is None:
            return True, "unmeasured"
        if trend.medium < settings.min_medium_trend:
            return False, (
                f"medium-term trend {trend.medium:.2f} below"
                f" {settings.min_medium_trend:.2f}"
            )
        notes.append(f"medium {trend.medium:+.2f}")

    if settings.sector_top_k > 0:
        sector = settings.sector_of.get(symbol)
        rank = ranks.get(sector) if sector else None
        if rank is not None:
            if rank > settings.sector_top_k:
                return False, (
                    f"{sector} ranks {rank}/{len(ranks)}, outside the top"
                    f" {settings.sector_top_k}"
                )
            notes.append(f"{sector} #{rank}")

    return True, "; ".join(notes)
