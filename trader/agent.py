"""The live agent: sync data, replay what it slept through, take a decision,
persist everything, schedule the next wake-up.

Nothing here talks to a broker. The only network call in the whole project is a
public GET for daily price history; orders exist solely as rows in the local
database.
"""

from __future__ import annotations

import time as clock
from datetime import datetime, timezone

from . import data as market
from . import events as calendar
from . import news as newsfeed
from .config import Settings
from .engine import Engine, StepResult, SymbolView, market_stats
from .models import Decision
from .portfolio import Portfolio
from .scheduler import build_plan
from .store import Store
from .strategy import analyze

K_CASH = "cash"
K_DAY = "day"
K_DAY_START_EQUITY = "day_start_equity"
K_PEAK_EQUITY = "peak_equity"
K_LAST_BAR = "last_bar"
K_NEXT_RUN = "next_run"
K_HALTED = "halted"
K_PLAN = "plan"
K_SESSIONS = "sessions_seen"


def sync(
    store: Store, settings: Settings, bars: int | None = None, log=print
) -> dict[str, int]:
    """Refresh the local cache for every symbol, benchmark included.

    This always refetches and *replaces* the whole series rather than appending
    a tail, and that is not laziness. Equity prices are restated backwards: a
    split or a dividend rewrites every bar before it. Appending freshly
    adjusted bars onto stale ones would leave a step change at the join, and
    the agent would read that step as a gap that never happened — hitting
    stops, firing breakouts, all of it on an artefact of its own cache.

    A symbol that fails to fetch is reported and skipped rather than aborting
    the run: eighty tradable names beat none.
    """
    counts: dict[str, int] = {}
    for symbol in settings.data_universe:
        try:
            before = {
                b.open_time: b.close for b in store.load_bars(symbol, settings.interval)
            }
            fetched = market.fetch_history(
                symbol, settings, bars=market.ALL_HISTORY if bars is None else bars
            )
            restated = sum(
                1
                for b in fetched
                if b.open_time in before
                and abs(b.close - before[b.open_time]) > 0.005 * max(b.close, 1e-9)
            )
            counts[symbol] = store.replace_bars(symbol, settings.interval, fetched)
            if restated:
                log(f"  ~ {symbol}: {restated} past session(s) restated (split/dividend)")
        except market.DataError as exc:
            log(f"  ! {symbol}: {exc}")
            counts[symbol] = 0
    return counts


MAX_CATCH_UP = 10
"""Cap on sessions replayed in one tick — two trading weeks.

A longer outage is a cold start, not a catch-up. Silently replaying a month
would fill orders at opens that are long gone and supervise stops against
sessions whose outcome is already history."""


def pending_steps(
    store: Store, settings: Settings, max_catch_up: int = MAX_CATCH_UP
) -> list[tuple[int, dict[str, SymbolView]]]:
    """Every session that closed since the agent last looked, oldest first.

    Replaying them is not optional housekeeping. Two things happened while the
    agent slept and both live at the broker: resting stops could have been
    filled, and orders queued before it slept were executed at an opening
    print. Reading only the newest session would leave the account describing a
    position it no longer holds.
    """
    last_seen: dict[str, int] = store.get_state(K_LAST_BAR, {}) or {}

    analyses: dict[str, object] = {}
    index_of: dict[str, dict[int, int]] = {}
    for symbol in settings.data_universe:
        bars = store.load_bars(symbol, settings.interval)
        if len(bars) < settings.warmup_bars:
            continue
        analyses[symbol] = analyze(bars, settings)
        index_of[symbol] = {b.open_time: i for i, b in enumerate(bars)}

    if not index_of:
        return []

    stamps: set[int] = set()
    for symbol, idx in index_of.items():
        seen = last_seen.get(symbol)
        if seen is None:
            stamps.add(max(idx))  # cold start: begin at the present
        else:
            stamps.update(t for t in idx if t > seen)

    if not stamps:
        # Nothing new closed — a holiday, a weekend, or a second tick the same
        # evening. Re-evaluate the newest session anyway so a tick is never a
        # silent no-op, but `new_bar` keeps time from advancing twice.
        stamps = {max(max(idx) for idx in index_of.values())}

    steps = []
    for ts in sorted(stamps)[-max_catch_up:]:
        views = {
            symbol: SymbolView(
                analyses[symbol],
                index_of[symbol][ts],
                new_bar=last_seen.get(symbol) != ts,
            )
            for symbol in index_of
            if ts in index_of[symbol]
        }
        if views:
            steps.append((ts, views))
    return steps


def _roll_day(store: Store, now: datetime, equity: float) -> float:
    """Reset the daily loss budget on the calendar date change."""
    today = now.strftime("%Y-%m-%d")
    if store.get_state(K_DAY) != today:
        store.set_state(K_DAY, today)
        store.set_state(K_DAY_START_EQUITY, equity)
        return equity
    return store.get_state(K_DAY_START_EQUITY, equity)


def run_tick(
    store: Store,
    settings: Settings,
    now: datetime | None = None,
    do_sync: bool = True,
    log=print,
) -> StepResult:
    now = now or datetime.now(timezone.utc)
    ts = int(now.timestamp() * 1000)

    if do_sync:
        sync(store, settings, log=log)
        if settings.earnings_mode != "off":
            fresh = sum(calendar.sync(store, settings, log=log).values())
            log(f"  {fresh} new earnings date(s) cached")
        if settings.news_enabled:
            # Collected and archived. Not consulted: no rule in this agent
            # reads a headline. See `news.py`.
            fresh = sum(newsfeed.sync(store, settings, log=log).values())
            log(f"  {fresh} new headline(s) archived (not used for decisions)")

    steps = pending_steps(store, settings)
    if not steps:
        log("no symbol has enough history yet — run `sync` first")
        return StepResult(ts=ts, equity=0.0)

    cash = store.get_state(K_CASH, settings.initial_capital)
    portfolio = Portfolio(settings, cash=cash, positions=store.load_positions())
    views = steps[-1][1]
    prices = {s: v.analysis.closes[v.index] for s, v in views.items()}

    equity_before = portfolio.equity(prices)
    day_start = _roll_day(store, now, equity_before)
    peak = max(store.get_state(K_PEAK_EQUITY, settings.initial_capital), equity_before)
    seen = int(store.get_state(K_SESSIONS, 0))

    # The drawdown latch and the resting order queue both have to outlive the
    # process. An agent that forgets it halted resumes on the next wake-up, and
    # one that forgets its queue buys nothing it decided on yesterday.
    books = {}
    if settings.earnings_mode != "off":
        series = {
            symbol: view.analysis.bars for symbol, view in views.items()
        }
        books = calendar.calendars(store, settings, series)

    engine = Engine(
        settings,
        portfolio,
        on_trade=store.save_trade,
        halted=bool(store.get_state(K_HALTED, False)),
        pending=store.load_orders(),
        calendars=books,
    )

    if len(steps) > 1:
        log(f"  catching up on {len(steps) - 1} session(s) closed while asleep")

    result = StepResult(ts=ts, equity=equity_before)
    for position, (bar_ts, bar_views) in enumerate(steps):
        live = position == len(steps) - 1
        advanced = any(v.new_bar for v in bar_views.values())
        if advanced:
            seen += 1
        # Only the final session can be decided on: the agent is awake now, not
        # then. Whether it decides at all is the cadence, counted in sessions
        # so that a live run and a backtest land on the same ones.
        decide = live and (seen - 1) % settings.decide_every_n_sessions == 0
        stepped = engine.step(
            bar_views,
            ts if live else bar_ts,
            day_start,
            peak,
            decide=decide,
        )
        peak = max(peak, stepped.equity)
        result.decisions += stepped.decisions
        result.trades += stepped.trades
        result.orders = stepped.orders
        result.equity = stepped.equity
        if not decide and live:
            log(
                f"  not a decision session ({seen} seen, decides every"
                f" {settings.decide_every_n_sessions}) — stops and resting"
                " orders only"
            )

    # --- persist ---------------------------------------------------------
    store.set_state(K_CASH, portfolio.cash)
    open_now = set(portfolio.positions)
    for symbol in set(store.load_positions()) - open_now:
        store.delete_position(symbol)
    for pos in portfolio.positions.values():
        store.save_position(pos)
    store.save_orders(engine.pending)

    for d in result.decisions:
        if not d.equity:
            d.equity = result.equity
        store.save_decision(d)

    store.set_state(K_PEAK_EQUITY, peak)
    store.set_state(K_HALTED, engine.halted)
    store.set_state(K_SESSIONS, seen)
    store.set_state(
        K_LAST_BAR,
        {s: v.analysis.bars[v.index].open_time for s, v in views.items()},
    )
    store.save_equity(
        ts,
        result.equity,
        portfolio.cash,
        portfolio.exposure({s: v.analysis.closes[v.index] for s, v in views.items()}),
    )

    # --- schedule the next wake-up ---------------------------------------
    vol_ratio, near = market_stats(views, settings)
    plan = build_plan(
        now,
        len(portfolio.positions),
        len(engine.pending),
        vol_ratio,
        near,
        settings,
    )
    store.set_state(K_PLAN, {"decide_every": plan.decide_every, "reason": plan.reason})
    store.set_state(K_NEXT_RUN, plan.next_run.isoformat())

    log(
        f"[{now:%Y-%m-%d %H:%M UTC}] equity {result.equity:,.2f} "
        f"{settings.quote} | cash {portfolio.cash:,.2f} | "
        f"{len(portfolio.positions)} open | {len(engine.pending)} queued for the "
        f"open | next {plan.next_run:%Y-%m-%d %H:%M %Z}"
    )
    for d in result.decisions:
        if d.action != "HOLD":
            log(f"    {d.action:4} {d.symbol:6} @ {d.price:10.2f}  {d.reason}")
    for order in engine.pending:
        log(
            f"    queued {order.side:4} {order.symbol:6} for the next open"
            f"  ({order.reason})"
        )

    return result


def run_forever(store: Store, settings: Settings, log=print) -> None:
    """Sleep until the next closing bell, tick, repeat.

    Long sleeps are broken into short naps so a Ctrl-C lands promptly and a
    laptop resuming from suspend catches up rather than oversleeping.
    """
    log("agent started — paper trading only, no real orders will ever be sent")
    while True:
        try:
            run_tick(store, settings, log=log)
        except Exception as exc:  # a bad tick must not kill a long-running agent
            log(f"  ! tick failed: {type(exc).__name__}: {exc}")
            store.save_decision(
                Decision(
                    int(clock.time() * 1000),
                    "-",
                    "HOLD",
                    f"tick failed: {type(exc).__name__}: {exc}",
                    0.0,
                )
            )

        target = store.get_state(K_NEXT_RUN)
        wake = datetime.fromisoformat(target) if target else datetime.now(timezone.utc)
        while True:
            remaining = (wake - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            clock.sleep(min(remaining, 60))
