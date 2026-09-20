#!/usr/bin/env python3
"""The two agents as one virtual portfolio.

    python3 portfolio.py /var/lib/cryptoagent/live.db /var/lib/stockagent/live.db

Each agent answers "am I making money?" on its own, with its own benchmark, via
`python -m trader track`. This answers the question the person who owns the
board actually asked: **together**, over the whole time they have been running,
have they made anything — and does it mean anything yet?

It reads the databases directly and read-only, so it works across both repos
without importing either, and it is safe to run while the agents are writing.

Two things it assumes, stated rather than buried: that one USDT is one USD, and
that an agent funded later was sitting in cash until it started. Both are
harmless here and neither is hidden.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

TARGET_T = 2.0
MIN_YEARS = 1.0
MIN_EXITS = 30


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")


class Agent:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.parent.name if path.parent.name != "/" else path.stem
        conn = _open(path)
        try:
            self.runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_ts")]
            self.curve = [
                (r["ts"], r["equity"])
                for r in conn.execute("SELECT ts, equity FROM equity_curve ORDER BY ts")
            ]
            live = [dict(r) for r in self.runs if r["ended_ts"] is None]
            self.run = live[-1] if live else None
            start = (
                self.run["started_ts"]
                if self.run
                else (self.curve[0][0] if self.curve else 0)
            )
            # Only the exits of the run in progress: the abandoned ones are
            # listed separately and must not be counted towards this record.
            self.exits = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE exit_time >= ?", (start,)
            ).fetchone()[0]
        finally:
            conn.close()

        self.abandoned = [r for r in self.runs if r["ended_ts"] is not None]
        self.curve = [(ts, eq) for ts, eq in self.curve if ts >= start]
        self.initial = float(self.run["initial"]) if self.run else (
            self.curve[0][1] if self.curve else 0.0
        )
        self.equity = self.curve[-1][1] if self.curve else self.initial

    @property
    def started(self) -> str:
        return _day(self.run["started_ts"]) if self.run else "—"

    @property
    def total_return(self) -> float:
        return self.equity / self.initial - 1 if self.initial else 0.0

    def daily(self) -> dict[str, float]:
        """One point per UTC day: the two agents run on different timeframes,
        and a day is the shortest period that is not an artefact of either."""
        out: dict[str, float] = {}
        for ts, equity in self.curve:
            out[_day(ts)] = equity
        return out


def combined(agents: list[Agent]) -> list[tuple[str, float]]:
    """Both accounts added up, day by day, from the day the last one started.

    The starting day matters more than it looks. Run the sum from the *first*
    agent's start and the second one contributes a flat line of untouched cash
    for however long it was not running — which halves the measured volatility
    and inflates the combined Sharpe for no reason but bookkeeping. The
    portfolio only exists once both accounts do.
    """
    starts = [sorted(a.daily())[0] for a in agents if a.daily()]
    if not starts:
        return []
    common = max(starts)
    days = sorted({d for a in agents for d in a.daily() if d >= common})
    series = []
    for day in days:
        total = 0.0
        for agent in agents:
            points = agent.daily()
            past = [d for d in points if d <= day]
            total += points[past[-1]] if past else agent.initial
        series.append((day, total))
    return series


def _returns(series: list[tuple[str, float]]) -> list[float]:
    return [
        series[i][1] / series[i - 1][1] - 1
        for i in range(1, len(series))
        if series[i - 1][1] > 0
    ]


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv]
    if not paths:
        print(__doc__.strip())
        return 1
    missing = [p for p in paths if not p.exists()]
    if missing:
        print("no such database: " + ", ".join(str(p) for p in missing))
        return 1

    agents = [Agent(p) for p in paths]
    seen: dict[str, int] = {}
    for agent in agents:  # two agents installed side by side can share a folder name
        seen[agent.name] = seen.get(agent.name, 0) + 1
        if seen[agent.name] > 1:
            agent.name = f"{agent.name}:{agent.path.stem}"
    series = combined(agents)
    if not series:
        print("nothing recorded yet — the agents have not taken a decision")
        return 1

    initial = series[0][1]
    equity = series[-1][1]
    ret = equity / initial - 1 if initial else 0.0
    days = (
        datetime.strptime(series[-1][0], "%Y-%m-%d")
        - datetime.strptime(series[0][0], "%Y-%m-%d")
    ).days or 1
    years = days / 365.0

    returns = _returns(series)
    sd = statistics.stdev(returns) if len(returns) > 1 else 0.0
    sharpe = statistics.mean(returns) / sd * math.sqrt(365) if sd else 0.0
    t = sharpe * math.sqrt(years)

    peak, drawdown = -float("inf"), 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, 1 - value / peak)

    print("=" * 66)
    print(f"  VIRTUAL PORTFOLIO  {series[0][0]} -> {series[-1][0]} ({days} days)")
    print("  (paper accounts — simulation only, no real orders)")
    print("=" * 66)
    print(
        f"  {'agent':<16}{'from':>12}{'funded':>12}{'equity':>12}"
        f"{'P&L':>12}{'return':>9}{'exits':>7}"
    )
    for agent in agents:
        print(
            f"  {agent.name:<16}{agent.started:>12}{agent.initial:>12,.2f}"
            f"{agent.equity:>12,.2f}{agent.equity - agent.initial:>+12,.2f}"
            f"{agent.total_return:>+9.2%}{agent.exits:>7d}"
        )
    print("  " + "-" * 69)
    funded = sum(a.initial for a in agents)
    held = sum(a.equity for a in agents)
    print(
        f"  {'TOTAL':<16}{'':>12}{funded:>12,.2f}{held:>12,.2f}"
        f"{held - funded:>+12,.2f}"
        f"{(held / funded - 1) if funded else 0:>+9.2%}"
        f"{sum(a.exits for a in agents):>7d}"
    )
    print()
    print(f"  TOGETHER  (from {series[0][0]}, the day the last account started)")
    print(f"  Max drawdown      {drawdown:10.2%}")
    print(f"  Sharpe (daily)    {sharpe:10.2f}")
    print(f"  t                 {t:10.2f}   bar: {TARGET_T:.1f}")

    exits = sum(a.exits for a in agents)
    missing = []
    if years < MIN_YEARS:
        missing.append(f"ran long enough ({years:.2f} of {MIN_YEARS:.0f} year(s))")
    if exits < MIN_EXITS:
        missing.append(f"enough exits ({exits} of {MIN_EXITS})")
    if ret <= 0:
        missing.append(f"made money ({ret:+.2%})")
    if t < TARGET_T:
        missing.append(f"t on the curve ({t:.2f} of {TARGET_T:.1f})")

    if not missing:
        print("  Verdict           the combined account clears every gate here.")
        print("                    Each agent's own checklist, below, is the real")
        print("                    test — this one has no benchmark in it.")
    elif sharpe <= 0:
        print("  Verdict           not working. No amount of waiting fixes a")
        print("                    Sharpe that is not positive.")
    else:
        print(f"  Verdict           not yet — missing {missing[0]}.")
        for item in missing[1:]:
            print(f"                    and {item}.")
        if t < TARGET_T:
            needed = max((TARGET_T / sharpe) ** 2, MIN_YEARS)
            print(f"                    At this Sharpe the t needs {needed:.1f} years.")

    print()
    print("  Two caveats this view cannot remove. Two agents are not two")
    print("  independent bets on the same question — crypto and US equities")
    print("  correlate in exactly the weeks it matters — and there is no")
    print("  benchmark here: beating buy-and-hold is asked agent by agent.")

    abandoned = [(a, r) for a in agents for r in a.abandoned]
    if abandoned:
        print()
        print("  ABANDONED RUNS  (a record with these behind it is a different claim)")
        for agent, run in abandoned:
            change = run["final"] / run["initial"] - 1 if run["initial"] else 0.0
            print(
                f"   {agent.name:<14}{_day(run['started_ts'])} -> "
                f"{_day(run['ended_ts'])}  {change:+.1%}"
                + (f"   {run['note']}" if run["note"] else "")
            )

    print()
    print("  Each agent's own benchmark and gate checklist:")
    for agent in agents:
        print(f"    python -m trader --db {agent.path} track")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
