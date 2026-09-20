"""The live agent: sync data, replay what it slept through, take a decision,
persist everything, schedule the next wake-up.

Nothing here talks to a broker. The only network call in the whole project is a
public GET for daily price history; orders exist solely as rows in the local
database.
"""

from __future__ import annotations

import time as clock
from datetime import datetime, timedelta, timezone

from . import data as market
from . import events as calendar
from . import health, notify, track
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
K_LAST_TICK = "last_tick"
K_DEGRADED_SINCE = "degraded_since"
K_DEGRADED_COUNT = "degraded_count"


def sync(
    store: Store,
    settings: Settings,
    bars: int | None = None,
    log=print,
    heartbeat=None,
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

    The whole link going down is a different failure and gets a different
    answer: `Link` notices after the second name times out and the rest give up
    instantly, so an outage costs one round of timeouts rather than eighty-
    eight. That matters more than it sounds — a sync that takes half an hour to
    fetch nothing is an agent permanently busy failing to be online.

    `heartbeat` is called once per name, so a slow sync can keep a watchdog fed
    without the watchdog having to be slack enough to sleep through a hang.
    """
    counts: dict[str, int] = {}
    link = market.Link()
    for symbol in settings.data_universe:
        if heartbeat:
            heartbeat()
        counts.setdefault(symbol, 0)
        try:
            known = {b.open_time: b for b in store.load_bars(symbol, settings.interval)}
            fetched = market.fetch_history(
                symbol,
                settings,
                bars=market.ALL_HISTORY if bars is None else bars,
                link=link,
            )
            restated = sum(
                1
                for b in fetched
                if b.open_time in known
                and abs(b.close - known[b.open_time].close) > 0.005 * max(b.close, 1e-9)
            )
            counts[symbol] = store.replace_bars(
                symbol, settings.interval, fetched, known=known
            )
            if restated:
                log(f"  ~ {symbol}: {restated} past session(s) restated (split/dividend)")
        except market.OfflineError as exc:
            log(f"  ! {symbol}: {exc}")
            if link.down:
                # One timeout is a blip and the next name may well work. Two in
                # a row is the link, and the eighty-six names after it will fail
                # identically — at twenty seconds each.
                log(
                    f"  ! link is down — abandoning this sync after "
                    f"{len(counts)}/{len(settings.data_universe)} name(s)"
                )
                break
        except market.DataError as exc:
            log(f"  ! {symbol}: {exc}")
    for symbol in settings.data_universe:
        counts.setdefault(symbol, 0)
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
        # Bounded on purpose: a live tick needs the warm-up plus what it slept
        # through, and nothing older can change a number it computes today.
        # Reading all thirty-six years for every name is what makes this loop
        # the memory high-water mark of the whole process.
        bars = store.load_bars(symbol, settings.interval, limit=settings.live_window)
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


RETRY_BACKOFF = (300, 600, 1_200, 2_400, 3_600)
"""How long to wait before trying again after a tick that could not see the
market, in seconds: 5 minutes, then 10, 20, 40, and an hour from then on.

A fixed short retry would hammer a router that is down for a day; a fixed long
one would leave the agent blind for an hour after a thirty-second blip. The
ceiling is an hour because the agent only ever needs to be online once a day,
shortly after a closing bell, and an hour of margin against a bell is plenty."""


def retry_delay(consecutive: int) -> int:
    return RETRY_BACKOFF[min(max(consecutive, 1), len(RETRY_BACKOFF)) - 1]


def _schedule(store: Store, when: datetime) -> None:
    store.set_state(K_NEXT_RUN, when.isoformat())


def _degrade(store: Store, now: datetime) -> int:
    """Record that this tick could not see a current market, and say how many
    in a row that is."""
    tries = int(store.get_state(K_DEGRADED_COUNT, 0) or 0) + 1
    store.set_state(K_DEGRADED_COUNT, tries)
    if not store.get_state(K_DEGRADED_SINCE):
        store.set_state(K_DEGRADED_SINCE, now.isoformat())
    return tries


def _recover(store: Store) -> None:
    store.set_state(K_DEGRADED_COUNT, 0)
    store.set_state(K_DEGRADED_SINCE, "")


def run_tick(
    store: Store,
    settings: Settings,
    now: datetime | None = None,
    do_sync: bool = True,
    log=print,
    heartbeat=None,
) -> StepResult:
    now = now or datetime.now(timezone.utc)
    ts = int(now.timestamp() * 1000)

    if do_sync:
        prices = sync(store, settings, log=log, heartbeat=heartbeat)
        # Both of these are optional feeds and both are one request per name.
        # With the link down they would each repeat the half-hour of timeouts
        # the price sync just established was pointless, so they are asked only
        # when something actually came back.
        online = any(prices.values())
        if not online:
            log("  skipping the earnings and headline feeds — nothing is reachable")
        if online and settings.earnings_mode != "off":
            fresh = sum(calendar.sync(store, settings, log=log).values())
            log(f"  {fresh} new earnings date(s) cached")
        if online and settings.news_enabled:
            # Collected and archived. Not consulted: no rule in this agent
            # reads a headline. See `news.py`.
            fresh = sum(newsfeed.sync(store, settings, log=log).values())
            log(f"  {fresh} new headline(s) archived (not used for decisions)")

    steps = pending_steps(store, settings)
    if not steps:
        log("no symbol has enough history yet — run `sync` first")
        # A cold start with no link lands here. Come back in minutes rather
        # than leaving `next_run` unset, which would spin the run loop.
        _schedule(store, now + timedelta(seconds=retry_delay(_degrade(store, now))))
        return StepResult(ts=ts, equity=0.0)

    # Can the agent see a current market at all? Cached sessions look exactly
    # the same whether the link is up or the router died last Tuesday, and a
    # breakout read off last Tuesday's close becomes an order queued for an
    # open that has already happened. Exits are deliberately still supervised:
    # a stop is a promise made at entry, and the last known close is the honest
    # thing to measure it against.
    stale, age = health.is_stale(store, settings, now)

    cash = store.get_state(K_CASH, settings.initial_capital)
    portfolio = Portfolio(settings, cash=cash, positions=store.load_positions())
    views = steps[-1][1]
    prices = {s: v.analysis.closes[v.index] for s, v in views.items()}

    equity_before = portfolio.equity(prices)
    # Pre-register the run on the first tick, so the forward record starts
    # where the account starts rather than wherever someone later decides to
    # measure from. See `track.py`.
    track.ensure_run(store, settings, ts, equity_before)
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
    if stale:
        since = store.get_state(K_DEGRADED_SINCE) or now.isoformat()
        why = (
            f"data {health._human(age)} old" if age is not None else "cache empty"
        ) + f" (limit {health._human(health.max_data_age(settings))}), degraded since {since}"
        log(f"  ! no current market — entries suspended, stops still supervised: {why}")
        store.save_decision(Decision(ts, "-", "HOLD", f"entries suspended: {why}", 0.0))

    result = StepResult(ts=ts, equity=equity_before)
    for position, (bar_ts, bar_views) in enumerate(steps):
        live = position == len(steps) - 1
        advanced = any(v.new_bar for v in bar_views.values())
        if advanced:
            seen += 1
        # Only the final session can be decided on: the agent is awake now, not
        # then. Whether it decides at all is the cadence, counted in sessions
        # so that a live run and a backtest land on the same ones.
        decide = (
            live and not stale and (seen - 1) % settings.decide_every_n_sessions == 0
        )
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
        if not decide and live and not stale:
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
    upcoming = plan.next_run
    if stale:
        # Blind, so come back sooner than the cadence would — but never later
        # than the next closing bell, which stays the deadline whatever the
        # link is doing.
        upcoming = min(
            upcoming, now + timedelta(seconds=retry_delay(_degrade(store, now)))
        )
    else:
        _recover(store)
    store.set_state(K_PLAN, {"decide_every": plan.decide_every, "reason": plan.reason})
    store.set_state(K_LAST_TICK, now.isoformat())
    _schedule(store, upcoming)

    log(
        f"[{now:%Y-%m-%d %H:%M UTC}] equity {result.equity:,.2f} "
        f"{settings.quote} | cash {portfolio.cash:,.2f} | "
        f"{len(portfolio.positions)} open | {len(engine.pending)} queued for the "
        f"open | next {upcoming:%Y-%m-%d %H:%M %Z}"
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


CLOCK_WAIT_SECONDS = 600
"""How long to wait at startup for the clock to be set before giving up on it.

A Compute Module has no battery-backed clock. Power it up and it believes it is
whenever it last shut down, until NTP answers — which, after a power cut that
also took the router down, can be minutes. The whole schedule here is "the next
weekday closing bell, exchange-local", so ticking before then writes a next
wake-up in the past and prices sessions against a date that has not happened.

The wait is bounded rather than indefinite: a board with no internet at all
would otherwise never start, and an agent that is up and refusing to enter is
strictly more useful than one that is not up. Past the timeout it runs anyway,
and the staleness gate keeps it from acting on what it cannot verify."""


def await_clock(
    store: Store,
    settings: Settings,
    log=print,
    heartbeat=None,
    sleep=clock.sleep,
    timeout: float = CLOCK_WAIT_SECONDS,
) -> bool:
    """Block until the wall clock is believable, or until `timeout`."""
    ok, note = health.clock_is_sane(store, settings)
    if ok:
        return True
    log(f"  waiting for the clock to be set — {note}")
    waited = 0.0
    while waited < timeout:
        if heartbeat:
            heartbeat()
        sleep(min(5.0, timeout - waited))
        waited += 5.0
        ok, note = health.clock_is_sane(store, settings)
        if ok:
            log(f"  clock set after {waited:.0f}s — {note}")
            return True
    log(f"  ! clock still unset after {timeout:.0f}s ({note}) — running without entries")
    return False


def _status_line(store: Store, settings: Settings) -> str:
    """One line for `systemctl status`, on a board with no screen."""
    try:
        state = health.check(store, settings)
    except Exception as exc:  # status is a nicety; never let it end the run
        return f"status unavailable: {type(exc).__name__}"
    bits = [
        "next " + (f"{state.next_run:%a %H:%M %Z}" if state.next_run else "unplanned"),
        f"{state.positions} open",
    ]
    if state.orders:
        bits.append(f"{state.orders} queued")
    if state.age is not None:
        bits.append(f"data {health._human(state.age)} old")
    if state.degraded_since:
        bits.append(f"DEGRADED since {state.degraded_since:%Y-%m-%d %H:%M UTC}")
    return " | ".join(bits)


def run_forever(store: Store, settings: Settings, log=print) -> None:
    """Sleep until the next closing bell, tick, repeat.

    Long sleeps are broken into short naps so a Ctrl-C lands promptly, a laptop
    resuming from suspend catches up rather than oversleeping, and — the reason
    the nap length is no longer a constant — a systemd watchdog gets fed often
    enough to tell a sleeping agent apart from a wedged one.
    """
    log("agent started — paper trading only, no real orders will ever be sent")

    every = notify.watchdog_interval()
    nap = min(60.0, every) if every else 60.0
    ping = notify.watchdog if every else (lambda: False)

    # Sent before the first tick, not after: a cold start downloads thirty-six
    # years for eighty-eight names, and a `Type=notify` unit that stays quiet
    # that long is killed on TimeoutStartSec having done nothing wrong.
    notify.ready("starting")
    await_clock(store, settings, log=log, heartbeat=ping)

    failures = 0
    while True:
        try:
            run_tick(store, settings, log=log, heartbeat=ping)
            failures = 0
        except Exception as exc:  # a bad tick must not kill a long-running agent
            failures += 1
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
            # The schedule is written at the end of a tick, so a tick that
            # raised has left `next_run` in the past — and a wake-up time in
            # the past is a busy loop, which on a failure caused by the network
            # means hammering it. Back off explicitly instead.
            _schedule(
                store,
                datetime.now(timezone.utc) + timedelta(seconds=retry_delay(failures)),
            )

        ping()
        notify.status(_status_line(store, settings))

        target = store.get_state(K_NEXT_RUN)
        wake = datetime.fromisoformat(target) if target else datetime.now(timezone.utc)
        while True:
            remaining = (wake - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            clock.sleep(min(remaining, nap))
            ping()
