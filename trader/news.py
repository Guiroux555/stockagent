"""Headlines: fetching them, scoring them, and archiving them.

**Read this before using any of it.** Everything else in this project is
measured against thirty-six years of history. A news signal cannot be, because
the free sources serve only the last few days — there is no archive to replay,
and there is no honest way to backtest a rule that reads one. That is not a
detail to be worked around; it is the reason this module is shaped the way it
is:

1. **Every item seen is written to the database and never deleted.** The agent
   builds its own archive as it runs. After a year of ticks there is a year of
   headlines timestamped at the moment they were *seen*, which is the only
   version of this data that can ever be replayed without hindsight. This is
   the point of the file.
2. **Nothing here touches a trading decision.** There is no veto, no score
   feeding the engine, no setting that would enable one. `Engine.step` does not
   take a news argument, so the absence is structural rather than a default
   someone can flip. When there is an archive long enough to test a rule
   against, that is the moment to write the rule — not before.
3. **It is off by default**, because it costs one HTTP request per name per
   tick and buys nothing today except a longer archive tomorrow.

Two measurements from the first full sync of this universe, which are the
reason for point 2 rather than decoration:

* **Only 12% of the headlines carry any score at all.** The rest are
  syndicated listicles — "Is a Recession Coming in 2026?", "Could $5,000
  Invested in SpaceX Help You Retire" — served under a ticker because the
  ticker appears somewhere in them.
* **The ticker attribution is unreliable in the way that matters.** The feed
  filed "Warren Buffett Steps Down as Berkshire's Chair" under GOOGL and
  "$949 million fraud verdict costs CVS a business" under AMZN. Both score
  −1.00, and both are about another company. A veto built on this would have
  refused entries on the strength of someone else's bad day.

The scoring is a keyword lexicon over headlines. That is a weak instrument and
it is worth saying plainly rather than dressing it up: it cannot read irony, it
cannot weigh a rumour against a filing, and "Apple crushes estimates" and
"Apple crushed by lawsuit" differ by one word it does not understand. It is
here because it is auditable — every score can be traced to the words that
produced it — not because it is good.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import Settings

_USER_AGENT = (
    "Mozilla/5.0 (compatible; stock-paper-trader/0.1; +public market data, read-only)"
)


class NewsError(RuntimeError):
    pass


# --- the lexicon ------------------------------------------------------------
# Deliberately small, deliberately in one place, deliberately readable. A
# thousand-word list tuned until the backtest liked it would be a fit to data
# this module does not have.

NEGATIVE: tuple[str, ...] = (
    "plunge", "plunges", "slump", "slumps", "tumble", "tumbles", "sinks", "sink",
    "crash", "crashes", "misses", "miss", "shortfall", "downgrade", "downgrades",
    "cuts guidance", "cut guidance", "slashes", "warns", "warning", "lawsuit",
    "sues", "probe", "investigation", "subpoena", "fraud", "recall", "layoffs",
    "job cuts", "bankruptcy", "default", "delisting", "halted", "short seller",
    "profit warning", "writedown", "write-down", "impairment", "resigns",
    "steps down", "accounting", "restates", "restatement", "fine", "penalty",
    "antitrust", "sell-off", "selloff", "disappoints", "weak demand", "glut",
)

POSITIVE: tuple[str, ...] = (
    "beats", "beat", "tops", "surge", "surges", "soars", "soar", "jumps", "jump",
    "rally", "rallies", "record", "raises guidance", "raised guidance", "upgrade",
    "upgrades", "buyback", "repurchase", "dividend increase", "raises dividend",
    "approval", "approved", "wins", "win", "acquires", "acquisition", "outperform",
    "strong demand", "expands", "breakthrough", "milestone", "partnership",
    "guidance raise", "profit jumps", "upbeat",
)

KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("earnings", ("earnings", "quarterly results", "q1", "q2", "q3", "q4", "eps",
                  "revenue", "beats", "misses")),
    ("guidance", ("guidance", "outlook", "forecast")),
    ("legal", ("lawsuit", "sues", "probe", "investigation", "antitrust",
               "subpoena", "settlement", "fine", "penalty")),
    ("rating", ("upgrade", "downgrade", "price target", "initiated", "analyst")),
    ("deal", ("acquires", "acquisition", "merger", "buyout", "stake", "spin-off")),
)

_WORD = re.compile(r"[a-z][a-z'\-]+")


@dataclass(frozen=True)
class NewsItem:
    """One headline, scored and stamped."""

    symbol: str
    uid: str
    """Stable id from the source, so the same story is archived once."""
    ts: int
    """Publication time, epoch milliseconds."""
    source: str
    title: str
    link: str
    score: float
    """Lexicon score in [-1, 1]. Zero means "nothing matched", which is not the
    same as "neutral" and is treated as no information rather than as balance."""
    kind: str
    matched: str = ""
    """The words that produced the score, so any number here can be argued
    with."""


def score_headline(title: str) -> tuple[float, str, str]:
    """Score one headline, and say which words did it.

    Returns (score, kind, matched words). A headline that matches nothing
    scores exactly zero and carries no weight later — silence is not neutrality.
    """
    text = title.lower()
    words = set(_WORD.findall(text))

    def matches(term: str) -> bool:
        # Single words match on a word boundary, so "win" does not fire on
        # "winding down"; phrases match as substrings.
        return term in words if " " not in term else term in text

    good = [t for t in POSITIVE if matches(t)]
    bad = [t for t in NEGATIVE if matches(t)]
    total = len(good) + len(bad)
    score = 0.0 if total == 0 else (len(good) - len(bad)) / total

    kind = "general"
    for name, terms in KINDS:
        if any(matches(t) for t in terms):
            kind = name
            break

    matched = ", ".join(sorted(f"+{w}" for w in good) + sorted(f"-{w}" for w in bad))
    return round(score, 4), kind, matched


# --- fetching ---------------------------------------------------------------


def _get(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch(symbol: str, settings: Settings, limit: int = 10) -> list[NewsItem]:
    """Recent headlines for one symbol, from the public search endpoint.

    No key, no account, read-only, and no history: this returns what is recent
    and nothing else. That limitation is the whole reason for the archive.
    """
    params = urllib.parse.urlencode(
        {
            "q": symbol,
            "newsCount": max(1, min(limit, 20)),
            "quotesCount": 0,
            "enableFuzzyQuery": "false",
        }
    )
    last: Exception | None = None
    for host in settings.hosts:
        for attempt in range(2):
            try:
                payload = _get(f"{host}/v1/finance/search?{params}")
                return _to_items(symbol, payload.get("news") or [])
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (429, 999):
                    time.sleep(2**attempt * 2)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(2**attempt)
    raise NewsError(f"no headlines for {symbol}: {last}")


def _to_items(symbol: str, rows: list) -> list[NewsItem]:
    items: list[NewsItem] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        uid = row.get("uuid") or row.get("link") or title
        published = row.get("providerPublishTime")
        if not title or not uid or not published:
            continue
        score, kind, matched = score_headline(title)
        items.append(
            NewsItem(
                symbol=symbol,
                uid=str(uid),
                ts=int(published) * 1000,
                source=(row.get("publisher") or "unknown").strip(),
                title=title,
                link=row.get("link") or "",
                score=score,
                kind=kind,
                matched=matched,
            )
        )
    return items


def sync(store, settings: Settings, log=print, pause: float = 0.3) -> dict[str, int]:
    """Fetch headlines for the whole universe and add them to the archive.

    Nothing is ever replaced or deleted: an item already archived keeps the
    timestamp at which it was *first seen*, which is the only field that makes
    a future replay honest. Rewriting it on every sync would quietly turn the
    archive into a hindsight dataset.
    """
    counts: dict[str, int] = {}
    for symbol in settings.universe:
        try:
            items = fetch(symbol, settings, limit=settings.news_per_symbol)
            counts[symbol] = store.save_news(items)
        except NewsError as exc:
            log(f"  ! {symbol}: {exc}")
            counts[symbol] = 0
        time.sleep(pause)
    return counts


# --- reading the archive ----------------------------------------------------


@dataclass
class NewsView:
    """What the archive says about one name, right now."""

    symbol: str
    score: float = 0.0
    items: int = 0
    """Items that actually carried a score. A name with ten headlines none of
    which matched the lexicon has zero items here, not ten."""
    worst: float = 0.0
    latest_ts: int = 0
    kinds: tuple[str, ...] = ()
    headlines: list[str] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.items > 0


def assess(items: list[NewsItem], now_ms: int, settings: Settings) -> NewsView:
    """Aggregate recent scored headlines for one name.

    Recency-weighted, linearly to zero at the edge of the window: a headline
    from four hours ago and one from three days ago are not the same event, and
    averaging them flat pretends they are.
    """
    if not items:
        return NewsView(symbol="")
    symbol = items[0].symbol
    window = settings.news_max_age_hours * 3_600_000
    if window <= 0:
        return NewsView(symbol=symbol)

    weighted = 0.0
    weights = 0.0
    counted = 0
    worst = 0.0
    latest = 0
    kinds: list[str] = []
    headlines: list[str] = []

    for item in sorted(items, key=lambda x: -x.ts):
        age = now_ms - item.ts
        if age < 0 or age > window:
            continue
        latest = max(latest, item.ts)
        if item.score == 0.0:
            continue  # matched nothing: no information, not balance
        weight = 1.0 - age / window
        weighted += item.score * weight
        weights += weight
        counted += 1
        worst = min(worst, item.score)
        kinds.append(item.kind)
        if len(headlines) < 5:
            headlines.append(f"[{item.score:+.2f}] {item.title}")

    return NewsView(
        symbol=symbol,
        score=round(weighted / weights, 4) if weights else 0.0,
        items=counted,
        worst=worst,
        latest_ts=latest,
        kinds=tuple(dict.fromkeys(kinds)),
        headlines=headlines,
    )


