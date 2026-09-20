"""Human-readable status of the paper account."""

from __future__ import annotations

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
        bars = store.load_bars(symbol, settings.interval)
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
