"""The virtual account.

Fills are modelled pessimistically on purpose: the buy pays slippage up, the
sell takes slippage down, and both sides pay the fee. A paper agent that
ignores costs looks profitable at this trade frequency and is not.

The numbers are smaller than a crypto agent's — US retail equity commissions
are nominally zero and large-cap spreads are a basis point or two — but they
are not zero, and orders here fill in the opening auction, which is the most
volatile print of the day.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Position, Trade


class InsufficientFunds(RuntimeError):
    pass


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


class Portfolio:
    def __init__(
        self,
        settings: Settings,
        cash: float | None = None,
        positions: dict[str, Position] | None = None,
    ):
        self.settings = settings
        self.cash = settings.initial_capital if cash is None else cash
        self.positions: dict[str, Position] = positions or {}

    # --- valuation --------------------------------------------------------

    def exposure(self, prices: dict[str, float]) -> float:
        return sum(
            p.notional(prices[s]) for s, p in self.positions.items() if s in prices
        )

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.exposure(prices)

    def unrealized(self, prices: dict[str, float]) -> float:
        return sum(
            p.unrealized(prices[s]) for s, p in self.positions.items() if s in prices
        )

    # --- execution --------------------------------------------------------

    def buy_fill_price(self, price: float) -> float:
        return price * (1 + self.settings.slippage)

    def sell_fill_price(self, price: float) -> float:
        return price * (1 - self.settings.slippage)

    def affordable_qty(self, price: float) -> float:
        """Largest quantity the cash balance covers once slippage and the fee
        are paid."""
        fill = self.buy_fill_price(price)
        if fill <= 0:
            return 0.0
        spendable = self.cash - self.settings.fee_per_order
        if spendable <= 0:
            return 0.0
        return spendable / (fill * (1 + self.settings.fee_rate))

    def buy(
        self,
        symbol: str,
        qty: float,
        price: float,
        ts: int,
        stop: float,
        atr: float,
    ) -> Fill:
        if symbol in self.positions:
            raise ValueError(f"already holding {symbol}; this agent does not average in")
        if qty <= 0:
            raise ValueError("qty must be positive")

        fill_price = self.buy_fill_price(price)
        cost = qty * fill_price
        fee = cost * self.settings.fee_rate + self.settings.fee_per_order
        if cost + fee > self.cash + 1e-9:
            raise InsufficientFunds(
                f"need {cost + fee:.2f} {self.settings.quote}, have {self.cash:.2f}"
            )

        self.cash -= cost + fee
        self.positions[symbol] = Position(
            symbol=symbol,
            qty=qty,
            entry_price=fill_price,
            entry_time=ts,
            stop=stop,
            peak=fill_price,
            atr_at_entry=atr,
            initial_risk=max(fill_price - stop, 0.0),
        )
        return Fill(symbol, "BUY", qty, fill_price, fee)

    def sell(self, symbol: str, price: float, ts: int, reason: str) -> Trade:
        """Close the whole position."""
        return self.reduce(symbol, 1.0, price, ts, reason)

    def reduce(
        self, symbol: str, fraction: float, price: float, ts: int, reason: str
    ) -> Trade:
        """Sell `fraction` of a position, banking that part of the profit.

        The entry fee is apportioned to the quantity sold, so a scale-out and
        the later close together book exactly the fees a single full exit would
        have. The remainder keeps its original entry price and initial risk:
        what it cost and what it risked did not change because some of it was
        sold.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            raise ValueError(f"no open position on {symbol}")
        if not 0 < fraction <= 1:
            raise ValueError("fraction must be in (0, 1]")

        qty = pos.qty if fraction == 1.0 else pos.qty * fraction
        if self.settings.whole_shares and fraction < 1.0:
            # Half of seven shares is not three and a half shares.
            qty = float(int(qty))
            if qty <= 0 or qty >= pos.qty:
                qty = pos.qty
                fraction = 1.0

        fill_price = self.sell_fill_price(price)
        proceeds = qty * fill_price
        # A partial exit is a whole order, so it pays the flat exit charge in
        # full. The entry was *one* order, so its flat charge is booked once,
        # on the close — not apportioned by `fraction`, which is a fraction of
        # what is left rather than of what was bought, and would charge half a
        # euro on a scale-out and a whole one on the remainder.
        exit_fee = proceeds * self.settings.fee_rate + self.settings.fee_per_order
        entry_fee = qty * pos.entry_price * self.settings.fee_rate + (
            0.0 if fraction < 1.0 else self.settings.fee_per_order
        )

        self.cash += proceeds - exit_fee
        pnl = (proceeds - exit_fee) - (qty * pos.entry_price + entry_fee)

        partial = fraction < 1.0
        if partial:
            pos.qty -= qty
            pos.scaled_out = True
        else:
            del self.positions[symbol]

        return Trade(
            symbol=symbol,
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=fill_price,
            entry_time=pos.entry_time,
            exit_time=ts,
            fees=entry_fee + exit_fee,
            pnl=pnl,
            reason=reason,
            partial=partial,
        )

    # --- persistence helpers ---------------------------------------------

    def snapshot(self, prices: dict[str, float]) -> dict:
        return {
            "cash": round(self.cash, 4),
            "exposure": round(self.exposure(prices), 4),
            "equity": round(self.equity(prices), 4),
            "positions": {
                s: {
                    "qty": p.qty,
                    "entry": p.entry_price,
                    "stop": p.stop,
                    "price": prices.get(s),
                    "unrealized": round(p.unrealized(prices[s]), 4)
                    if s in prices
                    else None,
                }
                for s, p in self.positions.items()
            },
        }
