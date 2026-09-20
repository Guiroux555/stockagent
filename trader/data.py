"""Market data: public Yahoo Finance chart endpoint, no API key, read-only.

Three rules matter here, and all three exist to stop the agent fooling itself:

1. **The in-progress session is always discarded.** During market hours the
   endpoint returns today's partial bar, with a "close" that is just the last
   trade. Acting on it means the signal repaints, and a backtest of a
   repainting strategy reports profits that cannot be earned.
2. **Prices are adjusted for splits and dividends.** The raw OHLC is
   split-adjusted only, so a dividend shows up as an overnight gap down — a
   trailing stop would be hit by a payment the holder actually received. The
   adjustment factor is applied to all four prices, which makes the series a
   total-return series and the gap disappear.
3. **Bars are cached in SQLite**, so a backtest replays exactly the data a
   live tick saw, offline and reproducibly.

There is no trading endpoint anywhere in this file, and no credential: the
only verb used is GET.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .config import Settings
from .models import Bar

_USER_AGENT = (
    "Mozilla/5.0 (compatible; stock-paper-trader/0.1; +public market data, read-only)"
)

DEFAULT_SESSION_SECONDS = 6.5 * 3600
"""Length of a regular US equity session, used to date the closing bell when
the payload does not carry a trading period (it usually does)."""


class DataError(RuntimeError):
    pass


class OfflineError(DataError):
    """The link is down, as opposed to this one name being unavailable.

    Worth its own type because the two call for opposite responses: a bad
    ticker is skipped and the sync carries on, while no link at all means every
    remaining name will fail the same way and the sync should stop asking.
    """


class Link:
    """One shared judgement, for the length of a sync, about whether the
    internet is there.

    Without it an outage is quadratically expensive. Every name independently
    tries two hosts three times with backoff — call it twenty seconds of
    sleeping and timing out — and eighty-eight of them turn a dead router into
    a half-hour sync that ends with nothing fetched. That is longer than this
    agent's whole margin before the next closing bell: it would never again be
    idle, and would still know nothing.

    So the first couple of names pay the full retry budget, and once they have
    both failed at the *transport* layer the rest fail instantly. An HTTP error
    does not count: a 404 on one ticker says something about that ticker, not
    about the link.
    """

    def __init__(self, tolerance: int = 2) -> None:
        self.tolerance = tolerance
        self.consecutive = 0
        self.reason = ""

    @property
    def down(self) -> bool:
        return self.consecutive >= self.tolerance

    def failed(self, exc: Exception) -> None:
        self.consecutive += 1
        self.reason = f"{type(exc).__name__}: {exc}"

    def worked(self) -> None:
        self.consecutive = 0
        self.reason = ""

    def check(self, what: str) -> None:
        """Raise instead of dialling, once the link is known to be down."""
        if self.down:
            raise OfflineError(f"link down ({self.reason}) — skipping {what}")


def now_ms() -> int:
    return int(time.time() * 1000)


def _get(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _request(
    path: str, params: dict, settings: Settings, link: Link | None = None
) -> dict:
    """Try each configured host, with backoff, before giving up.

    The two Yahoo hosts serve the same data and rate-limit independently, so
    falling through the list keeps a long sync running when one of them starts
    answering 429.

    Failures are sorted into two kinds on the way out, because they mean
    different things. An HTTP status came from a server, so there *is* a link
    and the problem is this request. A refused connection, a DNS failure or a
    timeout is the link itself — that is what `link` accumulates, and what
    turns the remaining names of an offline sync into instant failures.
    """
    if link is not None:
        link.check(str(params.get("symbol") or path))
    query = urllib.parse.urlencode(params)
    last: Exception | None = None
    transport_only = True
    for host in settings.hosts:
        url = f"{host}{path}?{query}"
        for attempt in range(3):
            try:
                payload = _get(url)
                if link is not None:
                    link.worked()
                return payload
            except urllib.error.HTTPError as exc:
                last = exc
                transport_only = False
                if exc.code in (429, 999):  # rate limited: back off hard
                    time.sleep(2**attempt * 2)
                    continue
                if exc.code == 404:
                    raise DataError(f"unknown symbol for {path}") from None
                if 400 <= exc.code < 500:
                    break  # bad request or blocked host: next host, no retry
                time.sleep(2**attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                # No retry, and no backoff: retrying is for a server that said
                # something (a rate limit, a 5xx). A connection that timed out
                # said nothing, and asking the same unreachable host twice more
                # costs a minute to learn what the first thirty seconds already
                # established. Fall through to the other host instead; if that
                # one is dead too, the next tick is five minutes away and will
                # pick up a blip soon enough.
                last = exc
                break
    if transport_only:
        if link is not None:
            link.failed(last or OfflineError("unreachable"))
        raise OfflineError(f"no host reachable for {path} {params}: {last}")
    raise DataError(f"all hosts failed for {path} {params}: {last}")


def _session_seconds(meta: dict) -> float:
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = period.get("start"), period.get("end")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        return float(end - start)
    return DEFAULT_SESSION_SECONDS


def _to_bars(result: dict) -> list[Bar]:
    """Turn one chart payload into adjusted bars, dropping incomplete rows.

    Yahoo emits `null` for every field of a session it has no data for (an
    exchange holiday it lists anyway, a halt). Those rows are dropped rather
    than forward-filled: a bar that never traded has no high and no low, and
    inventing them would feed the stop a price that never existed.
    """
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    span = _session_seconds(result.get("meta") or {})

    bars: list[Bar] = []
    for i, ts in enumerate(stamps):
        try:
            o, h, low, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            break
        if None in (ts, o, h, low, c) or c <= 0:
            continue
        # Dividend adjustment: scale the whole bar by adjusted-close over
        # close, which preserves the shape of the session and removes the
        # ex-dividend gap the stop would otherwise be hit by.
        factor = 1.0
        if adj is not None and i < len(adj) and adj[i] is not None and c:
            factor = adj[i] / c
        volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
        bars.append(
            Bar(
                open_time=int(ts) * 1000,
                open=o * factor,
                high=h * factor,
                low=low * factor,
                close=c * factor,
                volume=float(volume),
                close_time=int((ts + span) * 1000),
            )
        )
    return bars


def drop_unclosed(bars: list[Bar], at: int | None = None) -> list[Bar]:
    """Remove any session that has not rung its closing bell yet."""
    cutoff = now_ms() if at is None else at
    return [b for b in bars if b.close_time <= cutoff]


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def fetch_bars(
    symbol: str,
    settings: Settings,
    start: str | None = None,
    end_time: int | None = None,
    link: Link | None = None,
) -> list[Bar]:
    """Daily bars for `symbol` from `start` (a YYYY-MM-DD day) to now.

    Unlike a paginated exchange API this endpoint returns the whole requested
    span in one response — forty years of daily bars is about a megabyte — so
    there is no page walk to get wrong.
    """
    params = {
        "period1": _epoch(start) if start else 0,
        "period2": end_time if end_time is not None else 9_999_999_999,
        "interval": settings.interval,
        "events": "div,split",
    }
    payload = _request(
        f"/v8/finance/chart/{urllib.parse.quote(symbol)}", params, settings, link=link
    )
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise DataError(f"{symbol}: {chart['error'].get('description', chart['error'])}")
    results = chart.get("result") or []
    if not results:
        raise DataError(f"{symbol}: empty response")
    return drop_unclosed(_to_bars(results[0]))


ALL_HISTORY = 0
"""Sentinel for `fetch_history`: take everything the source will serve."""


def fetch_history(
    symbol: str,
    settings: Settings,
    bars: int | None = None,
    link: Link | None = None,
) -> list[Bar]:
    """Closed daily bars for `symbol`.

    `bars=ALL_HISTORY` reaches back to `settings.history_start`, which for most
    of this universe means the 1990s and for some of it the 1980s. Any other
    number returns that many of the most recent sessions.
    """
    target = settings.history_bars if bars is None else bars
    unlimited = target == ALL_HISTORY
    start = settings.history_start if unlimited else None
    ordered = fetch_bars(symbol, settings, start=start, link=link)
    if unlimited:
        return ordered
    return ordered[-target:] if target else ordered
