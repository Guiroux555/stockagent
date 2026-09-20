"""Human-readable status of the paper account."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .models import utc
from .store import Store
from .strategy import analyze


def _prices(store: Store, settings: Settings) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol in settings.data_universe:
        bars = store.load_bars(symbol, settings.interval, limit=1)
        if bars:
            out[symbol] = bars[-1].close
    return out



@dataclass
class BudgetCheck:
    """Whether a given virtual budget can actually open a position.

    It is possible to fund this agent with a number that makes it read every
    signal correctly and decline every single one, for a fortnight, silently.
    That is not a bug and it is not visible in any log: two caps cross, and
    every candidate is refused for being too small before a signal is even
    read. So the arithmetic gets its own type, and `fund` prints it.

    Equities are where this bites and crypto is not, for one reason: a share is
    indivisible and some of them cost a thousand dollars. `whole_shares` turning
    this off is what makes a small account expressible at all.
    """

    capital: float
    cap_per_position: float
    reachable: list[str]
    unreachable: list[tuple[str, float]]
    settings: Settings
    needed_for_one: float
    needed_for_all: float
    """What the cached prices say a viable budget is, rather than what the
    arithmetic says. `Settings.min_capital` is a lower bound that is not
    attainable: a whole multiple of the share price has to land inside the
    window, so at exactly the bound almost nothing fits."""

    @property
    def viable(self) -> bool:
        return bool(self.reachable)

    @property
    def priced(self) -> int:
        return len(self.reachable) + len(self.unreachable)

    def report(self) -> str:
        q = self.settings.quote
        lines = []
        if self.viable and not self.unreachable:
            return ""
        if not self.priced:
            return "  (no cached prices yet — run `sync` to check the budget)"

        if not self.viable:
            lines += [
                f"  !! at {self.capital:,.2f} {q} this agent cannot open a single"
                " position.",
                f"     One position is capped at {self.settings.max_position_pct:.0%}"
                f" of equity, so {self.cap_per_position:,.2f} {q}, and the broker",
                f"     minimum is {self.settings.min_notional:,.2f} {q}. Every"
                " candidate is refused before a signal is read.",
            ]
        else:
            worst = max(price for _, price in self.unreachable)
            lines += [
                f"  !  at {self.capital:,.2f} {q}, {len(self.unreachable)} of"
                f" {self.priced} names cannot be entered:",
                f"     one share of the dearest of them costs {worst:,.2f} {q} and"
                f" a position here is capped at {self.cap_per_position:,.2f}.",
                f"     The agent will trade the {len(self.reachable)} it can reach,"
                " which is a different universe from the",
                "     one the backtest measured.",
            ]
        lines += ["", "     Two ways out, and they are not the same decision:"]
        if not self.viable:
            lines.append(
                f"       fund {self.needed_for_one:,.0f}    one position becomes"
                " possible; the settings stay as measured"
            )
        lines += [
            f"       fund {self.needed_for_all:,.0f}    all {self.priced} names"
            " become reachable, which is the universe",
            f"       {'':14}the backtest actually ran on",
            "",
            "       --config config/small-account.json   keeps the budget and makes"
            " shares divisible.",
            f"       {'':38}Then the broker's per-order fee decides",
            f"       {'':38}the outcome, not the strategy — see the",
            f"       {'':38}README. At a euro an order it loses money.",
        ]
        return "\n".join(lines)


def can_enter(price: float, cap: float, settings: Settings) -> bool:
    """Whether one position in a share at `price` fits between the caps.

    With whole shares the smallest expressible position is one share, so the
    question is whether some whole number of them lands in the window at all.
    With fractional shares the price drops out and only the window matters.
    """
    if price <= 0 or cap <= 0:
        return False
    if not settings.whole_shares:
        return cap >= settings.min_notional
    shares = int(cap // price)
    return shares >= 1 and shares * price >= settings.min_notional


def smallest_viable(store: Store, settings: Settings) -> tuple[float, float]:
    """The budget that reaches one name, and the budget that reaches them all.

    `Settings.min_capital` is a lower bound and not an attainable number: with
    whole shares a *whole multiple* of the share price has to land inside the
    window, so at exactly the bound almost nothing fits. This asks the cached
    prices instead of the arithmetic.
    """
    import math

    prices = [p for p in _prices(store, settings).values() if p > 0]
    if not prices:
        return settings.min_capital, settings.min_capital
    if not settings.whole_shares:
        return settings.min_capital, settings.min_capital

    needed = []
    for price in prices:
        shares = max(1, math.ceil(settings.min_notional / price))
        needed.append(shares * price / settings.max_position_pct)
    return min(needed), max(needed)


def budget_check(store: Store, settings: Settings, capital: float) -> BudgetCheck:
    cap = capital * settings.max_position_pct
    reachable: list[str] = []
    unreachable: list[tuple[str, float]] = []
    for symbol, price in _prices(store, settings).items():
        if can_enter(price, cap, settings):
            reachable.append(symbol)
        else:
            unreachable.append((symbol, price))
    one, every = smallest_viable(store, settings)
    return BudgetCheck(
        capital,
        cap,
        sorted(reachable),
        sorted(unreachable),
        settings,
        needed_for_one=one,
        needed_for_all=every,
    )

def status(store: Store, settings: Settings) -> str:
    from .agent import K_CASH, K_HALTED, K_NEXT_RUN, K_PEAK_EQUITY, K_PLAN

    q = settings.quote
    cash = store.get_state(K_CASH, settings.initial_capital)
    positions = store.load_positions()
    orders = store.load_orders()
    prices = _prices(store, settings)
    exposure = sum(p.qty * prices[s] for s, p in positions.items() if s in prices)
    equity = cash + exposure
    peak = store.get_state(K_PEAK_EQUITY, settings.initial_capital)
    trades = store.load_trades()
    plan = store.get_state(K_PLAN, {}) or {}
    nxt = store.get_state(K_NEXT_RUN)

    pnl = equity - settings.initial_capital
    ret = pnl / settings.initial_capital if settings.initial_capital else 0.0
    dd = (1 - equity / peak) if peak else 0.0

    lines = [
        "=" * 74,
        "  PAPER ACCOUNT  (simulation — no broker, no real orders)",
        "=" * 74,
        f"  Equity        {equity:12,.2f} {q}   ({ret:+.2%} since start)",
        f"  Cash          {cash:12,.2f} {q}",
        f"  Exposure      {exposure:12,.2f} {q}   ({exposure / equity:.0%} of equity)"
        if equity
        else "",
        f"  Drawdown      {dd:12.2%} from peak {peak:,.2f} {q}",
        "",
    ]

    if store.get_state(K_HALTED, False):
        resume = settings.drawdown_resume
        lines.append(
            "  !! HALTED on drawdown — no new entries"
            + (
                f" until it recovers to {resume:.0%}"
                if resume is not None
                else " until reset by hand"
            )
        )
        lines.append("")

    if positions:
        lines.append("  OPEN POSITIONS")
        lines.append(
            f"  {'symbol':<8}{'shares':>10}{'entry':>11}{'last':>11}"
            f"{'stop':>11}{'P&L':>12}{'R':>7}"
        )
        for sym, p in sorted(positions.items()):
            last = prices.get(sym, p.entry_price)
            lines.append(
                f"  {sym:<8}{p.qty:>10,.0f}{p.entry_price:>11.2f}{last:>11.2f}"
                f"{p.stop:>11.2f}{p.unrealized(last):>+12,.2f}"
                f"{p.r_multiple(last):>+7.1f}"
            )
    else:
        lines.append("  OPEN POSITIONS   none — fully in cash")

    lines.append("")
    if orders:
        lines.append("  RESTING FOR THE NEXT OPEN  (decided at the last close)")
        for o in orders:
            size = "" if o.fraction >= 1 else f" {o.fraction:.0%} of the position"
            lines.append(
                f"    {o.side:<4} {o.symbol:<8}{size}  signalled at"
                f" {o.signal_price:.2f}  — {o.reason}"
            )
        lines.append("")

    if trades:
        wins = [t for t in trades if t.pnl > 0]
        realized = sum(t.pnl for t in trades)
        lines += [
            f"  CLOSED TRADES {len(trades)}"
            f"   win rate {len(wins) / len(trades):.0%}"
            f"   realized {realized:+,.2f} {q}"
            f"   fees {sum(t.fees for t in trades):,.2f} {q}",
        ]
        for t in trades[-5:]:
            lines.append(
                f"    {utc(t.exit_time):%Y-%m-%d}  {t.symbol:<8}"
                f"{t.pnl:>+11,.2f} {q}  ({t.return_pct:+.2%})  {t.reason}"
            )
    else:
        lines.append("  CLOSED TRADES    none yet")

    lines.append("")
    if plan:
        every = plan.get("decide_every", 1)
        cadence = "every session" if every == 1 else f"every {every} sessions"
        lines.append(f"  Cadence       decides {cadence}  — {plan.get('reason', '')}")
    if nxt:
        wake = datetime.fromisoformat(nxt)
        hours = (wake - datetime.now(timezone.utc)).total_seconds() / 3600
        lines.append(f"  Next decision {wake:%Y-%m-%d %H:%M %Z}  (in {hours:.1f} h)")
    lines.append("=" * 74)
    return "\n".join(x for x in lines if x != "")


def decision_log(store: Store, limit: int = 25) -> str:
    rows = store.recent_decisions(limit)
    if not rows:
        return "no decisions recorded yet"
    out = [f"  last {len(rows)} decisions (most recent first)", "-" * 74]
    for d in rows:
        stamp = f"{utc(d.ts):%Y-%m-%d %H:%M}" if d.ts else "-"
        price = f"@{d.price:.2f}" if d.price else ""
        out.append(f"  {stamp}  {d.action:<4} {d.symbol:<7} {price:>11}  {d.reason}")
    return "\n".join(out)


def watchlist(store: Store, settings: Settings) -> str:
    """What the strategy currently sees on each name, gate by gate, ordered
    strongest first."""
    from .engine import SymbolView
    from .ranking import rank_universe
    from .strategy import entry_signal

    views: dict[str, SymbolView] = {}
    short: list[str] = []
    for symbol in settings.universe:
        # Same bounded window as a live tick: these commands are run over ssh
        # on the board itself, and they should cost what a tick costs.
        bars = store.load_bars(symbol, settings.interval, limit=settings.live_window)
        if len(bars) < settings.warmup_bars:
            short.append(f"  {symbol:<8} not enough history ({len(bars)} sessions)")
            continue
        views[symbol] = SymbolView(analyze(bars, settings), len(bars) - 1)

    ranks = rank_universe(views, settings)
    gate = f"top {settings.rs_top_k}" if settings.rs_top_k > 0 else "filter off"
    out = [f"  WATCHLIST   (RS = relative strength rank, {gate})", "-" * 96]

    for symbol in sorted(views, key=lambda s: ranks.get(s, 10**6)):
        a = views[symbol].analysis
        i = views[symbol].index
        sig = entry_signal(a, settings, i)
        rank = ranks.get(symbol)
        badge = f"#{rank}" if rank else "--"
        out.append(
            f"  {badge:>4} {symbol:<7} {a.closes[i]:>10.2f}  RSI {a.rsi[i]:>5.1f}  "
            f"ATR {a.atr_pct(i):>6.2%}  {sig.action:<4} {sig.reason}"
        )
    return "\n".join(out + short)


def trend_board(store: Store, settings: Settings) -> str:
    """Short and medium-term trends: by sector first, then by name.

    Sector first because that is the order the correlation measurement says to
    read it in — names inside a sector move together, so a name's sector is
    more informative about it than its own last week.
    """
    from .engine import SymbolView
    from .strategy import analyze
    from .trends import HORIZONS, collect, market

    views: dict[str, SymbolView] = {}
    for symbol in settings.universe:
        # The window covers the longest horizon this report reads (12 months,
        # 252 sessions) — see `Settings.live_window`.
        bars = store.load_bars(symbol, settings.interval, limit=settings.live_window)
        if len(bars) >= settings.warmup_bars:
            views[symbol] = SymbolView(analyze(bars, settings), len(bars) - 1)
    if not views:
        return "not enough cached history — run `python -m trader sync` first"

    trends = collect(views, settings)
    if not trends:
        return "no name has enough history for a medium-term reading yet"
    overall = market(trends, settings)
    horizons = [name for name, _ in HORIZONS]

    out = [
        "=" * 92,
        "  TRENDS   returns per horizon; 'risk' columns are the same return"
        " divided by the name's ATR%",
        "=" * 92,
        f"  Market    {overall.rising} rising, {overall.falling} falling of"
        f" {len(trends)}   breadth {overall.breadth:.0%} above EMA200,"
        f" {overall.breadth_short:.0%} up short-term",
        "",
        "  BY SECTOR   (equal weight, strongest medium term first)",
        f"  {'sector':<24}" + "".join(f"{h:>8}" for h in horizons)
        + f"{'short':>9}{'medium':>9}{'breadth':>9}",
        "  " + "-" * 88,
    ]
    for sector in overall.leaders:
        cells = "".join(
            f"{sector.returns.get(h, 0.0):>+8.1%}" for h in horizons
        )
        out.append(
            f"  {sector.name:<24}{cells}"
            f"{sector.short:>+9.2f}{sector.medium:>+9.2f}{sector.breadth:>9.0%}"
        )

    out += [
        "",
        "  BY NAME   (strongest medium term first, top 20)",
        f"  {'name':<8}{'sector':<24}" + "".join(f"{h:>8}" for h in horizons)
        + f"{'medium':>9}{'state':>10}",
        "  " + "-" * 88,
    ]
    of = settings.sector_of
    ranked = sorted(trends.values(), key=lambda t: -t.medium)
    for trend in ranked[:20]:
        cells = "".join(f"{trend.returns.get(h, 0.0):>+8.1%}" for h in horizons)
        flag = trend.label + ("*" if trend.ema_stack else "")
        out.append(
            f"  {trend.symbol:<8}{of.get(trend.symbol, '-'):<24}{cells}"
            f"{trend.medium:>+9.2f}{flag:>10}"
        )
    out.append("")
    out.append("  * every moving average in the ladder pointing the same way")
    out.append("=" * 92)
    return "\n".join(out)


def news_board(store: Store, settings: Settings, limit: int = 30) -> str:
    """The headline archive, most recent first, with what each score is made of.

    Every number here can be traced to the words that produced it. That is the
    only claim this scoring makes: not that it is good, that it is auditable.
    """
    from .models import utc

    items, lo, hi = store.news_span()
    out = [
        "=" * 92,
        "  NEWS ARCHIVE",
        "=" * 92,
    ]
    if not items:
        out += [
            "  empty.",
            "",
            "  Nothing is wrong. The free sources serve only the last few days,"
            " so this archive",
            "  can only be built forwards, one tick at a time. Set"
            " news_enabled and run the agent;",
            "  in a year there will be a year of it, timestamped when it was"
            " seen rather than",
            "  when it is convenient — which is the only version of this data"
            " worth replaying.",
            "=" * 92,
        ]
        return "\n".join(out)

    span_days = (hi - lo) / 86_400_000 if hi > lo else 0.0
    out += [
        f"  {items:,} headline(s) over {span_days:.0f} day(s)"
        f"   {utc(lo):%Y-%m-%d} -> {utc(hi):%Y-%m-%d}",
        "  Archived only — no rule in this agent reads a headline."
        "  See news.py for why.",
        "",
    ]
    for item in store.load_news(limit=limit):
        out.append(
            f"  {utc(item.ts):%Y-%m-%d %H:%M}  {item.symbol:<7}"
            f"{item.score:>+6.2f}  {item.kind:<9} {item.title[:52]:<52}"
        )
        if item.matched:
            out.append(f"  {'':<17}  {'':<7}{'':>6}  {'':<9} -> {item.matched}")
    out.append("=" * 92)
    return "\n".join(out)


def event_board(store: Store, settings: Settings) -> str:
    """Coverage of the earnings calendar, and what is coming.

    Coverage is printed first and in full, because a calendar with holes is
    worse than no calendar: it blocks the sessions it knows about and leaves
    the ones it does not wide open, while looking like protection.
    """
    from .events import COVERAGE_FLOOR, project_next
    from .models import session_date

    coverage = store.earnings_coverage()
    missing = [s for s in settings.universe if s not in coverage]
    counts = [n for n, _, _ in coverage.values()]
    total = sum(counts)

    mode = settings.earnings_mode
    before, after = settings.blackout_window("confirmed")
    est_before, est_after = settings.blackout_window("estimated")

    out = [
        "=" * 84,
        "  EARNINGS CALENDAR   (SEC EDGAR, form 8-K item 2.02)",
        "=" * 84,
        f"  {total:,} release(s) across {len(coverage)}/{len(settings.universe)}"
        f" names   first usable session {COVERAGE_FLOOR}",
    ]
    if missing:
        out.append(f"  no calendar for: {', '.join(sorted(missing))}")
    if counts:
        out.append(
            f"  per name: fewest {min(counts)}, median"
            f" {sorted(counts)[len(counts) // 2]}, most {max(counts)}"
        )
    out += [
        "",
        f"  Mode      {mode}"
        + (
            "   — the calendar is cached and reported but gates nothing"
            if mode == "off"
            else f"   confirmed: -{before}/+{after} sessions,"
            f" estimated: -{est_before}/+{est_after}"
        ),
        "",
        "  NEXT UP   (projected from each company's own filing history)",
        f"  {'name':<8}{'projected':<14}{'last confirmed':<16}{'releases':>9}",
        "  " + "-" * 50,
    ]

    today = session_date(int(datetime.now(timezone.utc).timestamp() * 1000))
    upcoming = []
    for symbol in settings.universe:
        history = store.load_earnings(symbol)
        nxt = project_next(history, today)
        if nxt is not None:
            last = history[-1].event_date if history else "-"
            upcoming.append((nxt.event_date, symbol, last, len(history)))
    for day, symbol, last, n in sorted(upcoming)[:15]:
        out.append(f"  {symbol:<8}{day:<14}{last:<16}{n:>9}")
    if not upcoming:
        out.append("  nothing projected — run `python -m trader events --sync`")
    out.append("")
    out.append(
        "  A projection is an estimate, and a wrong date is worse than no date:"
    )
    out.append(
        "  it blocks the safe session and leaves the dangerous one open."
        "  Hence the wider window."
    )
    out.append("=" * 84)
    return "\n".join(out)
