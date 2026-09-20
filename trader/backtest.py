"""Replay cached sessions through the live engine.

The indicators are causal — a value at index `i` depends only on sessions up to
`i` — so the analysis is computed once over the full series and the cursor is
walked forward. That is equivalent to recomputing at every step.

The report prints two benchmarks, and the second one is the one that matters.
An equal-weight basket of this universe is a basket chosen with hindsight:
every name in it is a company that is still large and still listed today.
SPY is the thing an investor could actually have bought in 1993 without
knowing anything. Beating the basket is a weak claim; beating the index is
the claim worth making.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .engine import Engine, SymbolView
from .models import Bar, Trade, utc
from .portfolio import Portfolio
from .store import Store
from .strategy import analyze


@dataclass
class Benchmark:
    """What a passive holder of the same window would have made, and what they
    would have sat through to make it."""

    name: str
    final: float
    drawdown: float


@dataclass
class Report:
    start: datetime | None
    end: datetime | None
    initial: float
    final: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    exposure_curve: list[float] = field(default_factory=list)
    """Invested fraction of equity at each session close.

    Printed because it is the first thing that makes a low drawdown look less
    impressive: an agent that is half in cash should have half the drawdown,
    and saying so is the difference between a risk-adjusted claim and a
    flattering one."""
    benchmarks: list[Benchmark] = field(default_factory=list)
    sessions: int = 0
    decisions: int = 0
    decide_every: int = 1
    halted_at: int | None = None
    """When the drawdown breaker first latched, if it ever did. A run that
    freezes and reports nothing about it is a run that lies by omission."""

    @property
    def total_return(self) -> float:
        return self.final / self.initial - 1 if self.initial else 0.0

    @property
    def days(self) -> float:
        if not self.start or not self.end:
            return 0.0
        return max((self.end - self.start).total_seconds() / 86400, 1e-9)

    @property
    def cagr(self) -> float:
        if self.days < 1 or self.initial <= 0 or self.final <= 0:
            return 0.0
        return (self.final / self.initial) ** (365.25 / self.days) - 1

    @property
    def max_drawdown(self) -> float:
        peak, worst = -float("inf"), 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, 1 - eq / peak)
        return worst

    @property
    def avg_exposure(self) -> float:
        curve = self.exposure_curve
        return sum(curve) / len(curve) if curve else 0.0

    @property
    def return_per_drawdown(self) -> float:
        """CAGR per unit of worst drawdown — the crudest risk adjustment there
        is, and still more honest than comparing raw returns."""
        return self.cagr / self.max_drawdown if self.max_drawdown else 0.0

    def benchmark_cagr(self, b: Benchmark) -> float:
        if self.days < 1 or b.final <= 0 or self.initial <= 0:
            return 0.0
        return (b.final / self.initial) ** (365.25 / self.days) - 1

    def benchmark_return(self, b: Benchmark) -> float:
        return b.final / self.initial - 1 if self.initial else 0.0

    @property
    def round_trips(self) -> list[float]:
        """P&L per position, with any scale-out summed back into its entry.

        Every rate below is computed on these, not on exits. A partial exit
        books a second trade for the same entry — almost always a winning one —
        so counting exits would inflate the win rate for free and make the
        figures incomparable with a run that does not scale out.
        """
        grouped: dict[tuple[str, int], float] = {}
        for t in self.trades:
            key = (t.symbol, t.entry_time)
            grouped[key] = grouped.get(key, 0.0) + t.pnl
        return list(grouped.values())

    @property
    def partial_exits(self) -> int:
        return sum(1 for t in self.trades if t.partial)

    @property
    def wins(self) -> list[float]:
        return [p for p in self.round_trips if p > 0]

    @property
    def losses(self) -> list[float]:
        return [p for p in self.round_trips if p <= 0]

    @property
    def win_rate(self) -> float:
        trips = self.round_trips
        return len(self.wins) / len(trips) if trips else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(self.wins)
        pains = abs(sum(self.losses))
        if pains == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / pains

    @property
    def expectancy(self) -> float:
        trips = self.round_trips
        return sum(trips) / len(trips) if trips else 0.0

    @property
    def total_fees(self) -> float:
        return sum(t.fees for t in self.trades)

    @property
    def top_trade_share(self) -> float:
        """Share of the total profit produced by the single best trade.

        A trend follower legitimately earns most of its money from a few
        outliers, but when one trade *is* the result, the headline return
        describes a lucky draw rather than a repeatable edge. This number is
        printed so that fact cannot hide behind the CAGR.
        """
        gains = sum(self.wins)
        if gains <= 0:
            return 0.0
        return max(self.round_trips) / gains

    def pnl_excluding_best(self, k: int = 5) -> float:
        """Total profit with the `k` best positions removed — the pessimistic
        reading of the same history."""
        return sum(sorted(self.round_trips, reverse=True)[k:])

    @property
    def yearly_pnl(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for t in self.trades:
            year = utc(t.exit_time).year
            out[year] = out.get(year, 0.0) + t.pnl
        return dict(sorted(out.items()))

    @property
    def positive_years(self) -> tuple[int, int]:
        years = self.yearly_pnl
        return sum(1 for v in years.values() if v > 0), len(years)

    def summary(self, settings: Settings) -> str:
        q = settings.quote
        span = (
            f"{self.start:%Y-%m-%d} -> {self.end:%Y-%m-%d} ({self.days:.0f} days)"
            if self.start and self.end
            else "no data"
        )
        avg_win = sum(self.wins) / len(self.wins) if self.wins else 0.0
        avg_loss = sum(self.losses) / len(self.losses) if self.losses else 0.0
        pf = self.profit_factor
        cadence = (
            "every session"
            if self.decide_every == 1
            else f"every {self.decide_every} sessions"
        )
        lines = [
            "=" * 66,
            f"  BACKTEST  {span}",
            "=" * 66,
            f"  Universe          {len(settings.universe)} names,"
            f" benchmark {settings.benchmark}",
            f"  Timeframe         daily   sessions: {self.sessions}",
            f"  Cadence           decides {cadence}"
            f"  ({self.decisions} decision sessions)",
            "  Execution         signal at the close, fill at the next open",
            "",
            f"  Start equity      {self.initial:12.2f} {q}",
            f"  Final equity      {self.final:12.2f} {q}",
            f"  Total return      {self.total_return:12.2%}",
            f"  CAGR              {self.cagr:12.2%}",
            f"  Max drawdown      {self.max_drawdown:12.2%}",
            f"  CAGR per drawdown {self.return_per_drawdown:12.2f}",
            f"  Avg exposure      {self.avg_exposure:12.1%}"
            f"   (the rest sat in cash, earning nothing here)",
            "",
        ]

        for b in self.benchmarks:
            edge = self.total_return - self.benchmark_return(b)
            per_dd = self.benchmark_cagr(b) / b.drawdown if b.drawdown else 0.0
            lines += [
                f"  {b.name:<17} {b.final:12.2f} {q}"
                f"   ({self.benchmark_return(b):+.2%}, CAGR"
                f" {self.benchmark_cagr(b):+.2%})",
                f"    its drawdown    {b.drawdown:12.2%}"
                f"   CAGR per drawdown {per_dd:.2f}",
                f"    edge vs it      {edge:+12.2%}",
            ]

        lines += [
            "",
            f"  Positions         {len(self.round_trips):12d}"
            + (
                f"   ({self.partial_exits} scaled out,"
                f" {len(self.trades)} exits in all)"
                if self.partial_exits
                else ""
            ),
            f"  Win rate          {self.win_rate:12.1%}",
            f"  Profit factor     {pf:12.2f}"
            if pf != float("inf")
            else "  Profit factor              inf",
            f"  Expectancy/trade  {self.expectancy:12.2f} {q}",
            f"  Avg win           {avg_win:12.2f} {q}",
            f"  Avg loss          {avg_loss:12.2f} {q}",
            f"  Fees paid         {self.total_fees:12.2f} {q}",
        ]

        if self.round_trips:
            ex5 = self.pnl_excluding_best(5)
            good, total = self.positive_years
            lines += [
                "",
                "  CONCENTRATION  (how much of this was one lucky trade?)",
                f"  Best trade        {self.top_trade_share:12.1%} of all gains",
                f"  P&L less top 5    {ex5:+12.2f} {q}"
                + (
                    "   <-- the edge is the outliers, not the system"
                    if ex5 <= 0
                    else ""
                ),
                f"  Positive years    {good:12d} of {total}",
            ]
            if self.yearly_pnl:
                lines.append("  P&L by year")
                lines += _wrap_years(self.yearly_pnl)

        if self.halted_at is not None:
            lines += [
                "",
                "  !! HALTED  the drawdown breaker latched on "
                f"{utc(self.halted_at):%Y-%m-%d} and never released.",
                "             Everything after that date is a frozen account,",
                "             not a strategy result.",
            ]

        lines.append("=" * 66)
        return "\n".join(lines)


def _wrap_years(years: dict[int, float], per_line: int = 6) -> list[str]:
    items = [f"{y}: {v:+,.0f}" for y, v in years.items()]
    return [
        "      " + "  ".join(items[i : i + per_line])
        for i in range(0, len(items), per_line)
    ]


def _timeline(series: dict[str, list[Bar]]) -> list[int]:
    stamps: set[int] = set()
    for bars in series.values():
        stamps.update(b.open_time for b in bars)
    return sorted(stamps)


def run_backtest(
    store: Store,
    settings: Settings,
    start: int | None = None,
    end: int | None = None,
    decide_every: int | None = None,
) -> Report:
    """Replay the cache.

    Every session is stepped, always — resting stops and queued orders live at
    the broker and do not need the agent to be awake. `decide_every` controls
    only how often signals are read and the trailing stop is moved, which is
    the one thing a sleeping agent genuinely cannot do.
    """
    every = settings.decide_every_n_sessions if decide_every is None else max(1, decide_every)

    series = {
        sym: store.load_bars(sym, settings.interval) for sym in settings.data_universe
    }
    series = {s: b for s, b in series.items() if len(b) > settings.warmup_bars}
    if not series:
        raise RuntimeError(
            "not enough cached history — run `python -m trader sync` first"
        )

    analyses = {sym: analyze(bars, settings) for sym, bars in series.items()}
    index_of = {
        sym: {b.open_time: i for i, b in enumerate(bars)} for sym, bars in series.items()
    }

    portfolio = Portfolio(settings)
    trades: list[Trade] = []
    engine = Engine(settings, portfolio, on_trade=trades.append)

    curve: list[tuple[int, float]] = []
    exposure: list[float] = []
    day_start_equity = settings.initial_capital
    peak_equity = settings.initial_capital
    sessions = 0
    decisions = 0
    halted_at: int | None = None
    first_ts: int | None = None
    last_index: dict[str, int] = {}

    for ts in _timeline(series):
        if start and ts < start:
            continue
        if end and ts > end:
            break

        views: dict[str, SymbolView] = {}
        for sym, idx in index_of.items():
            i = idx.get(ts)
            if i is None or i < settings.warmup_bars:
                continue
            views[sym] = SymbolView(analysis=analyses[sym], index=i, new_bar=True)
            last_index[sym] = i
        if not views:
            continue

        if first_ts is None:
            first_ts = ts

        decide = sessions % every == 0
        prices = {s: v.analysis.closes[v.index] for s, v in views.items()}
        # One session is one day: the daily loss budget resets every step.
        day_start_equity = portfolio.equity(prices)

        result = engine.step(views, ts, day_start_equity, peak_equity, decide=decide)
        if engine.halted and halted_at is None:
            halted_at = ts
        elif not engine.halted:
            halted_at = None
        peak_equity = max(peak_equity, result.equity)
        curve.append((ts, result.equity))
        if result.equity > 0:
            closes = {s: v.analysis.closes[v.index] for s, v in views.items()}
            exposure.append(portfolio.exposure(closes) / result.equity)
        sessions += 1
        decisions += int(decide)

    # Close whatever is still open at the last close seen, so the final number
    # is realisable rather than a paper mark.
    if portfolio.positions:
        last_ts = curve[-1][0] if curve else (first_ts or 0)
        for sym in list(portfolio.positions):
            # `last_index`, not `closes[-1]`: with an `end` cutoff the file
            # holds sessions the backtest never saw, and marking out at one of
            # them would price the exit in the future.
            price = analyses[sym].closes[last_index[sym]]
            trades.append(portfolio.sell(sym, price, last_ts, "backtest end"))
        if curve:
            curve[-1] = (curve[-1][0], portfolio.cash)

    timeline = [ts for ts, _ in curve]
    benchmarks = [
        _passive(
            "Equal-weight basket",
            {s: b for s, b in series.items() if s in set(settings.universe)},
            settings,
            first_ts,
            last_index,
            timeline,
        )
    ]
    if settings.benchmark in series:
        benchmarks.append(
            _passive(
                f"Buy & hold {settings.benchmark}",
                {settings.benchmark: series[settings.benchmark]},
                settings,
                first_ts,
                last_index,
                timeline,
            )
        )

    return Report(
        start=utc(first_ts) if first_ts else None,
        end=utc(curve[-1][0]) if curve else None,
        initial=settings.initial_capital,
        final=portfolio.cash,
        trades=trades,
        equity_curve=curve,
        exposure_curve=exposure,
        benchmarks=benchmarks,
        sessions=sessions,
        decisions=decisions,
        decide_every=every,
        halted_at=halted_at,
    )


def _passive(
    name: str,
    series: dict[str, list[Bar]],
    settings: Settings,
    first_ts: int | None,
    last_index: dict[str, int],
    timeline: list[int],
) -> Benchmark:
    """Equal-weight the given names at the first evaluated session, pay one
    round of costs, and hold to the last session of the *same window*.

    Comparing a windowed strategy run against a full-history benchmark is how
    an in-sample report ends up quoting the wrong number to beat.

    Over thirty years the names do not all exist at the start, so each slice is
    bought at that name's first available session and its cash simply waits
    until then. That is what an investor holding this basket could actually
    have done, and it is the same constraint the strategy works under.
    """
    if first_ts is None or not series:
        return Benchmark(name, settings.initial_capital, 0.0)

    entries: dict[str, float] = {}
    exits: dict[str, float] = {}
    for sym, bars in series.items():
        opening = next((b for b in bars if b.open_time >= first_ts), None)
        end_i = last_index.get(sym)
        if opening is not None and end_i is not None and opening.close > 0:
            entries[sym] = opening.close
            exits[sym] = bars[end_i].close
    if not entries:
        return Benchmark(name, settings.initial_capital, 0.0)

    slice_size = settings.initial_capital / len(entries)
    cost = (1 - settings.fee_rate) * (1 - settings.slippage)
    qty = {sym: slice_size * cost / entry for sym, entry in entries.items()}
    final = sum(qty[sym] * exits[sym] * cost for sym in entries)

    prices = {
        sym: {b.open_time: b.close for b in bars}
        for sym, bars in series.items()
        if sym in qty
    }
    last: dict[str, float | None] = dict.fromkeys(qty, None)
    peak = 0.0
    worst = 0.0
    for ts in timeline:
        value = 0.0
        for sym, held in qty.items():
            price = prices[sym].get(ts, last[sym])
            if price is None:
                value += slice_size  # not listed yet: the slice waits in cash
            else:
                last[sym] = price
                value += held * price
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1 - value / peak)

    return Benchmark(name, final, worst)
