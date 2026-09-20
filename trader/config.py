"""Every tunable of the agent, in one frozen object.

Nothing else in the codebase reads an environment variable or hardcodes a
threshold: the strategy, the risk layer and the backtester all take a
``Settings`` instance, which is what makes a backtest and a live tick provably
the same run.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("TRADER_DB", "data/trader.db"))


@dataclass(frozen=True)
class Settings:
    # --- universe & timeframe -------------------------------------------
    universe: tuple[str, ...] = (
        # Information technology
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CSCO", "ADBE", "CRM",
        "AMD", "INTC", "TXN", "QCOM", "IBM", "ACN", "AMAT",
        # Communication services
        "GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ",
        # Consumer discretionary
        "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "BKNG", "TJX",
        # Consumer staples
        "PG", "KO", "PEP", "WMT", "COST", "PM", "MDLZ", "CL",
        # Health care
        "JNJ", "UNH", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
        "AMGN", "ISRG", "GILD",
        # Financials
        "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "SPGI", "BLK", "AXP",
        "C", "SCHW",
        # Industrials
        "CAT", "BA", "HON", "UNP", "GE", "RTX", "LMT", "DE", "UPS", "MMM",
        # Energy
        "XOM", "CVX", "COP", "SLB", "EOG",
        # Utilities
        "NEE", "DUK", "SO",
        # Materials
        "LIN", "SHW", "APD", "NEM",
        # Real estate
        "AMT", "PLD", "SPG",
    )
    """Eighty-seven US large caps, spread deliberately across all eleven sectors.

    Sector spread is the point, not decoration. The crypto version of this
    agent measured its twenty pairs at an average pairwise correlation of 0.67,
    worth 1.46 independent assets out of twenty — widening that universe bought
    almost no statistical power because everything moved together. Equities are
    the cheapest available fix: sectors genuinely decouple, so eighty-seven names buy
    materially more independent bets than eighty-seven coins would. How much more is
    measured in the README rather than assumed here.

    Every name on this list is a company that is large and listed *today*. The
    survivorship bias that creates is the single worst problem with the
    backtest, it cannot be fixed with free data, and it is why the report
    benchmarks against SPY as well as against this basket."""

    benchmark: str = "SPY"
    """Fetched and analysed like any other symbol, but never traded.

    It serves two jobs: it is the market anchor for the regime filter, and it
    is the benchmark a real investor actually has access to. An equal-weight
    basket of the names above is *not* that benchmark — it is a basket
    chosen with hindsight, and beating it proves less than beating the index
    does."""

    interval: str = "1d"
    """Daily sessions.

    Not a preference — a constraint, stated plainly. Free equity history is
    daily; intraday from the same sources reaches back two years at best. The
    crypto agent's most useful finding was that lengthening history was the
    only lever that moved statistical significance at all, so thirty years of
    daily bars beats two years of hourly ones by a wide margin."""

    # --- account ----------------------------------------------------------
    initial_capital: float = 100_000.0
    quote: str = "USD"

    # --- risk ------------------------------------------------------------
    risk_per_trade: float = 0.006
    """Fraction of equity lost if the stop is hit. Position size is derived
    from this and the stop distance, never set as a fixed notional."""
    max_position_pct: float = 0.08
    max_concurrent: int = 12
    max_exposure_pct: float = 0.75
    daily_loss_limit: float = 0.04
    """Equity drawdown within one session that halts new entries until the
    next one. Exits are never halted."""
    max_drawdown_stop: float = 0.35
    """Peak-to-trough equity loss that stops the agent opening anything new."""
    drawdown_resume: float | None = 0.15
    """Drawdown level at which a halted agent starts trading again; `None`
    halts permanently until a human resets it."""

    # --- execution costs --------------------------------------------------
    fee_rate: float = 0.0002
    """Commission plus regulatory fees, per side. US retail equity commissions
    are nominally zero; 2bp is a pessimistic stand-in for the fees that are
    not."""
    slippage: float = 0.0005
    """Half-spread plus impact, per side, applied against the agent. Orders
    fill in the opening auction, which is the most liquid print of the day and
    also the most volatile, so this is not generous."""
    min_notional: float = 500.0
    whole_shares: bool = True
    """Round orders down to whole shares.

    Fractional shares exist at most retail brokers now, but rounding down is
    the conservative assumption and it has a real consequence the crypto agent
    never faced: a 600 USD risk budget against a 1,200 USD share price cannot
    express a small position at all. Turning this off makes the sizing
    continuous again, and the README measures what that is worth."""

    max_gap_atr: float = 1.0
    """Cancel a queued entry if the opening print has gapped more than this
    many ATR beyond the close the decision was made on.

    A market-on-open order is not a promise to buy at any price. Without this,
    the agent buys every overnight earnings spike at the top of the gap, with
    a stop that was computed for a price that no longer exists."""

    # --- strategy ---------------------------------------------------------
    fast_ema: int = 12
    slow_ema: int = 26
    trend_ema: int = 50
    regime_ema: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    stop_atr_mult: float = 4.0
    trail_atr_mult: float = 5.0
    take_profit_r: float = 0.0
    """Fixed profit target in R, disabled (0) by default. A trend follower
    with a low win rate needs its winners open-ended."""
    rsi_overbought: float = 78.0
    min_atr_pct: float = 0.005
    max_atr_pct: float = 0.10
    """Daily ATR as a fraction of price. A large cap sits near 1.5%; the band
    excludes a dead tape below and an unhedgeable one above."""
    trend_slope_lookback: int = 5
    breakout_bars: int = 20
    """The entry trigger: a close above the highest high of the previous N
    sessions (a Donchian channel break).

    A breakout is a *state*, not an event: price is either above the level it
    cleared or it is not, so the agent sees it whenever it wakes. That is what
    makes the result robust to how often it wakes, which is the property the
    crypto version of this agent had to rebuild its trigger to obtain."""
    trigger_window: int = 1
    min_hold_bars: int = 4
    """Blocks churn on a non-stop exit; a stop always overrides it."""

    # --- partial exits ----------------------------------------------------
    scale_out_r: float = 0.0
    scale_out_fraction: float = 0.5
    breakeven_after_scale: bool = True

    # --- market regime ----------------------------------------------------
    min_breadth: float = 0.0
    """Minimum fraction of the universe trading above its own EMA 200 before
    any new position is opened; 0 disables."""
    market_anchor: str = "SPY"
    """Symbol whose own regime gates the whole universe. Empty disables.

    SPY rather than a breadth threshold: it is one well-defined condition, it
    is the thing every US equity is actually correlated to, and it does not
    need a number tuned on the same data the strategy was tuned on."""

    # --- relative strength (cross-sectional) ------------------------------
    rs_top_k: int = 0
    rs_lookback: int = 63
    """Sessions used to measure relative strength. 63 ~ one quarter."""
    rs_vol_normalise: bool = True

    # --- decision cadence -------------------------------------------------
    decide_every_n_sessions: int = 1
    """How often the agent evaluates signals, in sessions. 1 = every close.

    Daily bars produce exactly one close a day, so unlike the crypto agent
    there is no honest way to decide five times a day: the extra wake-ups would
    read the same unchanged bar. The cadence question does not disappear
    though, it inverts — the interesting direction is *less* often, and what
    that costs is measured in the README.

    Between decisions the resting stop still protects open positions (it lives
    at the broker, not in this process) but the trailing stop is not moved,
    because moving it requires an order the sleeping agent did not send."""

    settle_minutes: int = 20
    """How long after the closing bell to wait before reading the session.
    Consolidated closing prints are not instant."""

    vol_lookback: int = 120

    # --- data -------------------------------------------------------------
    history_bars: int = 600
    history_start: str = "1990-01-01"
    """Earliest session to request on a full sync. Data exists well before
    this for some names; 1990 already spans four bear markets."""
    hosts: tuple[str, ...] = (
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    )

    @property
    def data_universe(self) -> tuple[str, ...]:
        """Every symbol to download: the tradable ones plus the benchmark."""
        extra = tuple(
            s for s in (self.benchmark, self.market_anchor) if s and s not in self.universe
        )
        return self.universe + tuple(dict.fromkeys(extra))

    @property
    def warmup_bars(self) -> int:
        """Bars needed before any signal is trustworthy."""
        return (
            max(
                self.regime_ema,
                self.trend_ema,
                self.slow_ema,
                self.rs_lookback,
                self.breakout_bars,
            )
            + self.atr_period
            + 5
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        """Read overrides from a JSON file, falling back to the defaults.

        Only keys present in the file are overridden, so a config can stay
        small and still survive new fields being added here.
        """
        settings = cls()
        if path is None:
            env = os.environ.get("TRADER_CONFIG")
            if not env:
                return settings
            path = env
        p = Path(path)
        if not p.exists():
            return settings
        raw = json.loads(p.read_text())
        known = set(asdict(settings))
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        for key in ("universe", "hosts"):
            if key in raw:
                raw[key] = tuple(raw[key])
        return replace(settings, **raw)
