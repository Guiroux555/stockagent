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

    sectors: tuple[tuple[str, str], ...] = (
        ("Information technology", "AAPL MSFT NVDA AVGO ORCL CSCO ADBE CRM AMD"
                                   " INTC TXN QCOM IBM ACN AMAT"),
        ("Communication services", "GOOGL META NFLX DIS CMCSA T VZ"),
        ("Consumer discretionary", "AMZN TSLA HD MCD NKE SBUX LOW BKNG TJX"),
        ("Consumer staples", "PG KO PEP WMT COST PM MDLZ CL"),
        ("Health care", "JNJ UNH LLY ABBV MRK PFE TMO ABT DHR AMGN ISRG GILD"),
        ("Financials", "BRK-B JPM BAC WFC GS MS SPGI BLK AXP C SCHW"),
        ("Industrials", "CAT BA HON UNP GE RTX LMT DE UPS MMM"),
        ("Energy", "XOM CVX COP SLB EOG"),
        ("Utilities", "NEE DUK SO"),
        ("Materials", "LIN SHW APD NEM"),
        ("Real estate", "AMT PLD SPG"),
    )
    """Which sector each name belongs to.

    Not decoration, and not only documentation: the measured independence of
    this universe comes entirely from this split. Names inside one sector
    correlate at 0.467, names across two at 0.324 — and the eleven financials
    correlate at 0.671, which is exactly the figure the crypto version of this
    agent measured across its whole universe. A sector is a crypto market.

    It is a tuple of pairs rather than a dict so that `Settings` stays hashable
    and frozen like everything else here."""

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
    risk_per_trade: float = 0.003
    """Fraction of equity lost if the stop is hit. Position size is derived
    from this and the stop distance, never set as a fixed notional."""
    max_position_pct: float = 0.04
    max_concurrent: int = 25
    """Twenty-five positions, not twelve.

    Widening the book buys no return at all — 12, 18, 25 and 35 slots all land
    within half a point of the same CAGR, in-sample and out. What it buys is
    the disappearance of single-trade dependence: the best position falls from
    10.8% of all gains to 7.0% in-sample, and the worst drawdown from 19.5% to
    14.4%. The risk budget per position is cut to keep the total constant, so
    this is the same money spread over twice as many bets.

    This is the thing the crypto version of this agent tried and could not get.
    There, quadrupling the universe left effective independence at 1.46 assets
    out of twenty because everything correlated at 0.67. Here it works, and the
    reason is the only structural advantage equities have: sectors decouple."""
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

    execute_at_close: bool = False
    """Fill orders at the close the decision was read from, instead of at the
    next open. **This is the dishonest mode, and it exists to be measured.**

    It is what most equity backtests do, usually without saying so, and it is
    not reachable in reality: the closing print is published once the book is
    shut. Turning it on and comparing is the only way to put a number on what
    that shortcut is worth, and the number is in the README. Leave it off."""

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
    """Initial stop distance. With size derived from the stop, this is mostly a
    leverage dial rather than a signal setting: from 2 to 8 ATR the return and
    the drawdown move together and CAGR-per-drawdown stays between 0.39 and
    0.45. Four sits in the middle of that flat stretch."""
    trail_atr_mult: float = 8.0
    """Wide, and the measurement is blunt about why: on daily bars the trailing
    stop does not earn its place.

    In-sample and out of sample alike, return rises monotonically as the trail
    widens and flattens once it stops binding — 3 ATR costs half the return of
    8, and switching it off entirely is marginally better again. A 5 ATR trail,
    which is what the crypto version of this agent settled on, gives up about
    two points of CAGR here. The reason is arithmetic: a daily ATR is around
    1.5% of price, so 5 ATR is a 7% pullback, and large caps hand back 7%
    inside perfectly healthy year-long trends. On 4h candles that same multiple
    is a far smaller move relative to the trend it is trying to survive.

    Eight is the point where it stops hurting. Leaving it there rather than
    disabling it costs roughly 0.7 points of out-of-sample CAGR, and buys a
    defined worst case on a position that has run a long way above its EMA 200
    — the one situation the regime exit is slow to handle. That trade is a
    judgement call, and it is stated here rather than hidden in a number."""
    take_profit_r: float = 0.0
    """Fixed profit target in R, disabled (0) by default. A trend follower
    with a low win rate needs its winners open-ended."""
    rsi_overbought: float = 78.0
    min_atr_pct: float = 0.005
    max_atr_pct: float = 0.10
    """Daily ATR as a fraction of price. A large cap sits near 1.5%.

    Measured, this band is inert: removing it changes the backtest by nothing
    at all, to the last basis point, because a US large cap essentially never
    trades outside it. It is kept as a guard against a name that stops behaving
    like one — a halt, a takeover, a collapse — and is reported as inert rather
    than quietly credited with the result."""
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
    need a number tuned on the same data the strategy was tuned on.

    Its measured value depends entirely on whether the window contains a bear
    market, and saying so is the honest version of this docstring. Over
    1990-2012, which holds two 50% index drawdowns, removing it doubles the
    worst drawdown (14.4% to 30.6%). Over 2012-2026, which holds one real bear
    market and two crashes that recovered within months, removing it is
    *better* (CAGR per drawdown 0.61 against 0.47). A protection is not
    evaluated on a period that did not need it, so it stays on — but it cost
    about 0.2 points of out-of-sample CAGR, and that number belongs here."""

    # --- relative strength (cross-sectional) ------------------------------
    rs_top_k: int = 0
    """Only the `k` strongest names may be entered; 0 turns the filter off.

    Off by default because it was measured and it does nothing. The crypto
    version of this agent blamed its own small universe — cross-sectional
    momentum is built for hundreds of names, not five correlated coins. That
    hypothesis is testable here and it is wrong: across eighty-seven names,
    every setting from top-10 to top-60 either loses return or is
    indistinguishable from leaving it off."""
    rs_lookback: int = 63
    """Sessions used to measure relative strength. 63 ~ one quarter."""
    rs_vol_normalise: bool = True

    # --- short / medium-term trend gates ----------------------------------
    min_medium_trend: float = 0.0
    """Minimum medium-term trend score (3 and 6 month return over ATR%) before
    a name may be entered; 0 disables the gate.

    The breakout trigger is a one-horizon question. This is the gate that asks
    the other one — has this been climbing for two quarters, or for two weeks?
    Off by default until the measurement in the README says otherwise."""

    sector_top_k: int = 0
    """Only names in the `k` strongest sectors may be entered; 0 disables.

    Worth testing separately from `rs_top_k` even though name-level momentum
    failed, because the correlation measurement says the sector is the unit
    that actually decouples: within a sector names correlate at 0.467, across
    sectors at 0.324. If cross-sectional momentum works anywhere in this
    universe, it should work here."""

    # --- earnings blackout --------------------------------------------------
    earnings_mode: str = "off"
    """`off`, `block` or `reduce`.

    Off by default until the measurement justifies it, like every other gate in
    this file. What it guards against is real and specific: `risk.py` promises
    that a position loses `risk_per_trade` if the stop is hit, and an earnings
    gap breaks that promise by jumping the stop instead of crossing it. See
    `events.py` and the README."""

    earnings_blackout_before: int = 2
    """Sessions before a *confirmed* release during which no new position is
    opened. Two, because the agent decides on a close and fills at the next
    open: one session of margin, plus the session of the release itself."""
    earnings_blackout_after: int = 1
    """Sessions after a release during which no new position is opened."""
    earnings_estimated_extra: int = 2
    """Extra sessions on both sides when the date is a projection rather than a
    filing. A wrong date is worse than no date — it blocks the safe day and
    leaves the dangerous one open — so an estimate buys a wider window rather
    than the same confidence."""
    earnings_size_factor: float = 0.5
    """Position multiplier in `reduce` mode. Halving the size halves the gap
    loss, which is the quantity the promise in `risk.py` is about."""

    sec_user_agent: str = (
        "stock-paper-trader/0.1 (paper trading research; example@example.com)"
    )
    """EDGAR refuses traffic whose User-Agent carries no contact address, and
    it wants one shaped like an email — the string above without the address
    returns 403.

    It is a courtesy header, not a credential: it identifies no account, opens
    no session and unlocks nothing that is not already public. The default is a
    placeholder on purpose, because nobody's real address belongs in a
    repository. **Put yours here before running a long sync**; the SEC asks for
    a way to reach whoever is generating the traffic, and a placeholder is an
    answer that does not answer."""
    cik_history: tuple[tuple[str, str], ...] = (
        ("XOM", "34088"),
        ("BLK", "1364742"),
        ("GOOGL", "1288776"),
        ("DIS", "1001039"),
    )
    """Predecessor filers to merge in, by ticker.

    The SEC ticker map points at whoever files *today*. A company that
    reorganised — Exxon in 2025, BlackRock in 2024, Google into Alphabet in
    2015, Disney through the Fox deal in 2019 — keeps its earlier filings under
    the old registrant, so following the map alone returns one release for
    Exxon and eight for BlackRock while the price series runs back to 1990.

    That is exactly the failure this whole feature is supposed to avoid: a
    calendar with holes blocks the sessions it knows about, leaves the rest
    open, and looks like protection either way. These four were found by the
    coverage check below, not guessed, and the check keeps running so the next
    one is found too."""

    min_calendar_coverage: float = 0.6
    """Fraction of the expected quarterly releases a name must have before its
    calendar is treated as usable. Below it, the name is reported as uncovered
    rather than quietly half-protected."""

    sec_pause: float = 0.15
    """Seconds between EDGAR requests. Their published ceiling is ten a second;
    this sits well under it."""

    # --- news -------------------------------------------------------------
    news_enabled: bool = False
    """Fetch headlines on every tick and add them to the archive.

    Collection only. **No rule in this agent reads a headline**, and there is
    no setting that would let one — the wiring does not exist, which is a
    stronger guarantee than a flag set to zero.

    The reason is measured rather than assumed. Every other rule here is tested
    against thirty-six years of history; a news rule cannot be, because the
    free sources serve only the last few days. Turning this on starts building
    the archive that would make that measurement possible later, and until the
    measurement exists the headlines stay out of the decision path. See
    `news.py`."""

    news_per_symbol: int = 10
    news_max_age_hours: int = 72
    """How recent a headline has to be to be summarised by the report. Three
    sessions: long enough to span a weekend, short enough that the market has
    not already priced it."""

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

    def blackout_window(self, status: str) -> tuple[int, int]:
        """Sessions blocked before and after a release of this kind."""
        extra = 0 if status == "confirmed" else self.earnings_estimated_extra
        return (
            self.earnings_blackout_before + extra,
            self.earnings_blackout_after + extra,
        )

    @property
    def sector_of(self) -> dict[str, str]:
        """Symbol -> sector name, for the names that have one."""
        return {
            symbol: name
            for name, members in self.sectors
            for symbol in members.split()
        }

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
