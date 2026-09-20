"""The decision step, shared verbatim by the live agent and the backtester.

This is deliberately the only place that turns signals into fills. If the
backtest ran different code from the live loop, its results would describe a
system that does not exist — so both call `Engine.step`, and the only
difference between them is where the sessions come from.

One session, in order:

1. **Resting stops.** The stop is an order sitting at the broker. It works
   whether or not the agent is awake, and it is checked against this session's
   low using the level set on an earlier session — never the level this
   session would produce.
2. **Yesterday's queue fills at this open.** Sells first, so a closing position
   funds the buys behind it, exactly as the cash would settle in reality.
3. **Signals are read off this close** and queued for the *next* open.

Step 3 never touches the account and step 2 never reads a signal. That
separation is the whole design: in an equity market the closing print exists
only once the book is shut, so an agent that fills at the close it decided on
is trading at a price that was never available to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import events as calendar
from . import indicators as ind
from . import ranking, regime, trends
from . import strategy as strat
from .config import Settings
from .models import Decision, Order, Trade
from .portfolio import Portfolio
from .risk import entry_guard, size_position
from .strategy import Analysis


@dataclass
class SymbolView:
    """One symbol's analysis, plus the session the agent is looking at."""

    analysis: Analysis
    index: int
    new_bar: bool = True
    """False when a live tick revisits a session it already processed, so
    time-based counters are not advanced twice."""

    @property
    def bar(self):
        return self.analysis.bars[self.index]


@dataclass
class StepResult:
    ts: int
    equity: float
    decisions: list[Decision] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    """The queue left for the next open. Replaces whatever was there: an order
    that did not fill at the next open has expired, and carrying it forward
    would have the agent buying a breakout it decided on a week ago."""


class Engine:
    def __init__(
        self,
        settings: Settings,
        portfolio: Portfolio,
        on_trade: Callable[[Trade], None] | None = None,
        halted: bool = False,
        pending: list[Order] | None = None,
        calendars: dict | None = None,
    ):
        self.settings = settings
        self.portfolio = portfolio
        self.on_trade = on_trade
        self.halted = halted
        """Drawdown breaker latch, carried between steps. A backtest keeps one
        engine for the whole run; a live tick loads it from the store."""
        self.pending: list[Order] = list(pending or [])
        self.calendars: dict = calendars or {}
        """Earnings dates per symbol, resolved onto that symbol's sessions.

        Built once by the caller rather than per step, and empty unless the
        blackout is switched on. Unlike the news archive this *is* handed to
        the engine, because a filing date is knowledge a participant had at the
        time rather than an interpretation made afterwards — see `events.py`."""

    # ------------------------------------------------------------------

    def step(
        self,
        views: dict[str, SymbolView],
        ts: int,
        day_start_equity: float,
        peak_equity: float,
        decide: bool = True,
    ) -> StepResult:
        """One session.

        `decide=False` is a session the agent slept through. Its resting stops
        still work and its queued orders still fill — both live at the broker —
        but no signal is read, no trailing stop is moved and nothing new is
        queued. That asymmetry is the honest cost of a low cadence, and it is
        what the cadence table in the README measures.

        Nothing in this method reads a headline. `news.py` collects and scores
        them, and deliberately stops there — see its module docstring and the
        README for why a signal that cannot be backtested is not allowed to
        reach the account.
        """
        pf = self.portfolio
        closes = {sym: v.analysis.closes[v.index] for sym, v in views.items()}
        result = StepResult(ts=ts, equity=pf.equity(closes))

        # --- 1. Resting stops, before anything else -----------------------
        for symbol in list(pf.positions):
            view = views.get(symbol)
            if view is not None:
                self._check_stop(symbol, view, ts, result)

        # --- 2. Fill the queue at this session's open ---------------------
        self._fill_queue(views, ts, result)

        # --- 3. Age every surviving position ------------------------------
        for symbol, pos in pf.positions.items():
            view = views.get(symbol)
            if view is not None and view.new_bar:
                pos.bars_held += 1

        closes = {sym: v.analysis.closes[v.index] for sym, v in views.items()}
        equity = pf.equity(closes)
        result.equity = equity

        if not decide:
            result.orders = list(self.pending)
            return result

        # --- 4. Read this close, queue for the next open ------------------
        queue: list[Order] = []
        self._queue_exits(views, ts, equity, queue, result)
        self._queue_entries(
            views, ts, equity, day_start_equity, peak_equity, queue, result
        )

        if self.settings.execute_at_close:
            # The mode that exists to be measured rather than used: fill at the
            # price the decision was read from. See `Settings.execute_at_close`.
            self._fill_at_close(views, ts, queue, result)
            queue = []

        self.pending = queue
        result.orders = queue
        result.equity = pf.equity(
            {sym: v.analysis.closes[v.index] for sym, v in views.items()}
        )
        return result

    # --- 1. stops ------------------------------------------------------

    def _check_stop(
        self, symbol: str, view: SymbolView, ts: int, result: StepResult
    ) -> None:
        pos = self.portfolio.positions[symbol]
        hit, fill_price = strat.stop_hit(pos, view.bar)
        if not hit:
            return
        stop = pos.stop
        trade = self.portfolio.sell(symbol, fill_price, ts, "stop hit")
        gapped = fill_price < stop - 1e-9
        note = f"stop at {stop:.2f}" + (
            f" — gapped through, filled at the {fill_price:.2f} open" if gapped else ""
        )
        self._record_trade(trade, result, note)

    # --- 2. the queue ---------------------------------------------------

    def _fill_queue(
        self, views: dict[str, SymbolView], ts: int, result: StepResult
    ) -> None:
        """Execute yesterday's orders at today's opening print.

        Sells go first so their cash is available to the buys, which is the
        order the settlement actually happens in for anyone trading a cash
        account without hoarding a buffer.
        """
        s = self.settings
        pf = self.portfolio
        carried: list[Order] = []
        ready: list[Order] = []

        for order in self.pending:
            view = views.get(order.symbol)
            if view is None:
                continue  # no data for this session: the order simply lapses
            if order.created_ts >= view.bar.open_time:
                # Queued at a close that came *after* this open — it belongs to
                # a session still in the future. Filling it here would be the
                # backtester handing the agent a price it had not yet seen.
                carried.append(order)
                continue
            ready.append(order)

        self.pending = carried

        for order in sorted(ready, key=lambda o: 0 if o.side == "SELL" else 1):
            view = views[order.symbol]
            price = view.analysis.opens[view.index]
            if order.side == "SELL":
                self._fill_sell(order, price, ts, result)
            else:
                open_prices = {
                    sym: v.analysis.opens[v.index] for sym, v in views.items()
                }
                self._fill_buy(order, view, price, ts, open_prices, result)

        if len(pf.positions) > s.max_concurrent:  # pragma: no cover - invariant
            raise AssertionError("position limit breached by the queue")

    def _fill_sell(self, order: Order, price: float, ts: int, result: StepResult) -> None:
        pos = self.portfolio.positions.get(order.symbol)
        if pos is None:
            # The resting stop got there first, on the very gap this order was
            # meant to escape. Nothing left to sell.
            return
        if order.fraction >= 1.0:
            trade = self.portfolio.sell(order.symbol, price, ts, order.reason)
        else:
            trade = self.portfolio.reduce(
                order.symbol, order.fraction, price, ts, order.reason
            )
            strat.move_stop_to_breakeven(pos, self.settings)
        self._record_trade(trade, result, order.reason)

    def _fill_buy(
        self,
        order: Order,
        view: SymbolView,
        price: float,
        ts: int,
        open_prices: dict[str, float],
        result: StepResult,
        same_bar: bool = True,
    ) -> None:
        s = self.settings
        pf = self.portfolio
        equity = pf.equity(open_prices)

        def refuse(reason: str) -> None:
            result.decisions.append(
                Decision(ts, order.symbol, "HOLD", reason, price, 0.0, equity)
            )

        if order.symbol in pf.positions:
            return refuse("queued buy dropped: already holding")
        if len(pf.positions) >= s.max_concurrent:
            return refuse(f"queued buy dropped: position limit ({s.max_concurrent})")
        if price <= order.stop:
            return refuse(
                f"queued buy cancelled: opened at {price:.2f}, at or below the"
                f" {order.stop:.2f} stop"
            )

        # A market-on-open order is not a promise to buy at any price. An
        # overnight gap either makes the entry far more expensive than the one
        # that was decided on, or — downward — means the breakout has already
        # failed. Both are reasons not to be filled.
        gap = price - order.signal_price
        if order.atr > 0 and abs(gap) > s.max_gap_atr * order.atr:
            return refuse(
                f"queued buy cancelled: gapped {gap / order.atr:+.1f} ATR overnight"
                f" ({order.signal_price:.2f} -> {price:.2f})"
            )

        sizing = size_position(
            pf, price, order.stop, equity, open_prices, s, scale=order.size_factor
        )
        if not sizing.ok:
            return refuse(f"queued buy not sized: {sizing.reason}")

        fill = pf.buy(order.symbol, sizing.qty, price, ts, order.stop, order.atr)
        result.decisions.append(
            Decision(
                ts,
                order.symbol,
                "BUY",
                f"{order.reason} | filled at the open | {sizing.reason}",
                fill.price,
                fill.qty,
                equity,
                {
                    "stop": round(order.stop, 4),
                    "atr": round(order.atr, 4),
                    "signal_close": round(order.signal_price, 4),
                    "gap_atr": round(gap / order.atr, 2) if order.atr else None,
                    "fee": round(fill.fee, 4),
                    "risk": round(sizing.risk_amount, 2),
                },
            )
        )

        # The stop is live the moment the fill is. A name that opens up and
        # then unwinds all day can take the position out on the same session
        # it was entered, and letting it ride to the close instead would be a
        # free overnight that the trader never got.
        pos = pf.positions[order.symbol]
        if same_bar and view.bar.low <= pos.stop:
            trade = pf.sell(order.symbol, pos.stop, ts, "stop hit, same session")
            self._record_trade(trade, result, f"stop at {pos.stop:.2f}, same session")

    def _fill_at_close(
        self,
        views: dict[str, SymbolView],
        ts: int,
        queue: list[Order],
        result: StepResult,
    ) -> None:
        """Execute the queue immediately, at this session's close.

        Deliberately kept to a handful of lines and deliberately not the
        default. The same-session stop check is skipped here because there is
        no session left to hit it in — which is exactly one of the ways this
        mode flatters itself.
        """
        closes = {sym: v.analysis.closes[v.index] for sym, v in views.items()}
        for order in sorted(queue, key=lambda o: 0 if o.side == "SELL" else 1):
            view = views.get(order.symbol)
            if view is None:
                continue
            price = view.analysis.closes[view.index]
            if order.side == "SELL":
                self._fill_sell(order, price, ts, result)
            else:
                self._fill_buy(order, view, price, ts, closes, result, same_bar=False)

    # --- 4. reading the close -------------------------------------------

    def _queue_exits(
        self,
        views: dict[str, SymbolView],
        ts: int,
        equity: float,
        queue: list[Order],
        result: StepResult,
    ) -> None:
        s = self.settings
        pf = self.portfolio
        for symbol, pos in pf.positions.items():
            view = views.get(symbol)
            if view is None:
                continue
            a, i = view.analysis, view.index
            sig = strat.exit_signal(a, pos, s, i)

            if sig.action == "SELL":
                queue.append(
                    Order(
                        symbol,
                        "SELL",
                        sig.reason,
                        sig.price,
                        created_ts=a.bars[i].open_time,
                    )
                )
                result.decisions.append(
                    Decision(
                        ts,
                        symbol,
                        "HOLD",
                        f"queued to sell at the next open: {sig.reason}",
                        sig.price,
                        pos.qty,
                        equity,
                    )
                )
                continue

            if s.earnings_exit_before > 0:
                book = self.calendars.get(symbol)
                window = book.window(view.index, s) if book else None
                if (
                    window is not None
                    and window.sessions_until is not None
                    and window.sessions_until <= s.earnings_exit_before
                ):
                    queue.append(
                        Order(
                            symbol,
                            "SELL",
                            f"closed ahead of earnings on {window.event_date}",
                            sig.price,
                            created_ts=a.bars[i].open_time,
                        )
                    )
                    continue

            if strat.should_scale_out(pos, sig.price, s):
                r = pos.r_multiple(sig.price)
                queue.append(
                    Order(
                        symbol,
                        "SELL",
                        f"banked {s.scale_out_fraction:.0%} at {r:.1f}R",
                        sig.price,
                        fraction=s.scale_out_fraction,
                        created_ts=a.bars[i].open_time,
                    )
                )

            if view.new_bar:
                strat.update_trailing_stop(pos, view.bar, a.atr[i], s)

            result.decisions.append(
                Decision(
                    ts,
                    symbol,
                    "HOLD",
                    sig.reason,
                    sig.price,
                    pos.qty,
                    0.0,
                    {"stop": round(pos.stop, 4), "bars_held": pos.bars_held},
                )
            )

    def _queue_entries(
        self,
        views: dict[str, SymbolView],
        ts: int,
        equity: float,
        day_start_equity: float,
        peak_equity: float,
        queue: list[Order],
        result: StepResult,
    ) -> None:
        s = self.settings
        pf = self.portfolio

        guard = entry_guard(
            pf, equity, day_start_equity, peak_equity, s, halted=self.halted
        )
        self.halted = guard.halted

        market = regime.assess(views, s)
        if not market.risk_on:
            result.decisions.append(
                Decision(
                    ts,
                    "-",
                    "HOLD",
                    f"risk-off, no new entries: {market.reason}",
                    0.0,
                    0.0,
                    equity,
                    {"breadth": round(market.breadth, 3)},
                )
            )
            return
        if not guard.allowed:
            result.decisions.append(
                Decision(
                    ts, "-", "HOLD", f"no new entries: {guard.reason}", 0.0, 0.0, equity
                )
            )
            return

        # The ranking spans the whole tradable universe, held names included:
        # how strong a stock is does not depend on whether we happen to own it.
        tradable_views = {k: v for k, v in views.items() if k in set(s.universe)}
        ranks = ranking.rank_universe(tradable_views, s)

        # The trend ladder is only computed when a gate actually reads it:
        # eighty-seven names over five horizons on every session is real work,
        # and paying for it to be discarded would be silly.
        gated = s.min_medium_trend != 0.0 or s.sector_top_k > 0
        ladder = trends.collect(tradable_views, s) if gated else {}
        sectors = trends.sector_ranks(ladder, s) if s.sector_top_k > 0 else {}

        selling = {o.symbol for o in queue if o.side == "SELL" and o.fraction >= 1.0}

        candidates = []
        for symbol in s.universe:
            view = views.get(symbol)
            if view is None or symbol in pf.positions:
                continue
            sig = strat.entry_signal(view.analysis, s, view.index)
            sig.symbol = symbol

            if sig.action != "BUY":
                result.decisions.append(
                    Decision(ts, symbol, "HOLD", sig.reason, sig.price, 0.0, equity)
                )
                continue

            strong_enough, rs_note = ranking.passes(symbol, ranks, s)
            if not strong_enough:
                result.decisions.append(
                    Decision(
                        ts,
                        symbol,
                        "HOLD",
                        f"signal fired but rejected on relative strength: {rs_note}",
                        sig.price,
                        0.0,
                        equity,
                        {"strength": sig.strength, "rs_rank": ranks.get(symbol)},
                    )
                )
                continue

            if gated:
                in_trend, trend_note = trends.passes(symbol, ladder, sectors, s)
                if not in_trend:
                    result.decisions.append(
                        Decision(
                            ts,
                            symbol,
                            "HOLD",
                            f"signal fired but rejected on trend: {trend_note}",
                            sig.price,
                            0.0,
                            equity,
                            {"strength": sig.strength},
                        )
                    )
                    continue
                if trend_note:
                    sig.reason = f"{sig.reason}; {trend_note}"

            size_factor = 1.0
            book = self.calendars.get(symbol)
            if book is not None:
                window = book.window(view.index, s)
                allowed, size_factor, event_note = calendar.passes(window, s)
                if not allowed:
                    result.decisions.append(
                        Decision(
                            ts,
                            symbol,
                            "HOLD",
                            f"signal fired but stood down for {event_note}",
                            sig.price,
                            0.0,
                            equity,
                            {"strength": sig.strength, "event": window.event_date},
                        )
                    )
                    continue
                if event_note:
                    sig.reason = f"{sig.reason}; {event_note}"

            if rs_note:
                sig.reason = f"{sig.reason}; {rs_note}"
            sig.size_factor = size_factor
            candidates.append(sig)

        if s.rs_top_k > 0:
            # With the filter on, peer strength is the better tie-break: it is
            # the measure the slots were rationed by in the first place.
            candidates.sort(key=lambda x: (ranks.get(x.symbol, 10**6), -x.strength))
        else:
            candidates.sort(key=lambda x: x.strength, reverse=True)

        # Slots are counted against what the book will look like at the open,
        # not what it looks like now: positions queued to be sold free a slot,
        # and every buy already in the queue takes one.
        for sig in candidates:
            taken = (
                len(pf.positions)
                - len(selling)
                + sum(1 for o in queue if o.side == "BUY")
            )
            if taken >= s.max_concurrent:
                result.decisions.append(
                    Decision(
                        ts,
                        sig.symbol,
                        "HOLD",
                        "signal fired but position limit reached",
                        sig.price,
                        0.0,
                        equity,
                        {"strength": sig.strength},
                    )
                )
                continue

            view = views[sig.symbol]
            queue.append(
                Order(
                    symbol=sig.symbol,
                    side="BUY",
                    reason=sig.reason,
                    signal_price=sig.price,
                    stop=sig.stop,
                    atr=sig.atr,
                    size_factor=sig.size_factor,
                    created_ts=view.bar.open_time,
                )
            )
            result.decisions.append(
                Decision(
                    ts,
                    sig.symbol,
                    "HOLD",
                    f"queued to buy at the next open: {sig.reason}",
                    sig.price,
                    0.0,
                    equity,
                    {
                        "stop": round(sig.stop, 4),
                        "atr": round(sig.atr, 4),
                        "strength": sig.strength,
                        "rs_rank": ranks.get(sig.symbol),
                    },
                )
            )

    # ------------------------------------------------------------------

    def _record_trade(self, trade: Trade, result: StepResult, reason: str) -> None:
        result.trades.append(trade)
        result.decisions.append(
            Decision(
                trade.exit_time,
                trade.symbol,
                "SELL",
                reason,
                trade.exit_price,
                trade.qty,
                0.0,
                {
                    "pnl": round(trade.pnl, 4),
                    "fees": round(trade.fees, 4),
                    "return_pct": round(trade.return_pct, 5),
                },
            )
        )
        if self.on_trade:
            self.on_trade(trade)


def market_stats(views: dict[str, SymbolView], settings: Settings) -> tuple[float, int]:
    """Two numbers the status report needs: how volatile the market is versus
    its own recent normal, and how many names sit close enough to their
    breakout level that the next session could produce a trigger.
    """
    ratios: list[float] = []
    near = 0

    for symbol, view in views.items():
        if symbol not in set(settings.universe):
            continue
        a, i = view.analysis, view.index
        if not a.ready(i):
            continue

        window = [
            (a.atr[j] / a.closes[j])
            for j in range(max(0, i - settings.vol_lookback), i + 1)
            if a.atr[j] is not None and a.closes[j]
        ]
        if len(window) >= 20:
            median = ind.percentile(window, 0.5)
            if median > 0:
                ratios.append(a.atr_pct(i) / median)

        atr = a.atr[i]
        level = a.donchian[i]
        if atr and level and 0 <= (level - a.closes[i]) < 0.5 * atr:
            near += 1

    vol_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    return round(vol_ratio, 4), near
