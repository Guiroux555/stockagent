"""The forward record: has the paper account actually made money, and does
that mean anything yet?

The backtest cannot answer this. Its parameters were chosen with hindsight over
the same thirty-six years it is scored on, and nothing measured on those years
can settle that, because the choosing happened there. Only a run forward, on
sessions that had not happened when the parameters were picked, can — and that
run is exactly what the Compute Module in the cupboard is doing.

So this file has one job and it is not cheerleading. It reports what the live
account did, against doing nothing and against SPY, and then says how far that
is from meaning anything. Three devices keep it honest:

1. **Every metric is computed by `backtest.Report`**, the same code and the
   same formulas that produce the numbers in the README. A forward test scored
   by its own kinder yardstick is not a test.
2. **The run is pre-registered.** The starting date and the starting capital
   are written down when the account is funded, not inferred afterwards from
   whichever point makes the curve look best.
3. **Restarts are in the ledger.** An agent with four abandoned runs behind it
   and one flattering one in progress is a very different claim from an agent
   with one run, and the difference should not depend on anyone remembering to
   mention it.

The arithmetic that matters most is the least welcome one. A t-statistic on an
equity curve grows with the square root of time: `t ≈ Sharpe × √years`. At an
annualised Sharpe of 1.0 — the region this strategy showed in backtest —
clearing the usual bar of t = 2 takes **four years**. There is no way to hurry
it, and an agent that looks brilliant after two months has told you almost
nothing.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .backtest import Benchmark, Report, _passive
from .config import Settings
from .models import utc
from .store import Store

TARGET_T = 2.0
"""The bar. Two standard errors is the convention this project uses everywhere
else, so the forward test is held to it too."""

MIN_POSITIONS = 30
"""Closed positions before the win rate and the expectancy are worth reading.

With a hit rate in the thirties the outcome of any ten trades is dominated by
whether one of them happened to be a runner. Thirty is not a threshold of
significance — it is the point below which the numbers are not even
descriptive."""

MIN_YEARS = 1.0
"""Elapsed time before any verdict is allowed, whatever the t says.

This gate exists because of a specific way the arithmetic lies. A Sharpe
measured over two months is estimated from forty-odd session returns and
carries an enormous standard error of its own, and those returns are not
independent: while a position is open its daily moves are the same trend
sampled repeatedly. Both effects push the t up.

A year is not long enough either — the honest number is four, see the module
docstring. It is the point below which the question is not worth asking."""

SESSIONS_PER_YEAR = 252
"""Trading sessions in a year, for turning the per-session Sharpe the
backtester reports into the annualised one this file talks in.

Annualised on purpose, and only here: the deflation in `backtest.py` is defined
on the raw statistic and its sample size, so that file must not scale it. This
one compares against a second agent on a different timeframe, which it cannot
do in per-session units."""


@dataclass(frozen=True)
class Run:
    """One funded run of the paper account."""

    id: int
    started: datetime
    ended: datetime | None
    initial: float
    final: float | None
    trades: int
    note: str

    @property
    def live(self) -> bool:
        return self.ended is None

    @property
    def total_return(self) -> float | None:
        if self.final is None or not self.initial:
            return None
        return self.final / self.initial - 1


def _run(row: dict) -> Run:
    return Run(
        id=int(row["id"]),
        started=utc(row["started_ts"]),
        ended=utc(row["ended_ts"]) if row["ended_ts"] else None,
        initial=float(row["initial"]),
        final=float(row["final"]) if row["final"] is not None else None,
        trades=int(row["trades"]),
        note=row["note"] or "",
    )


def ensure_run(store: Store, settings: Settings, ts: int, equity: float) -> Run:
    """Open a run if none is open, and back-date it if the account predates
    the ledger.

    The back-dating is the point. An account that has been trading for months
    and is only now getting a ledger must not have its record start today: that
    would silently drop whatever it did in the meantime, which is the cherry
    pick this whole file exists to prevent.
    """
    current = store.current_run()
    if current is None:
        first = store.first_equity()
        if first is not None:
            store.open_run(first[0], first[1], note="back-dated from the equity curve")
        else:
            store.open_run(ts, equity)
        current = store.current_run()
    return _run(current)


def _window_bars(store: Store, settings: Settings, first_ts: int, last_ts: int):
    """The cached sessions covering the live window, and nothing else.

    Bounded like a live tick, for the same reason: this command is run over ssh
    on the board itself.
    """
    span_days = max(last_ts - first_ts, 0) / 86_400_000
    want = int(span_days / 365 * SESSIONS_PER_YEAR) + 10
    series = {}
    last_index = {}
    for symbol in settings.data_universe:
        # `until` as well as `limit`: the tail of the *window*, not the tail of
        # the file.
        bars = [
            b
            for b in store.load_bars(
                symbol, settings.interval, limit=want, until=last_ts
            )
            if b.open_time >= first_ts
        ]
        if bars:
            series[symbol] = bars
            last_index[symbol] = len(bars) - 1
    return series, last_index


def _scaled(benchmark: Benchmark, scale: float) -> Benchmark:
    """`_passive` deploys `settings.initial_capital`; this account may have been
    funded with something else, and the comparison has to be like for like."""
    return Benchmark(
        benchmark.name,
        benchmark.final * scale,
        benchmark.drawdown,
        benchmark.coverage,
    )


def live_report(store: Store, settings: Settings, run: Run) -> Report:
    """Score the live account with the backtester's own yardstick."""
    started_ms = int(run.started.timestamp() * 1000)
    curve = [(ts, eq) for ts, eq in store.equity_curve() if ts >= started_ms]
    start_ms = curve[0][0] if curve else started_ms
    end_ms = curve[-1][0] if curve else start_ms
    trades = [t for t in store.load_trades() if t.exit_time >= start_ms]

    series, last_index = _window_bars(store, settings, start_ms, end_ms)
    timeline = sorted({b.open_time for bars in series.values() for b in bars})
    scale = run.initial / settings.initial_capital if settings.initial_capital else 1.0

    benchmarks = [
        _scaled(
            _passive(
                "Equal-weight basket",
                {s: b for s, b in series.items() if s in set(settings.universe)},
                settings,
                start_ms,
                last_index,
                timeline,
            ),
            scale,
        )
    ]
    if settings.benchmark in series:
        benchmarks.append(
            _scaled(
                _passive(
                    f"Buy & hold {settings.benchmark}",
                    {settings.benchmark: series[settings.benchmark]},
                    settings,
                    start_ms,
                    last_index,
                    timeline,
                ),
                scale,
            )
        )

    return Report(
        start=utc(start_ms),
        end=utc(end_ms),
        initial=run.initial,
        final=curve[-1][1] if curve else run.initial,
        trades=trades,
        equity_curve=curve,
        benchmarks=benchmarks,
        sessions=len(curve),
        decide_every=settings.decide_every_n_sessions,
    )


def years_for_significance(sharpe: float, target: float = TARGET_T) -> float | None:
    """How long a run at this annualised Sharpe takes to clear `target`
    standard errors.

    `t ≈ Sharpe × √years`, so the answer is `(target / Sharpe)²` — and it is
    quadratic, which is why halving the Sharpe quadruples the wait. `None` when
    the Sharpe is not positive, because then no amount of waiting helps.
    """
    if sharpe <= 0:
        return None
    return (target / sharpe) ** 2


@dataclass
class Record:
    """Everything the forward test can honestly say, plus what it cannot."""

    run: Run
    past: list[Run]
    report: Report
    settings: Settings

    @property
    def years(self) -> float:
        return self.report.days / 365.0

    @property
    def sharpe(self) -> float:
        """Annualised, so it is comparable with the other agent and with every
        Sharpe quoted outside this project."""
        return self.report.sharpe * math.sqrt(SESSIONS_PER_YEAR)

    @property
    def points(self) -> int:
        """Session returns recorded so far.

        Below about thirty of them a Sharpe is not a small number, it is an
        undefined one — which is a different thing from a bad result and has to
        read differently. `Report.sharpe` returns exactly 0.0 there rather than
        a figure, which is why this is checked before the sign is.
        """
        return len(self.report.session_returns)

    @property
    def t_stat(self) -> float:
        """The equity curve's own t: Sharpe scaled by the time elapsed."""
        return self.sharpe * math.sqrt(max(self.years, 0.0))

    @property
    def positions(self) -> int:
        return len(self.report.round_trips)

    @property
    def t_position(self) -> float:
        """Naive t on per-position P&L.

        Naive, and labelled so: positions opened in the same week are not
        independent draws, so this reads high. It is here because it fails for
        a different reason than the curve's t — one bad run of trades rather
        than one bad quarter — and a claim should have to survive both.
        """
        trips = self.report.round_trips
        if len(trips) < 3:
            return 0.0
        sd = statistics.stdev(trips)
        if sd == 0:
            return 0.0
        return statistics.mean(trips) / (sd / math.sqrt(len(trips)))

    @property
    def benchmark(self) -> Benchmark | None:
        """The one to beat: the index a real investor can buy, if it covers the
        window, and the basket otherwise."""
        available = [b for b in self.report.benchmarks if b.available]
        if not available:
            return None
        named = [b for b in available if self.settings.benchmark in b.name]
        return named[-1] if named else available[0]

    @property
    def benchmark_return(self) -> float | None:
        b = self.benchmark
        return self.report.benchmark_return(b) if b else None

    @property
    def gates(self) -> list[tuple[str, bool, str]]:
        """Everything that has to be true before "it works" is sayable.

        Printed as a checklist rather than collapsed into a verdict, because
        the useful information is *which* one is missing. One of these is
        nearly always the binding constraint, and it is usually time.
        """
        r = self.report
        bench = self.benchmark_return
        return [
            (
                "ran long enough",
                self.years >= MIN_YEARS,
                f"{self.years:.2f} of {MIN_YEARS:.0f} year(s)",
            ),
            (
                "enough positions",
                self.positions >= MIN_POSITIONS,
                f"{self.positions} of {MIN_POSITIONS}",
            ),
            ("made money", r.total_return > 0, f"{r.total_return:+.2%}"),
            (
                f"beat {self.benchmark.name}" if self.benchmark else "beat the index",
                bench is not None and r.total_return > bench,
                f"{r.total_return - bench:+.2%}" if bench is not None else "no benchmark",
            ),
            (
                "t on the equity curve",
                self.t_stat >= TARGET_T,
                f"{self.t_stat:.2f} of {TARGET_T:.1f}",
            ),
            (
                "t per position (naive)",
                self.t_position >= TARGET_T,
                f"{self.t_position:.2f} of {TARGET_T:.1f}",
            ),
        ]

    @property
    def established(self) -> bool:
        return all(passed for _, passed, _ in self.gates)

    @property
    def missing(self) -> list[str]:
        return [name for name, passed, _ in self.gates if not passed]

    @property
    def years_needed(self) -> float | None:
        """How long this run has to last before every gate can be clear.

        Floored at `MIN_YEARS`, because the t is not the only gate and a Sharpe
        of 5 measured over two months would otherwise announce that the work is
        already done. Whichever constraint binds last is the answer.
        """
        needed = years_for_significance(self.sharpe)
        if needed is None:
            return None
        return max(needed, MIN_YEARS)

    @property
    def eta(self) -> datetime | None:
        """When this run would clear the bar if it kept up its current Sharpe.

        Not a forecast. It is the answer to "how long do I have to leave this
        running?", and its job is to be discouragingly far away.
        """
        needed = self.years_needed
        if needed is None:
            return None
        return self.run.started + timedelta(days=needed * 365)

    def verdict(self) -> list[str]:
        r = self.report
        if self.established:
            return [
                "every gate above is clear. That is one strategy, run once,",
                "forward, on sessions that had not happened when its parameters",
                "were chosen — the strongest claim this project can make, and",
                "still not a promise about next year.",
            ]
        if self.points < 30:
            return [
                f"far too early — {r.days:.0f} days, {self.points} sessions.",
                "Below about thirty a Sharpe is not a small number, it is an",
                "undefined one, and a good first month is the single most",
                "misleading thing a strategy like this can show you.",
            ]
        if self.sharpe <= 0:
            return [
                f"not working so far. At a Sharpe of {self.sharpe:.2f} no amount",
                "of waiting clears the bar, so the question stops being 'how",
                "long?' and becomes 'why?' — start with `log`, then `backtest",
                "--days` over the same window to see if live and replay agree.",
            ]
        lines = [f"not yet — still missing: {', '.join(self.missing)}."]
        needed = self.years_needed
        if "t on the equity curve" in self.missing and needed is not None:
            lines.append(
                f"At the Sharpe shown so far ({self.sharpe:.2f}) the curve's own"
                f" t clears {TARGET_T:.1f} after"
            )
            lines.append(
                f"{needed:.1f} years"
                + (f", around {self.eta:%B %Y}." if self.eta else ".")
            )
            lines.append(
                "A Sharpe this young is mostly noise, so read that as an order"
            )
            lines.append("of magnitude, not an appointment.")
        else:
            lines.append(
                "Time alone will not close what is left — those gates are about"
            )
            lines.append("what the agent does, not how long it does it for.")
        return lines

    def to_dict(self) -> dict:
        """For the consolidated view across several agents."""
        r = self.report
        return {
            "agent": "stockagent",
            "quote": self.settings.quote,
            "started": self.run.started.isoformat(),
            "as_of": r.end.isoformat() if r.end else None,
            "days": r.days,
            "initial": r.initial,
            "equity": r.final,
            "pnl": r.final - r.initial,
            "total_return": r.total_return,
            "cagr": r.cagr,
            "max_drawdown": r.max_drawdown,
            "benchmark": self.benchmark.name if self.benchmark else None,
            "benchmark_return": self.benchmark_return,
            "sharpe": self.sharpe,
            "t_stat": self.t_stat,
            "t_position": self.t_position,
            "positions": self.positions,
            "win_rate": r.win_rate,
            "established": self.established,
            "missing": self.missing,
            "years_needed": self.years_needed,
            "eta": self.eta.isoformat() if self.eta else None,
            "past_runs": [
                {
                    "started": p.started.isoformat(),
                    "ended": p.ended.isoformat() if p.ended else None,
                    "total_return": p.total_return,
                    "trades": p.trades,
                    "note": p.note,
                }
                for p in self.past
            ],
            "daily_equity": _daily(r.equity_curve),
        }


def _daily(curve: list[tuple[int, float]]) -> list[tuple[str, float]]:
    """One point per UTC day, so curves from agents on different timeframes can
    be added together."""
    by_day: dict[str, float] = {}
    for ts, equity in curve:
        by_day[utc(ts).strftime("%Y-%m-%d")] = equity
    return sorted(by_day.items())


def record(store: Store, settings: Settings, now: datetime | None = None) -> Record:
    now = now or datetime.now(timezone.utc)
    current = store.current_run()
    if current is None:
        first = store.first_equity()
        ts = first[0] if first else int(now.timestamp() * 1000)
        equity = first[1] if first else settings.initial_capital
        run = Run(0, utc(ts), None, equity, None, 0, "not funded yet")
    else:
        run = _run(current)
    rows = [_run(r) for r in store.runs()]
    past = [r for r in rows if not r.live]
    return Record(
        run=run,
        past=past,
        report=live_report(store, settings, run),
        settings=settings,
    )


def report_text(store: Store, settings: Settings, now: datetime | None = None) -> str:
    rec = record(store, settings, now)
    r, q = rec.report, settings.quote
    span = (
        f"{r.start:%Y-%m-%d} -> {r.end:%Y-%m-%d} ({r.days:.0f} days)"
        if r.start and r.end
        else "nothing recorded yet"
    )

    lines = [
        "=" * 66,
        f"  TRACK RECORD  {span}",
        "  (paper account — simulation only, no real orders)",
        "=" * 66,
        f"  Funded            {rec.run.initial:12,.2f} {q}"
        + (f"   declared {rec.run.started:%Y-%m-%d %H:%M UTC}" if rec.run.id else ""),
        f"  Equity            {r.final:12,.2f} {q}",
        f"  P&L               {r.final - r.initial:+12,.2f} {q}   ({r.total_return:+.2%})",
        f"  Max drawdown      {r.max_drawdown:12.2%}",
        "",
    ]
    for b in r.benchmarks:
        if not b.available:
            continue
        lines.append(
            f"  {b.name:<19}{b.final:12,.2f} {q}"
            f"   ({r.benchmark_return(b):+.2%}, drawdown {b.drawdown:.1%})"
        )
    if rec.benchmark_return is not None:
        lines.append(
            f"  Edge vs benchmark {r.total_return - rec.benchmark_return:+12.2%}"
        )
    lines += [
        "",
        f"  Positions closed  {rec.positions:12d}"
        + (
            f"   (fewer than {MIN_POSITIONS}: the rates below are not yet descriptive)"
            if rec.positions < MIN_POSITIONS
            else ""
        ),
        f"  Win rate          {r.win_rate:12.1%}",
        f"  Expectancy/trade  {r.expectancy:12,.2f} {q}",
        "",
        "  IS IT LUCK?  (the only question this command exists to answer)",
        f"  Sharpe (annualised){rec.sharpe:11.2f}   from {rec.points} session(s)",
        "",
        "  Before 'it works' is sayable, all six of these:",
    ]
    for name, passed, detail in rec.gates:
        lines.append(f"   [{'x' if passed else ' '}] {name:<28} {detail}")
    lines.append("")
    for i, line in enumerate(rec.verdict()):
        lines.append(("  Verdict           " if i == 0 else "                    ") + line)

    if not rec.established:
        lines += [
            "",
            "  HOW LONG, REALLY   t grows with the square root of time, so",
            "  halving the Sharpe quadruples the wait:",
            "      Sharpe 0.5  ->  16 years      Sharpe 1.5  ->  1.8 years",
            "      Sharpe 1.0  ->   4 years      Sharpe 2.0  ->  1.0 year",
            "  The backtest was in the region of 1.0. Plan for years, not months.",
        ]

    if rec.past:
        lines += ["", "  THE LEDGER  (every funded run, so a restart cannot be quiet)"]
        for past in rec.past:
            lines.append(
                f"   #{past.id}  {past.started:%Y-%m-%d} -> {past.ended:%Y-%m-%d}"
                f"  {past.initial:11,.2f} -> {past.final:11,.2f}"
                f"  ({past.total_return:+.1%}, {past.trades} exits)"
                + (f"  {past.note}" if past.note else "")
            )
        lines.append(
            f"   #{rec.run.id}  {rec.run.started:%Y-%m-%d} -> now       "
            f"  {rec.run.initial:11,.2f} -> {r.final:11,.2f}"
            f"  ({r.total_return:+.1%}, {len(r.trades)} exits)   IN PROGRESS"
        )
        lines += [
            "",
            "  A record with abandoned runs behind it is a different claim from",
            "  one without. Read the whole column, not the last row.",
        ]

    lines.append("=" * 66)
    return "\n".join(lines)
