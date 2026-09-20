"""Command line interface.

python -m trader sync              fetch and cache daily sessions
python -m trader tick              take one decision now
python -m trader run               run continuously on its own schedule
python -m trader backtest          replay the cache through the same engine
python -m trader status            account, positions, resting orders
python -m trader trends            short and medium-term trends, by sector and name
python -m trader news              the headline archive, and what it is worth
python -m trader events            the earnings calendar, its coverage and its gaps
python -m trader watch             what the strategy sees right now
python -m trader log               recent decisions, including the HOLDs
python -m trader track             the forward record: is it making money yet?
python -m trader fund              set the virtual budget and pre-register the run
python -m trader health            clock, data, database — exits non-zero if sick
python -m trader backup            snapshot the database, keeping the last N
python -m trader reset             wipe the account, keep the price history
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import health as health_report
from . import track as track_report
from .agent import run_forever, run_tick, sync
from .backtest import run_backtest
from .config import DEFAULT_DB, Settings
from .report import (
    decision_log,
    event_board,
    news_board,
    status,
    trend_board,
    watchlist,
)
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
        "--trials",
        type=int,
        default=None,
        metavar="N",
        help="settings tried on this history, for the deflated Sharpe; count "
        "honestly, including the ones that were discarded",
    )
    b.add_argument(
        "--sr-variance",
        type=float,
        default=0.0004,
        metavar="V",
        help="variance of the per-session Sharpe across those trials",
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
    sub.add_parser("trends", help="short and medium-term trends, by sector and name")

    n = sub.add_parser("news", help="the headline archive")
    n.add_argument("--sync", action="store_true", help="fetch headlines first")
    n.add_argument("-n", type=int, default=30)

    e = sub.add_parser("events", help="the earnings calendar")
    e.add_argument(
        "--sync", action="store_true", help="fetch earnings dates from SEC EDGAR"
    )
    e.add_argument(
        "--gaps",
        action="store_true",
        help="compare opening gaps on earnings sessions with every other session",
    )

    tr = sub.add_parser(
        "track",
        help="the forward record since the account was funded, and whether it "
        "means anything yet",
    )
    tr.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="machine-readable, for deploy/portfolio.py across several agents",
    )

    fd = sub.add_parser(
        "fund",
        help="set the virtual budget and pre-register the run that measures it",
    )
    fd.add_argument("amount", type=float, help="virtual capital, in the quote currency")
    fd.add_argument(
        "--restart",
        action="store_true",
        help="close the run in progress and start a new one; the old run stays "
        "in the ledger and `track` keeps printing it",
    )

    sub.add_parser(
        "health",
        help="clock, data freshness, database integrity; exit code 1 if degraded",
    )

    bk = sub.add_parser("backup", help="snapshot the database to a dated file")
    bk.add_argument(
        "--dir",
        dest="directory",
        default=None,
        help="where snapshots go (default: a `backups` folder next to the database)",
    )
    bk.add_argument(
        "--keep",
        type=int,
        default=7,
        help="how many snapshots to keep; older ones are deleted (0 keeps all)",
    )

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
            if args.trials:
                dsr = report.deflated_sharpe(args.trials, args.sr_variance)
                print(
                    f"  Deflated Sharpe   {dsr:12.3f}"
                    f"   (session Sharpe {report.sharpe:.4f},"
                    f" {args.trials} trials, SR variance {args.sr_variance:g})"
                )
                print(
                    "  The probability the edge survives having tried that many"
                    " settings on one history."
                )
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

        if args.command == "trends":
            print(trend_board(store, settings))
            return 0

        if args.command == "events":
            from .events import gap_study
            from .events import sync as sync_events

            if args.sync:
                counts = sync_events(store, settings)
                print(f"  {sum(counts.values())} earnings date(s) cached")
            if args.gaps:
                print(gap_study(store, settings).summary(settings))
            else:
                print(event_board(store, settings))
            return 0

        if args.command == "news":
            if args.sync:
                from .news import sync as sync_news

                counts = sync_news(store, settings)
                print(f"  {sum(counts.values())} new headline(s) archived")
            print(news_board(store, settings, args.n))
            return 0

        if args.command == "log":
            print(decision_log(store, args.n))
            return 0

        if args.command == "track":
            if args.as_json:
                import json as _json

                print(
                    _json.dumps(
                        track_report.record(store, settings).to_dict(), indent=2
                    )
                )
            else:
                print(track_report.report_text(store, settings))
            return 0

        if args.command == "fund":
            return _fund(store, settings, args)

        if args.command == "health":
            state = health_report.check(store, settings)
            print(state.report())
            # Non-zero on degraded, so this is usable as a check from cron, a
            # systemd timer, or whatever watches the board from outside it.
            return 0 if state.ok else 1

        if args.command == "backup":
            return _backup(store, args)

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


def _fund(store: Store, settings: Settings, args) -> int:
    """Give the paper account a virtual budget, and write down when.

    Refusing to re-fund an account that has already traded is the whole point.
    A forward test whose starting line moves is not a forward test, so a
    restart has to be asked for explicitly — and even then the run it replaces
    stays in the ledger and `track` keeps printing it.
    """
    from .agent import K_CASH, K_DAY_START_EQUITY, K_PEAK_EQUITY

    if args.amount <= 0:
        print("a budget has to be positive")
        return 1

    now = datetime.now(timezone.utc)
    ts = int(now.timestamp() * 1000)
    traded = bool(store.load_trades() or store.load_positions() or store.load_orders())
    run = store.current_run()

    if traded and not args.restart:
        print(
            "this account has already traded. Re-funding it would move the "
            "starting line of the measurement,\nwhich is the one thing a "
            "forward test cannot survive.\n\n"
            "  python -m trader track              see what it has done so far\n"
            "  python -m trader fund --restart N   start again, keeping the old "
            "run in the ledger"
        )
        return 1

    if run is not None and not store.drop_empty_run():
        last = store.last_equity()
        store.close_run(
            last[0] if last else ts,
            last[1] if last else float(run["initial"]),
            len(store.load_trades()),
            note="restarted",
        )
    if traded:
        store.reset_trading_state()

    store.set_state(K_CASH, args.amount)
    store.set_state(K_PEAK_EQUITY, args.amount)
    store.set_state(K_DAY_START_EQUITY, args.amount)
    store.open_run(ts, args.amount)

    print(BANNER)
    print(f"  funded with {args.amount:,.2f} {settings.quote}, {now:%Y-%m-%d %H:%M UTC}")
    print("  the clock on the measurement starts now — `python -m trader track`")
    return 0


def _backup(store: Store, args) -> int:
    """Snapshot the database and rotate the old snapshots out.

    Kept deliberately dumb: a dated copy of one SQLite file, made with
    SQLite's own backup API so it is consistent even mid-write. Restoring is
    `cp`, which is the property that matters at 2am — see deploy/README.md.
    """
    directory = (
        Path(args.directory) if args.directory else Path(args.db).parent / "backups"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = store.backup(directory / f"{Path(args.db).stem}-{stamp}.db")

    size = dest.stat().st_size / 1e6
    print(f"  {dest}  ({size:.1f} MB)")

    if args.keep > 0:
        snapshots = sorted(directory.glob(f"{Path(args.db).stem}-*.db"))
        for old_file in snapshots[: -args.keep]:
            old_file.unlink()
            print(f"  removed {old_file.name}")
    return 0


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
        comparable = spy is not None and spy.available
        edge = r.total_return - r.benchmark_return(spy) if comparable else None
        results.append((r.total_return, edge))
        label = (
            f"{r.start:%Y-%m} -> {r.end:%Y-%m}" if r.start and r.end else "no data"
        )
        print(
            f"  {label:<26}{r.total_return:>+10.1%}{r.max_drawdown:>9.1%}"
            f"{len(r.round_trips):>11}"
            + (f"{edge:>+10.1%}" if edge is not None else f"{'n/a':>10}")
        )

    rets = sorted(r for r, _ in results)
    median = rets[len(rets) // 2]
    positive = sum(1 for r, _ in results if r > 0)
    judged = [e for _, e in results if e is not None]
    beat = sum(1 for e in judged if e > 0)
    print("-" * 74)
    print(
        f"  median {median:+.1%}   positive {positive}/{len(results)}"
        f"   beat SPY {beat}/{len(judged)}   worst {min(rets):+.1%}"
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
