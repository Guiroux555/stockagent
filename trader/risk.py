"""Position sizing and the circuit breakers.

The strategy decides *whether*; this module decides *how much*, and it is the
part that actually determines whether the account survives. Size is derived
from the stop distance, never from a fixed fraction of cash: a wide stop buys
less, a tight stop buys more, and the loss if the stop is hit is the same
fraction of equity either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .portfolio import Portfolio


@dataclass
class Sizing:
    qty: float
    reason: str
    risk_amount: float = 0.0

    @property
    def ok(self) -> bool:
        return self.qty > 0


@dataclass
class Guard:
    allowed: bool
    reason: str
    halted: bool = False
    """Whether the drawdown circuit breaker is latched after this check."""


def size_position(
    portfolio: Portfolio,
    price: float,
    stop: float,
    equity: float,
    prices: dict[str, float],
    settings: Settings,
    scale: float = 1.0,
) -> Sizing:
    """How many shares to buy at `price`, with the stop already chosen.

    `scale` shrinks the whole budget rather than the final share count, so
    every cap below still binds on the reduced position and the whole-share
    rounding happens last. Halving after the caps would let a capped position
    come back over its cap.

    `price` is the price the order will actually fill at — for this agent, the
    next session's opening print, not the close the signal was computed on.
    Sizing off the signal price would quietly risk more than the budget allows
    every time the market gapped up overnight, which in equities is most
    nights.
    """
    if price <= 0:
        return Sizing(0.0, "invalid price")

    fill = portfolio.buy_fill_price(price)
    stop_distance = fill - stop
    if stop_distance <= 0:
        return Sizing(0.0, "stop is not below entry")

    scale = max(0.0, min(scale, 1.0))
    if scale == 0.0:
        return Sizing(0.0, "size scaled to zero")

    risk_amount = equity * settings.risk_per_trade * scale
    qty = risk_amount / stop_distance
    binding = "risk budget"

    # Never let one name dominate the book, however tight its stop.
    cap_position = equity * settings.max_position_pct * scale / fill
    if cap_position < qty:
        qty, binding = cap_position, "max position size"

    # Keep a cash reserve so the agent is never fully invested.
    room = equity * settings.max_exposure_pct - portfolio.exposure(prices)
    cap_exposure = max(room, 0.0) / fill
    if cap_exposure < qty:
        qty, binding = cap_exposure, "total exposure cap"

    # And it can only spend what it actually has, fees included.
    cap_cash = portfolio.affordable_qty(price) * 0.999
    if cap_cash < qty:
        qty, binding = cap_cash, "available cash"

    if settings.whole_shares:
        # Always down, never to nearest: rounding up breaks the cap that was
        # binding a line earlier, and the one that binds is usually the cash.
        qty = float(int(qty))
        if qty <= 0:
            return Sizing(
                0.0,
                f"under one share at {fill:.2f} {settings.quote} ({binding})",
            )

    if qty <= 0:
        return Sizing(0.0, f"no room ({binding})")

    notional = qty * fill
    if notional < settings.min_notional:
        return Sizing(
            0.0,
            f"notional {notional:.2f} below minimum {settings.min_notional:.2f}"
            f" ({binding})",
        )

    return Sizing(
        qty=qty,
        reason=(
            f"{binding}: {qty:.0f} sh, {notional:.2f} {settings.quote},"
            f" risking {risk_amount:.2f}"
        ),
        risk_amount=risk_amount,
    )


def entry_guard(
    portfolio: Portfolio,
    equity: float,
    day_start_equity: float,
    peak_equity: float,
    settings: Settings,
    halted: bool = False,
) -> Guard:
    """Gates that block *new* entries. Exits are never blocked — an agent that
    cannot close a losing position is worse than one that cannot open a
    winning one.

    `halted` carries the drawdown breaker's latch between calls: once tripped
    it stays tripped until the drawdown recovers past `drawdown_resume`, so a
    dip back across the threshold does not flap the agent on and off.
    """
    drawdown = 1 - equity / peak_equity if peak_equity > 0 else 0.0

    if halted:
        resume = settings.drawdown_resume
        if resume is None:
            return Guard(
                False,
                f"halted on drawdown ({drawdown:.1%} from peak) — manual reset required",
                halted=True,
            )
        if drawdown > resume:
            return Guard(
                False,
                f"halted on drawdown, waiting to recover "
                f"({drawdown:.1%} from peak, resumes at {resume:.0%})",
                halted=True,
            )
        halted = False  # recovered; fall through to the ordinary gates

    elif drawdown >= settings.max_drawdown_stop:
        return Guard(
            False,
            f"max drawdown breached ({drawdown:.1%} from peak)",
            halted=True,
        )

    if len(portfolio.positions) >= settings.max_concurrent:
        return Guard(False, f"at position limit ({settings.max_concurrent})")

    if day_start_equity > 0:
        day_change = equity / day_start_equity - 1
        if day_change <= -settings.daily_loss_limit:
            return Guard(False, f"daily loss limit hit ({day_change:.1%})")

    return Guard(True, "ok")
