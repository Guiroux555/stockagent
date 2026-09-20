"""Earnings dates: where they come from, and what the agent is allowed to do
with them.

`risk.py` makes one promise: a position loses `risk_per_trade` of equity if the
stop is hit. An earnings release breaks that promise, and not by a little. The
stop is not crossed, it is *jumped over* — `strategy.stop_hit` already models
that honestly by filling at the open rather than at the stop — so the loss on
an overnight gap is unbounded by anything in the risk layer. `max_gap_atr`
covers the entry. Nothing covered the holding, and that is what this module is
for.

**This is not a news module, and the distinction is the whole design.**
`news.py` collects headlines and is wired to nothing, because a sentiment
signal cannot be backtested here. An earnings *date* is a different object:

* It is announced weeks ahead, so knowing it on the day before is not
  hindsight — it is what every participant knew.
* It comes from a filing, not an interpretation. There is no model in the
  path, so there is nothing for a model to know about the future.
* It says *when*, never *what*. This module has no opinion on whether the
  results will be good, which is precisely why it can only ever refuse a
  trade, never suggest one.

The source is SEC EDGAR: form 8-K carrying item 2.02, "Results of Operations
and Financial Condition". Free, official, keyless, GET-only, and
point-in-time by construction — a filing date is what it is and is never
restated. Coverage starts in **late 2004**, when item 2.02 was created; before
that, earnings were released by press release with no filing to key on, and
this module simply has nothing to say. That limit is real and the backtest
reports it rather than pretending the early years were quiet.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Settings
from .models import EXCHANGE_TZ

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_SHARD = "https://data.sec.gov/submissions/{name}"

EARNINGS_ITEM = "2.02"
"""The 8-K item code for "Results of Operations and Financial Condition".
An 8-K without it is a different event — a director leaving, a covenant
waiver — and not the one this module is about."""

CLOSING_HOUR = 16
"""Exchange-local hour after which a filing can only move the *next* open."""


class EventError(RuntimeError):
    pass


@dataclass(frozen=True)
class EarningsDate:
    """One earnings release, dated by the session it can actually move.

    `event_date` is not the filing date. A release accepted at 16:30 New York
    time cannot move a market that shut at 16:00, so its gap lands on the next
    session — and dating it by the filing day would put the blackout one day
    early and leave the real day open.
    """

    symbol: str
    event_date: str
    """Exchange-local calendar date of the session the release can move."""
    filed_date: str
    accepted_ts: int
    status: str
    """`confirmed` for a filing that exists, `estimated` for one projected from
    the company's own history."""
    source: str = "sec-8k-2.02"
    fetched_at: int = 0
    """When this row was written. Kept so that a future version can replay what
    was known at a given moment rather than what is known now."""

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


# --- fetching ---------------------------------------------------------------


def _get(url: str, settings: Settings, timeout: float = 30.0) -> dict:
    # The SEC asks for a contact address in the User-Agent and refuses traffic
    # without one. It is a courtesy header, not a credential: nothing here
    # identifies an account and nothing here can write.
    req = urllib.request.Request(url, headers={"User-Agent": settings.sec_user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _retrying(url: str, settings: Settings) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return _get(url, settings)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise EventError(f"not found: {url}") from None
            if exc.code == 403:
                # Almost always the User-Agent: EDGAR wants a contact address
                # in it and says nothing useful when there is none.
                raise EventError(
                    "EDGAR refused the request (403). Its User-Agent rule wants"
                    " a contact address; set `sec_user_agent` to something like"
                    ' "stock-paper-trader/0.1 (you@example.com)".'
                ) from None
            time.sleep(2**attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise EventError(f"failed: {url}: {last}")


def cik_map(settings: Settings) -> dict[str, int]:
    """Ticker to SEC central index key, for the whole market."""
    payload = _retrying(SEC_TICKERS, settings)
    return {
        str(row["ticker"]).upper(): int(row["cik_str"])
        for row in payload.values()
        if row.get("ticker") and row.get("cik_str") is not None
    }


def _effect_date(accepted: str, filed: str) -> str:
    """Which session a filing accepted at `accepted` can actually move.

    Acceptance timestamps are UTC. Anything at or after the closing bell in New
    York lands on the next calendar day; the next *session* is then whatever
    the price series says it is, which is how holidays take care of themselves.
    """
    try:
        stamp = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
    except ValueError:
        return filed
    local = stamp.astimezone(EXCHANGE_TZ)
    if local.hour >= CLOSING_HOUR:
        return (local.date() + timedelta(days=1)).isoformat()
    return local.date().isoformat()


def _rows(block: dict, symbol: str, now_ms: int) -> list[EarningsDate]:
    forms = block.get("form") or []
    items = block.get("items") or []
    filed = block.get("filingDate") or []
    accepted = block.get("acceptanceDateTime") or []
    out: list[EarningsDate] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        codes = items[i] if i < len(items) else ""
        if EARNINGS_ITEM not in (codes or ""):
            continue
        filing = filed[i] if i < len(filed) else ""
        stamp = accepted[i] if i < len(accepted) else ""
        if not filing:
            continue
        out.append(
            EarningsDate(
                symbol=symbol,
                event_date=_effect_date(stamp, filing),
                filed_date=filing,
                accepted_ts=_epoch_ms(stamp),
                status="confirmed",
                fetched_at=now_ms,
            )
        )
    return out


def _epoch_ms(stamp: str) -> int:
    try:
        return int(
            datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return 0


def fetch(symbol: str, cik: int, settings: Settings) -> list[EarningsDate]:
    """Every earnings 8-K this company has ever filed.

    EDGAR splits a long filing history: the most recent years sit in the main
    document and the rest in shards it names. Reading only the first would
    silently stop the history around 2015 for an old company, which is the kind
    of gap a backtest hides rather than reports.
    """
    now_ms = int(time.time() * 1000)
    payload = _retrying(SEC_SUBMISSIONS.format(cik=cik), settings)
    filings = payload.get("filings") or {}
    out = _rows(filings.get("recent") or {}, symbol, now_ms)
    for shard in filings.get("files") or []:
        name = shard.get("name")
        if not name:
            continue
        time.sleep(settings.sec_pause)
        out += _rows(_retrying(SEC_SHARD.format(name=name), settings), symbol, now_ms)
    return sorted(out, key=lambda e: e.event_date)


def sync(store, settings: Settings, log=print) -> dict[str, int]:
    """Fetch and cache every earnings date for the universe.

    A symbol that fails is reported and skipped rather than aborting the run.
    """
    try:
        ciks = cik_map(settings)
    except EventError as exc:
        log(f"  ! ticker map unavailable: {exc}")
        return {}

    extra = {sym: [int(c) for c in more.split()] for sym, more in settings.cik_history}

    counts: dict[str, int] = {}
    for symbol in settings.universe:
        cik = ciks.get(symbol.upper())
        filers = ([cik] if cik is not None else []) + extra.get(symbol, [])
        if not filers:
            log(f"  ! {symbol}: no CIK in the SEC ticker map")
            counts[symbol] = 0
            continue
        rows: list[EarningsDate] = []
        for filer in filers:
            try:
                rows += fetch(symbol, filer, settings)
            except EventError as exc:
                log(f"  ! {symbol} (CIK {filer}): {exc}")
            time.sleep(settings.sec_pause)
        counts[symbol] = store.save_earnings(rows)
    return counts


@dataclass
class Coverage:
    """How complete one name's calendar is, against how much price history it
    has. A hole here is not cosmetic: it is a session the filter believes is
    safe."""

    symbol: str
    releases: int
    expected: int
    first: str = ""
    last: str = ""

    @property
    def ratio(self) -> float:
        return self.releases / self.expected if self.expected else 0.0

    def usable(self, settings: Settings) -> bool:
        return self.ratio >= settings.min_calendar_coverage


def coverage(store, settings: Settings, floor: str = "2005-01-01") -> list[Coverage]:
    """Compare each name's calendar with the price history it has to cover.

    Roughly four releases a year, counted only over the span the price series
    and the calendar could both speak for. A name whose shares started trading
    in 2012 is not missing the years before it existed.
    """
    cached = store.earnings_coverage()
    out: list[Coverage] = []
    for symbol in settings.universe:
        bars = store.load_bars(symbol, settings.interval)
        if not bars:
            continue
        start = max(bars[0].session, floor)
        end = bars[-1].session
        years = max((int(end[:4]) - int(start[:4])) + 1, 1)
        rows, lo, hi = cached.get(symbol, (0, "", ""))
        out.append(Coverage(symbol, rows, years * 4, lo, hi))
    return out


# --- projecting the next one, from past filings only ------------------------


def project_next(history: list[EarningsDate], after: str) -> EarningsDate | None:
    """The next release after `after`, estimated from the company's own past.

    Companies report on a calendar they keep: the same fiscal quarter lands
    within a few days of the same date a year later. Projecting 364 days
    forward from the matching quarter (a whole number of weeks, so the weekday
    is preserved) is what a human does with the same information.

    It reads only filings that already happened, so it is safe to call inside a
    backtest — which is the reason the projection exists at all rather than
    being fetched from a calendar endpoint that would hand the backtest a date
    nobody knew yet.
    """
    past = [e for e in history if e.event_date <= after and e.confirmed]
    if len(past) < 4:
        return None
    anchor = past[-4]  # the same fiscal quarter, one year back
    try:
        projected = date.fromisoformat(anchor.event_date) + timedelta(days=364)
    except ValueError:
        return None
    if projected.isoformat() <= after:
        return None
    return EarningsDate(
        symbol=anchor.symbol,
        event_date=projected.isoformat(),
        filed_date="",
        accepted_ts=0,
        status="estimated",
        source="projected-from-8k",
        fetched_at=int(time.time() * 1000),
    )


def known_at(
    history: list[EarningsDate], as_of: str, horizon_days: int = 120
) -> list[EarningsDate]:
    """What a participant standing on session `as_of` could know.

    Confirmed releases up to today, plus one projection for the next one. A
    confirmed date in the future is exactly the thing a backtest must not see,
    so it is filtered out here rather than trusted not to matter.
    """
    past = [e for e in history if e.confirmed and e.event_date <= as_of]
    out = list(past)
    nxt = project_next(history, as_of)
    if nxt is not None:
        try:
            ahead = (
                date.fromisoformat(nxt.event_date) - date.fromisoformat(as_of)
            ).days
        except ValueError:
            ahead = 0
        if 0 < ahead <= horizon_days:
            out.append(nxt)
    return out


# --- the filter, pure -------------------------------------------------------


@dataclass
class EventWindow:
    """Where a session sits relative to the nearest earnings release."""

    symbol: str
    sessions_until: int | None = None
    sessions_since: int | None = None
    status: str = ""
    event_date: str = ""

    @property
    def inside(self) -> bool:
        return self.sessions_until is not None or self.sessions_since is not None


def window_for(
    symbol: str,
    sessions: list[str],
    index: int,
    events: list[EarningsDate],
    settings: Settings,
) -> EventWindow:
    """Is session `index` inside a blackout, and by how much?

    `sessions` is the symbol's own calendar of exchange-local dates, so the
    distances below are counted in trading sessions rather than in days. A
    three-day window that quietly becomes a one-day window over a long weekend
    is not a window.
    """
    if not events or index < 0 or index >= len(sessions):
        return EventWindow(symbol)

    position = {day: k for k, day in enumerate(sessions)}

    best = EventWindow(symbol)
    for event in events:
        # An event date that is not itself a session (a filing dated on a
        # holiday) lands on the next session that exists.
        slot = position.get(event.event_date)
        if slot is None:
            later = [k for k, day in enumerate(sessions) if day >= event.event_date]
            if not later:
                continue
            slot = later[0]

        before, after = settings.blackout_window(event.status)
        delta = slot - index
        if 0 <= delta <= before:
            if best.sessions_until is None or delta < best.sessions_until:
                best = EventWindow(symbol, delta, None, event.status, event.event_date)
        elif -after <= delta < 0:
            since = -delta
            if best.sessions_since is None and best.sessions_until is None:
                best = EventWindow(symbol, None, since, event.status, event.event_date)
    return best


def passes(window: EventWindow, settings: Settings) -> tuple[bool, float, str]:
    """Does an entry survive the earnings calendar?

    Returns (allowed, size multiplier, reason). `reduce` keeps the trade and
    shrinks it, which is the more surgical answer: the breakout is still a
    breakout, it is the unbounded gap that is the problem, and a smaller
    position bounds it.

    It gates entries only. Exits are never touched — the invariant of this
    project is that an agent which cannot close a losing position is exactly
    the wrong way round, and a calendar is no reason to make an exception.
    """
    if settings.earnings_mode == "off" or not window.inside:
        return True, 1.0, ""

    when = (
        f"earnings in {window.sessions_until} session(s)"
        if window.sessions_until is not None
        else f"earnings {window.sessions_since} session(s) ago"
    )
    tag = f"{when} ({window.status} {window.event_date})"

    if settings.earnings_mode == "reduce":
        return True, settings.earnings_size_factor, f"halved for {tag}"
    return False, 0.0, tag


# --- the measurement that decides whether any of this is worth it -----------


COVERAGE_FLOOR = "2005-01-01"
"""First session the calendar can speak for. Item 2.02 was created in August
2004, so everything before is silence rather than quiet."""


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class GapStudy:
    """Opening gaps on earnings sessions against every other session.

    The question this answers is the one that has to come before any filter:
    is there a tail to cut? A blackout that blocks sessions no more dangerous
    than the rest is a filter that only costs trades.
    """

    symbols: int = 0
    event_sessions: int = 0
    other_sessions: int = 0
    event_gaps: list[float] = None
    other_gaps: list[float] = None
    event_atr: list[float] = None
    other_atr: list[float] = None
    atr_pct: dict[str, float] = None

    def summary(self, settings: Settings) -> str:
        eg, og = self.event_gaps or [], self.other_gaps or []
        if not eg or not og:
            return "no overlap between the price history and the calendar"
        lines = [
            "=" * 76,
            f"  OPENING GAPS   earnings sessions vs the rest, since {COVERAGE_FLOOR}",
            "=" * 76,
            f"  {self.symbols} names   {self.event_sessions:,} earnings sessions"
            f"   {self.other_sessions:,} other sessions",
            "",
            f"  {'|open / previous close - 1|':<34}{'earnings':>12}"
            f"{'other':>12}{'ratio':>10}",
            "  " + "-" * 72,
        ]
        for label, q in (
            ("median", 0.50),
            ("75th percentile", 0.75),
            ("90th percentile", 0.90),
            ("95th percentile", 0.95),
            ("99th percentile", 0.99),
        ):
            e, o = _percentile(eg, q), _percentile(og, q)
            lines.append(
                f"  {label:<34}{e:>11.2%}{o:>12.2%}{(e / o if o else 0):>9.2f}x"
            )
        lines.append(
            f"  {'worst':<34}{max(eg):>11.2%}{max(og):>12.2%}"
            f"{(max(eg) / max(og) if og else 0):>9.2f}x"
        )
        lines.append("")
        ea, oa = self.event_atr or [], self.other_atr or []
        for k in (2, 4, 6, 8):
            fe = sum(1 for m in ea if m > k) / len(ea) if ea else 0.0
            fo = sum(1 for m in oa if m > k) / len(oa) if oa else 0.0
            lines.append(
                f"  {'gap beyond ' + str(k) + ' x ATR':<34}{fe:>11.2%}{fo:>12.2%}"
                f"{(fe / fo if fo else 0):>9.0f}x"
            )

        if self.atr_pct:
            vals = sorted(self.atr_pct.values())
            mid = vals[len(vals) // 2]
            lines += [
                "",
                "  WHAT A GAP COSTS   (real ATR per name, not a round assumption)",
                f"  median daily ATR across {len(vals)} names: {mid:.2%}"
                f"   range {vals[0]:.2%} to {vals[-1]:.2%}",
            ]
            for atr in (vals[0], mid, vals[-1]):
                distance = settings.stop_atr_mult * atr
                by_risk = settings.risk_per_trade / distance
                size = min(by_risk, settings.max_position_pct)
                binding = "risk budget" if by_risk <= settings.max_position_pct else "position cap"
                for gap in (0.20, 0.35):
                    loss = size * gap
                    lines.append(
                        f"    ATR {atr:>5.2%}  position {size:>5.2%} of equity"
                        f" ({binding:<12})  a -{gap:.0%} gap costs {loss:>5.2%}"
                        f" = {loss / settings.risk_per_trade:>4.1f}x the budget"
                    )
        lines.append("=" * 76)
        return "\n".join(lines)


def gap_study(store, settings: Settings, floor: str = COVERAGE_FLOOR) -> GapStudy:
    """Compare opening gaps on earnings sessions with every other session.

    The ATR used for each session is the one from the session *before*, so the
    comparison never measures a gap against volatility the gap itself created.
    """
    from .strategy import analyze

    study = GapStudy(
        event_gaps=[], other_gaps=[], event_atr=[], other_atr=[], atr_pct={}
    )
    for symbol in settings.universe:
        bars = store.load_bars(symbol, settings.interval)
        if len(bars) < settings.warmup_bars:
            continue
        dates = {e.event_date for e in store.load_earnings(symbol) if e.confirmed}
        if not dates:
            continue
        study.symbols += 1
        a = analyze(bars, settings)
        widths: list[float] = []
        for i in range(1, len(bars)):
            if bars[i].session < floor:
                continue
            previous = bars[i - 1].close
            atr = a.atr[i - 1]
            if previous <= 0 or atr is None or atr <= 0:
                continue
            gap = abs(bars[i].open / previous - 1.0)
            in_atr = abs(bars[i].open - previous) / atr
            widths.append(atr / previous)
            if bars[i].session in dates:
                study.event_gaps.append(gap)
                study.event_atr.append(in_atr)
            else:
                study.other_gaps.append(gap)
                study.other_atr.append(in_atr)
        if widths:
            study.atr_pct[symbol] = _percentile(widths, 0.5)

    study.event_sessions = len(study.event_gaps)
    study.other_sessions = len(study.other_gaps)
    return study
