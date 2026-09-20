"""The execution model, which is where an equity agent is most easily wrong.

Every test here defends one sentence: *the agent never trades at a price it had
already seen when it decided to trade.* On a 24/7 market that sentence is
almost free; on an exchange that publishes a closing print after the book has
shut, it has to be built and then guarded.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import bars_from_closes, trending_closes

from trader.engine import Engine, SymbolView
from trader.models import Order, Position
from trader.portfolio import Portfolio
from trader.strategy import analyze


def make_views(series: dict, settings, i: int, new_bar: bool = True):
    return {
        sym: SymbolView(analyze(bars, settings), i, new_bar=new_bar)
        for sym, bars in series.items()
    }


def run(settings, series: dict, upto: int | None = None, decide_every: int = 1):
    """Walk the engine forward one session at a time, as the backtester does."""
    pf = Portfolio(settings)
    engine = Engine(settings, pf)
    out = []
    length = min(len(b) for b in series.values())
    for i in range(settings.warmup_bars, upto or length):
        views = make_views(series, settings, i)
        result = engine.step(
            views, i, settings.initial_capital, settings.initial_capital,
            decide=(i - settings.warmup_bars) % decide_every == 0,
        )
        out.append((i, result))
    return engine, pf, out


@pytest.fixture
def one_name(settings):
    return {"AAA": bars_from_closes(trending_closes(seed=3))}


@pytest.fixture
def cfg(settings):
    """A single-name universe with the market gate out of the way."""
    return replace(settings, universe=("AAA",), market_anchor="", min_notional=0.0)


# --- the central invariant --------------------------------------------------


def test_every_fill_happens_at_an_opening_print(cfg, one_name):
    """Not at the close the signal was read from, and not at yesterday's open
    either: at the open of the very session the fill is recorded on."""
    _, _, steps = run(cfg, one_name)
    analysis = analyze(one_name["AAA"], cfg)
    fills = 0
    for i, result in steps:
        for d in result.decisions:
            if d.action != "BUY":
                continue
            fills += 1
            expected = analysis.opens[i] * (1 + cfg.slippage)
            assert d.price == pytest.approx(expected), (
                f"a buy on session {i} filled at {d.price}, not at that"
                f" session's {analysis.opens[i]} open"
            )
            assert d.price != pytest.approx(analysis.closes[i])
    assert fills, "no buy happened, so this test proved nothing"


def test_an_order_is_never_filled_on_the_session_that_created_it(cfg, one_name):
    """The guard that makes the invariant above hold even when a live tick
    re-reads a session it has already processed."""
    analysis = analyze(one_name["AAA"], cfg)
    i = cfg.warmup_bars + 10
    bar = analysis.bars[i]

    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    # An order stamped with *this* session's open belongs to the next one.
    engine.pending = [
        Order("AAA", "BUY", "test", analysis.closes[i], stop=analysis.closes[i] * 0.9,
              atr=1.0, created_ts=bar.open_time)
    ]
    engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)

    assert not pf.positions, "filled at an open that had already happened"
    assert engine.pending, "the order was dropped instead of being carried"


def test_the_queue_does_not_survive_two_sessions(cfg, one_name):
    """A market-on-open order is good for one open. Carrying it would have the
    agent buying a breakout it decided on last week."""
    analysis = analyze(one_name["AAA"], cfg)
    i = cfg.warmup_bars + 10
    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    engine.pending = [
        Order("AAA", "BUY", "test", 1e9, stop=1.0, atr=1e9,
              created_ts=analysis.bars[i - 1].open_time)
    ]
    engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert engine.pending == [], "an unfilled order stayed in the queue"


# --- gaps -------------------------------------------------------------------


def _queued_buy(cfg, series, i, signal_price, stop, atr):
    analysis = analyze(series["AAA"], cfg)
    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    engine.pending = [
        Order("AAA", "BUY", "test", signal_price, stop=stop, atr=atr,
              created_ts=analysis.bars[i - 1].open_time)
    ]
    result = engine.step(make_views(series, cfg, i), i, 1e5, 1e5, decide=False)
    return pf, result, analysis


def test_a_buy_that_gaps_up_past_the_guard_is_cancelled(cfg, one_name):
    i = cfg.warmup_bars + 10
    open_price = analyze(one_name["AAA"], cfg).opens[i]
    # Pretend the decision was taken far below this open: a huge overnight gap.
    pf, result, _ = _queued_buy(
        cfg, one_name, i, signal_price=open_price * 0.8, stop=open_price * 0.5, atr=1.0
    )
    assert not pf.positions
    assert any("gapped" in d.reason for d in result.decisions)


def test_a_buy_that_gaps_down_past_the_guard_is_cancelled_too(cfg, one_name):
    """Downward is not a bargain. If the open is far below the close that broke
    the high, the breakout has already failed."""
    i = cfg.warmup_bars + 10
    open_price = analyze(one_name["AAA"], cfg).opens[i]
    pf, result, _ = _queued_buy(
        cfg, one_name, i, signal_price=open_price * 1.2, stop=open_price * 0.5, atr=1.0
    )
    assert not pf.positions
    assert any("gapped" in d.reason for d in result.decisions)


def test_a_buy_that_opens_below_its_own_stop_is_cancelled(cfg, one_name):
    i = cfg.warmup_bars + 10
    open_price = analyze(one_name["AAA"], cfg).opens[i]
    pf, result, _ = _queued_buy(
        cfg, one_name, i, signal_price=open_price, stop=open_price * 1.01, atr=1e9
    )
    assert not pf.positions
    assert any("at or below the" in d.reason for d in result.decisions)


def test_a_small_gap_still_fills(cfg, one_name):
    i = cfg.warmup_bars + 10
    a = analyze(one_name["AAA"], cfg)
    pf, _, _ = _queued_buy(
        cfg, one_name, i,
        signal_price=a.opens[i] * 0.999, stop=a.opens[i] * 0.9, atr=a.opens[i] * 0.02,
    )
    assert "AAA" in pf.positions


# --- resting stops ----------------------------------------------------------


def _hold(cfg, series, i, stop, qty=10.0):
    pf = Portfolio(cfg)
    a = analyze(series["AAA"], cfg)
    pf.positions["AAA"] = Position(
        symbol="AAA", qty=qty, entry_price=a.closes[i - 1], entry_time=0,
        stop=stop, peak=a.closes[i - 1], atr_at_entry=1.0,
        initial_risk=max(a.closes[i - 1] - stop, 0.01), bars_held=50,
    )
    return pf, a


def test_a_resting_stop_fires_on_a_session_the_agent_slept_through(cfg, one_name):
    """The one thing an unattended agent gets for free: the order is at the
    broker, and the broker does not need it to be awake."""
    i = cfg.warmup_bars + 10
    pf, a = _hold(cfg, one_name, i, stop=0.0)
    pf.positions["AAA"].stop = a.lows[i] * 1.001
    engine = Engine(cfg, pf)
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert not pf.positions
    assert result.trades and result.trades[0].reason == "stop hit"


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(cfg, one_name):
    """Assuming otherwise is the single most common way a backtest invents
    money, and in equities every session starts with a gap."""
    i = cfg.warmup_bars + 10
    pf, a = _hold(cfg, one_name, i, stop=0.0)
    pf.positions["AAA"].stop = a.opens[i] * 1.05  # opened well below the stop
    engine = Engine(cfg, pf)
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(a.opens[i] * (1 - cfg.slippage))
    assert trade.exit_price < pf.sell_fill_price(a.opens[i] * 1.05)


def test_the_stop_beats_a_queued_sell_at_the_same_open(cfg, one_name):
    """Both want the same opening print. The resting order is already at the
    exchange; the one the agent sent last night is not."""
    i = cfg.warmup_bars + 10
    pf, a = _hold(cfg, one_name, i, stop=0.0)
    pf.positions["AAA"].stop = a.opens[i] * 1.05
    engine = Engine(cfg, pf)
    engine.pending = [
        Order("AAA", "SELL", "lost regime", a.closes[i - 1],
              created_ts=a.bars[i - 1].open_time)
    ]
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert len(result.trades) == 1
    assert result.trades[0].reason == "stop hit"


def test_a_stop_can_be_hit_on_the_session_the_position_was_opened(cfg, one_name):
    """Buying at the open and riding to the close regardless would hand the
    agent a free overnight it never had."""
    i = cfg.warmup_bars + 10
    a = analyze(one_name["AAA"], cfg)
    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    engine.pending = [
        Order("AAA", "BUY", "test", a.opens[i], stop=a.lows[i] * 1.001,
              atr=a.opens[i], created_ts=a.bars[i - 1].open_time)
    ]
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert not pf.positions
    assert any("same session" in t.reason for t in result.trades)


# --- cadence ----------------------------------------------------------------


def _a_session_the_position_is_held_through(cfg, series) -> int:
    """First session where the exit rules say hold, so the test is about the
    trailing stop and not about an exit that happens to fire."""
    from trader.strategy import exit_signal

    a = analyze(series["AAA"], cfg)
    for i in range(cfg.warmup_bars + 5, len(a.bars)):
        pf, _ = _hold(cfg, series, i, stop=1.0)
        if exit_signal(a, pf.positions["AAA"], cfg, i).action == "HOLD":
            return i
    raise AssertionError("the synthetic series never holds a position")


def test_a_session_without_a_decision_queues_nothing(cfg, one_name):
    i = cfg.warmup_bars + 10
    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert result.orders == []


def test_a_sleeping_agent_does_not_move_its_trailing_stop(cfg, one_name):
    """Moving a stop is a cancel and a replace. An agent that is not awake
    cannot send one, and pretending otherwise is how a low cadence gets to keep
    the protection it did not pay for."""
    i = _a_session_the_position_is_held_through(cfg, one_name)
    pf, _ = _hold(cfg, one_name, i, stop=1.0)
    engine = Engine(cfg, pf)
    engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
    assert pf.positions["AAA"].stop == 1.0, "a sleeping agent replaced its stop"

    engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=True)
    assert pf.positions["AAA"].stop > 1.0, "an awake agent failed to raise it"


# --- book keeping -----------------------------------------------------------


def test_the_position_limit_counts_orders_already_queued(settings):
    """Otherwise the agent queues twenty buys against three free slots and
    discovers the problem at the opening auction."""
    series = {
        f"S{k}": bars_from_closes(trending_closes(seed=k)) for k in range(1, 9)
    }
    cfg = replace(
        settings, universe=tuple(series), market_anchor="", max_concurrent=2,
        min_notional=0.0,
    )
    engine, pf, steps = run(cfg, series)
    for _, result in steps:
        assert len(result.orders) + len(pf.positions) <= cfg.max_concurrent + len(
            [o for o in result.orders if o.side == "SELL"]
        )


def test_positions_never_exceed_the_limit(settings):
    series = {
        f"S{k}": bars_from_closes(trending_closes(seed=k)) for k in range(1, 9)
    }
    cfg = replace(
        settings, universe=tuple(series), market_anchor="", max_concurrent=3,
        min_notional=0.0,
    )
    _, pf, steps = run(cfg, series)
    assert any(steps)
    assert len(pf.positions) <= 3


# --- the mode that exists to be measured ------------------------------------


def test_close_fill_mode_fills_at_the_close_and_is_off_by_default(settings, one_name):
    """Kept reachable so the README's claim about what it is worth can be
    reproduced; kept off because the closing print is published after the book
    is shut."""
    assert settings.execute_at_close is False

    cheating = replace(
        settings, universe=("AAA",), market_anchor="", min_notional=0.0,
        execute_at_close=True,
    )
    _, _, steps = run(cheating, one_name)
    analysis = analyze(one_name["AAA"], cheating)
    fills = [
        (i, d) for i, r in steps for d in r.decisions if d.action == "BUY"
    ]
    assert fills, "nothing was bought, so this test proved nothing"
    for i, d in fills:
        assert d.price == pytest.approx(analysis.closes[i] * (1 + cheating.slippage))


def test_close_fill_mode_leaves_nothing_queued(settings, one_name):
    cheating = replace(
        settings, universe=("AAA",), market_anchor="", min_notional=0.0,
        execute_at_close=True,
    )
    engine, _, steps = run(cheating, one_name)
    assert engine.pending == []
    assert all(r.orders == [] for _, r in steps)


# --- the earnings blackout --------------------------------------------------


def _with_calendar(cfg, series, event_sessions):
    """An engine whose calendar marks the given sessions as release days."""
    from trader.events import Calendar, EarningsDate

    bars = series["AAA"]
    sessions = [b.session for b in bars]
    rows = [
        EarningsDate("AAA", sessions[k], sessions[k], 0, "confirmed")
        for k in event_sessions
    ]
    pf = Portfolio(cfg)
    return pf, Engine(cfg, pf, calendars={"AAA": Calendar("AAA", sessions, rows)})


def _first_buy_session(cfg, series) -> int:
    """A session the strategy wants to buy on, so a gate test is about the
    gate and not about a session with no signal in it."""
    from dataclasses import replace as _replace

    plain = _replace(cfg, earnings_mode="off")
    for i in range(plain.warmup_bars, len(series["AAA"]) - 2):
        pf, engine = _with_calendar(plain, series, [])
        out = engine.step(make_views(series, plain, i), i, 1e5, 1e5, decide=True)
        if [o for o in out.orders if o.side == "BUY"]:
            return i
    raise AssertionError("the synthetic series never queued a buy")


def test_block_mode_refuses_to_queue_before_a_release(settings, one_name):
    from dataclasses import replace as _replace

    cfg = _replace(
        settings, universe=("AAA",), market_anchor="", min_notional=0.0,
        earnings_mode="block", earnings_date_source="scheduled",
        earnings_announce_horizon=30,
    )
    i = _first_buy_session(cfg, one_name)
    pf, engine = _with_calendar(cfg, one_name, [i + 1])
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=True)
    assert not [o for o in result.orders if o.side == "BUY"]
    assert any("stood down for earnings" in d.reason for d in result.decisions)


def test_reduce_mode_queues_a_smaller_order_instead(settings, one_name):
    """More surgical than refusing: the breakout is still a breakout, it is the
    unbounded gap that is the problem."""
    from dataclasses import replace as _replace

    cfg = _replace(
        settings, universe=("AAA",), market_anchor="", min_notional=0.0,
        earnings_mode="reduce", earnings_date_source="scheduled",
        earnings_announce_horizon=30,
    )
    found = _first_buy_session(cfg, one_name)
    pf, engine = _with_calendar(cfg, one_name, [found + 1])
    result = engine.step(make_views(one_name, cfg, found), found, 1e5, 1e5, decide=True)
    buys = [o for o in result.orders if o.side == "BUY"]
    assert buys and buys[0].size_factor == cfg.earnings_size_factor


def test_a_reduced_order_really_buys_less(settings, one_name):
    """The factor has to reach the sizing, not just ride on the order."""
    from dataclasses import replace as _replace

    from trader.models import Order

    cfg = _replace(settings, universe=("AAA",), market_anchor="", min_notional=0.0)
    a = analyze(one_name["AAA"], cfg)
    i = cfg.warmup_bars + 10
    sizes = []
    for factor in (1.0, 0.5):
        pf = Portfolio(cfg)
        engine = Engine(cfg, pf)
        engine.pending = [
            Order("AAA", "BUY", "test", a.opens[i], stop=a.opens[i] * 0.9,
                  atr=a.opens[i] * 0.02, size_factor=factor,
                  created_ts=a.bars[i - 1].open_time)
        ]
        engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=False)
        sizes.append(pf.positions["AAA"].qty if "AAA" in pf.positions else 0)
    assert sizes[1] < sizes[0]


def test_the_blackout_never_blocks_an_exit(settings, one_name):
    """The invariant of this project: an agent that cannot close a losing
    position because of a calendar is exactly the wrong way round."""
    from dataclasses import replace as _replace

    cfg = _replace(
        settings, universe=("AAA",), market_anchor="", min_notional=0.0,
        earnings_mode="block", earnings_date_source="scheduled",
        earnings_announce_horizon=30,
    )
    i = cfg.warmup_bars + 40
    pf, engine = _with_calendar(cfg, one_name, [i, i + 1])
    a = analyze(one_name["AAA"], cfg)
    pf.positions["AAA"] = Position(
        symbol="AAA", qty=10, entry_price=a.closes[i - 1], entry_time=0,
        stop=a.lows[i] * 1.001, peak=a.closes[i - 1], atr_at_entry=1.0,
        initial_risk=1.0, bars_held=50,
    )
    result = engine.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=True)
    assert result.trades and result.trades[0].reason == "stop hit"


def test_an_off_blackout_changes_nothing(settings, one_name):
    from dataclasses import replace as _replace

    cfg = _replace(settings, universe=("AAA",), market_anchor="", min_notional=0.0)
    i = cfg.warmup_bars + 40
    plain = Engine(cfg, Portfolio(cfg))
    _, gated = _with_calendar(cfg, one_name, [i + 1])
    a = plain.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=True)
    b = gated.step(make_views(one_name, cfg, i), i, 1e5, 1e5, decide=True)
    assert [o.symbol for o in a.orders] == [o.symbol for o in b.orders]
