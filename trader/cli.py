"""Command line interface.

python -m trader sync              fetch and cache daily sessions
python -m trader tick              take one decision now
python -m trader run               run continuously on its own schedule
python -m trader backtest          replay the cache through the same engine
python -m trader status            account, positions, resting orders
python -m trader watch             what the strategy sees right now
python -m trader log               recent decisions, including the HOLDs
python -m trader reset             wipe the account, keep the price history
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .agent import run_forever, run_tick, sync
from .backtest import run_backtest
from .config import DEFAULT_DB, Settings
from .report import decision_log, status, watchlist
from .store import Store

BANNER = (
    "stock-paper-trader — SIMULATION ONLY. "
    "No broker credentials, no real orders, ever."
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trader", description=BANNER)
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file")
    p.add_argument("--config", default=None, help="JSON settings overrides")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="download and cache daily sessions")
    s.add_argument(
        "--bars",
        type=int,
        default=None,
        help="sessions per symbol; the default takes everything since"
        " history_start, which is the only form that is certain to be"
        " correctly adjusted",
    )

    t = sub.add_parser("tick", help="take one decision now")
    t.add_argument("--no-sync", action="store_true", help="use cached data only")

    sub.add_parser("run", help="run continuously, one decision per session")

    b = sub.add_parser("backtest", help="replay cached sessions")
    b.add_argument("--days", type=int, default=None, help="limit to the last N days")
    b.add_argument("--trades", action="store_true", help="list every round trip")
    b.add_argument(
        "--cadence",
        type=int,
        default=None,
        metavar="N",
        help="decide every N sessions; defaults to the configured cadence",
    )
    b.add_argument(
        "--split",
        type=float,
        default=None,
        metavar="F",
        help="hold out the last 1-F of history: tune on the first F, judge on "
        "the rest (e.g. --split 0.6)",
    )
    b.add_argument(
        "--walk-forward",
        type=int,
        default=None,
        metavar="N",
        help="split the history into N consecutive windows and report each",
    )

    sub.add_parser("status", help="account summary")
    sub.add_parser("watch", help="current view of each name")

    lg = sub.add_parser("log", help="recent decisions")
    lg.add_argument("-n", type=int, default=25)

    r = sub.add_parser("reset", help="wipe the account, keep the price history")
    r.add_argument("--yes", action="store_true", help="skip the confirmation")

    sub.add_parser("config", help="print the effective settings")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.load(args.config)

    if args.command == "config":
        print(settings.to_json())
        return 0

    with Store(args.db) as store:
        if args.command == "sync":
            print(BANNER)
            counts = sync(store, settings, bars=args.bars)
            cached = sum(counts.values())
            missing = [s for s, n in counts.items() if not n]
            print(f"  {cached:,} sessions cached across {len(counts)} symbols")
            if missing:
                print(f"  not fetched: {', '.join(missing)}")
            return 0

        if args.command == "tick":
            run_tick(store, settings, do_sync=not args.no_sync)
            print()
            print(status(store, settings))
            return 0

        if args.command == "run":
            try:
                run_forever(store, settings)
            except KeyboardInterrupt:
                print("\nstopped")
            return 0

        if args.command == "backtest":
            start = None
            if args.days:
                cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
                start = int(cutoff.timestamp() * 1000)
            if args.split is not None:
                return _split_backtest(store, settings, args, start)
            if args.walk_forward is not None:
                return _walk_forward(store, settings, args)
            report = run_backtest(
                store, settings, start=start, decide_every=args.cadence
            )
            print(report.summary(settings))
            if args.trades:
                print()
                _print_trades(report, settings)
            return 0

        if args.command == "status":
            print(status(store, settings))
            return 0

        if args.command == "watch":
            print(watchlist(store, settings))
            return 0

        if args.command == "log":
            print(decision_log(store, args.n))
            return 0

        if args.command == "reset":
            if not args.yes:
                reply = input("wipe account, positions and trade history? [y/N] ")
                if reply.strip().lower() not in ("y", "yes"):
                    print("cancelled")
                    return 1
            store.reset_trading_state()
            print("trading state cleared; cached price history kept")
            return 0

    return 1


def _session_stamps(store: Store, settings: Settings) -> list[int]:
    stamps: set[int] = set()
    for symbol in settings.data_universe:
        stamps.update(b.open_time for b in store.load_bars(symbol, settings.interval))
    return sorted(stamps)


def _split_backtest(store: Store, settings: Settings, args, start) -> int:
    """Run the same engine twice: once on the earlier fraction of history, once
    on what was held back.

    Tuning on everything you have and quoting the result is how backtests come
    to promise returns that never arrive. Seeing the two halves side by side is
    the cheapest defence against it.
    """
    fraction = args.split
    if not 0.1 <= fraction <= 0.9:
        print("--split must be between 0.1 and 0.9")
        return 1

    stamps = _session_stamps(store, settings)
    if len(stamps) < 500:
        print("not enough cached history to split — run `python -m trader sync` first")
        return 1
    cut = stamps[int(len(stamps) * fraction)]

    print(">>> IN-SAMPLE (the part you are allowed to tune on)")
    print(
        run_backtest(
            store, settings, start=start, end=cut, decide_every=args.cadence
        ).summary(settings)
    )
    print()
    print(">>> OUT-OF-SAMPLE (the part that actually tells you something)")
    oos = run_backtest(store, settings, start=cut, decide_every=args.cadence)
    print(oos.summary(settings))
    if args.trades:
        print()
        _print_trades(oos, settings)
    return 0


def _walk_forward(store: Store, settings: Settings, args) -> int:
    """Consecutive windows, each judged on its own.

    A single total return can be one good year carrying a decade. Chopping the
    history into windows and printing every one of them, including the ugly
    ones, is what makes that visible.
    """
    windows = args.walk_forward
    if not 2 <= windows <= 40:
        print("--walk-forward must be between 2 and 40")
        return 1

    stamps = _session_stamps(store, settings)
    if len(stamps) < 500:
        print("not enough cached history — run `python -m trader sync` first")
        return 1

    size = len(stamps) // windows
    print(f"  WALK-FORWARD   {windows} windows of ~{size} sessions")
    print(f"  {'window':<26}{'return':>10}{'maxDD':>9}{'positions':>11}{'vs SPY':>10}")
    print("-" * 74)
    results = []
    for k in range(windows):
        lo = stamps[k * size]
        hi = stamps[min((k + 1) * size, len(stamps) - 1)]
        r = run_backtest(
            store, settings, start=lo, end=hi, decide_every=args.cadence
        )
        spy = next(
            (b for b in r.benchmarks if b.name.startswith("Buy & hold")), None
        )
        edge = r.total_return - (r.benchmark_return(spy) if spy else 0.0)
        results.append((r.total_return, edge))
        label = (
            f"{r.start:%Y-%m} -> {r.end:%Y-%m}" if r.start and r.end else "no data"
        )
        print(
            f"  {label:<26}{r.total_return:>+10.1%}{r.max_drawdown:>9.1%}"
            f"{len(r.round_trips):>11}{edge:>+10.1%}"
        )

    rets = sorted(r for r, _ in results)
    median = rets[len(rets) // 2]
    positive = sum(1 for r, _ in results if r > 0)
    beat = sum(1 for _, e in results if e > 0)
    print("-" * 74)
    print(
        f"  median {median:+.1%}   positive {positive}/{len(results)}"
        f"   beat SPY {beat}/{len(results)}   worst {min(rets):+.1%}"
    )
    return 0


def _print_trades(report, settings: Settings) -> None:
    from .models import utc

    print(
        f"  {'exit':<13}{'symbol':<8}{'entry':>11}{'exit':>11}"
        f"{'P&L':>12}{'ret':>9}  reason"
    )
    print("-" * 92)
    for t in report.trades:
        print(
            f"  {utc(t.exit_time):%Y-%m-%d}   {t.symbol:<8}"
            f"{t.entry_price:>11.2f}{t.exit_price:>11.2f}"
            f"{t.pnl:>+12,.2f}{t.return_pct:>+9.2%}  {t.reason}"
        )


if __name__ == "__main__":
    sys.exit(main())
