"""SQLite persistence: one file holds the cached sessions, the account, the
open positions, the orders queued for the next open, every closed trade and the
full decision log.

One file means the whole state of the agent can be backed up by copying it, and
inspected with any SQLite browser — which matters when you need to answer "why
did it buy that?" three weeks later.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from .models import Bar, Decision, Order, Position, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    open_time  INTEGER NOT NULL,
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     REAL NOT NULL,
    close_time INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT PRIMARY KEY,
    qty          REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    stop         REAL NOT NULL,
    peak         REAL NOT NULL,
    atr_at_entry REAL NOT NULL,
    initial_risk REAL NOT NULL DEFAULT 0,
    scaled_out   INTEGER NOT NULL DEFAULT 0,
    bars_held    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_orders (
    symbol       TEXT PRIMARY KEY,
    side         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    signal_price REAL NOT NULL,
    stop         REAL NOT NULL DEFAULT 0,
    atr          REAL NOT NULL DEFAULT 0,
    fraction     REAL NOT NULL DEFAULT 1,
    size_factor  REAL NOT NULL DEFAULT 1,
    created_ts   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS earnings (
    symbol      TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    filed_date  TEXT NOT NULL DEFAULT '',
    accepted_ts INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'confirmed',
    source      TEXT NOT NULL DEFAULT '',
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (symbol, event_date)
);
CREATE INDEX IF NOT EXISTS idx_earnings_symbol ON earnings(symbol, event_date);

CREATE TABLE IF NOT EXISTS news (
    uid        TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    source     TEXT NOT NULL,
    title      TEXT NOT NULL,
    link       TEXT NOT NULL DEFAULT '',
    score      REAL NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL DEFAULT 'general',
    matched    TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (symbol, uid)
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_ts ON news(symbol, ts);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    qty         REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price  REAL NOT NULL,
    entry_time  INTEGER NOT NULL,
    exit_time   INTEGER NOT NULL,
    fees        REAL NOT NULL,
    pnl         REAL NOT NULL,
    reason      TEXT NOT NULL,
    partial     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS decisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    symbol  TEXT NOT NULL,
    action  TEXT NOT NULL,
    reason  TEXT NOT NULL,
    price   REAL NOT NULL,
    qty     REAL NOT NULL DEFAULT 0,
    equity  REAL NOT NULL DEFAULT 0,
    meta    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts       INTEGER PRIMARY KEY,
    equity   REAL NOT NULL,
    cash     REAL NOT NULL,
    exposure REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts INTEGER NOT NULL,
    ended_ts   INTEGER,
    initial    REAL NOT NULL,
    final      REAL,
    trades     INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: str | Path = "data/trader.db"):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # WAL alone survives a crash; it does not survive the plug being
        # pulled, which on a board with no battery is the ordinary way to turn
        # it off. Under `synchronous=NORMAL` — the default with WAL — the last
        # commits sit in the page cache and a power cut takes them, so the
        # agent comes back believing it still holds a position it closed. FULL
        # fsyncs every commit. A tick commits a handful of times and writes a
        # few kilobytes, so the cost is unmeasurable here and the guarantee is
        # exactly the one an unattended appliance needs.
        self.conn.execute("PRAGMA synchronous=FULL")
        # A `status` run and a backup can read while the agent writes.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that later versions introduced.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a
        database from an earlier version keeps its old shape and the next
        insert fails on a column count. Adding them here means an existing
        paper account survives an upgrade instead of having to be wiped.
        """
        added = {
            "pending_orders": {
                "fraction": "REAL NOT NULL DEFAULT 1",
                "size_factor": "REAL NOT NULL DEFAULT 1",
            },
            "positions": {
                "initial_risk": "REAL NOT NULL DEFAULT 0",
                "scaled_out": "INTEGER NOT NULL DEFAULT 0",
            },
            "trades": {"partial": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, columns in added.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

    def close(self) -> None:
        # Fold the WAL back into the main file so the database is one file
        # again — which is what makes `cp live.db` a valid backup and what
        # keeps an SD card from carrying an ever-growing sidecar.
        with contextlib.suppress(sqlite3.Error):
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- integrity & backup -----------------------------------------------

    def integrity_check(self) -> str | None:
        """`None` if the file is sound, else what SQLite says is wrong with it.

        Called on every start, because the failure this guards against is not
        hypothetical on a Pi: a power cut during a write on a cheap SD card can
        leave a file that opens fine and fails on the first query. Finding that
        out at boot, next to a known-good backup, beats finding it out three
        weeks later in a stack trace.
        """
        try:
            rows = self.conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            return str(exc)
        verdict = [r[0] for r in rows]
        return None if verdict == ["ok"] else "; ".join(verdict)

    def backup(self, dest: str | Path) -> Path:
        """Copy the whole database to `dest`, online and atomically.

        `sqlite3`'s own backup API is used rather than `cp`, because the agent
        may well be mid-write: copying the file by hand while a WAL is open
        produces something that looks like a database and is not one. The copy
        lands on a temporary name and is renamed into place, so an interrupted
        backup can never replace a good one with half a file.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.unlink(missing_ok=True)
        with sqlite3.connect(str(tmp)) as out:
            self.conn.backup(out)
        tmp.replace(dest)
        return dest

    # --- bars -------------------------------------------------------------

    def save_bars(self, symbol: str, interval: str, bars: Iterable[Bar]) -> int:
        rows = [
            (
                symbol,
                interval,
                b.open_time,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.close_time,
            )
            for b in bars
        ]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()
        return len(rows)

    def replace_bars(
        self,
        symbol: str,
        interval: str,
        bars: Iterable[Bar],
        known: dict[int, Bar] | None = None,
    ) -> int:
        """Make the stored series *be* `bars`, and write only what differs.

        The replacing is needed because equity prices are retroactively
        restated: a split or a dividend rewrites every bar before it. Merging a
        freshly adjusted series into stale rows would leave a discontinuity at
        the join, and the agent would read it as a gap that never happened.

        Writing only the difference is needed because of where this now runs. A
        `DELETE` plus a full re-`INSERT` rewrote every row of the table on every
        sync — eighty-eight names times thirty-six years of sessions, several
        hundred thousand rows a day onto an SD card, to record one new close.
        Flash wears out. The result is identical either way: rows the exchange
        no longer serves are deleted, changed rows are overwritten, and on an
        ordinary day exactly one row is written.

        `known` is the caller's already-loaded copy of the series, since the
        sync reads it anyway to count restatements; omit it and it is read here.
        """
        incoming = {b.open_time: b for b in bars}
        if not incoming:
            # An empty fetch is a failed fetch, not a delisting. Keeping the
            # cache is the difference between one bad sync and a cold start.
            return 0
        if known is None:
            known = {b.open_time: b for b in self.load_bars(symbol, interval)}

        gone = [t for t in known if t not in incoming]
        if gone:
            self.conn.executemany(
                "DELETE FROM bars WHERE symbol=? AND interval=? AND open_time=?",
                [(symbol, interval, t) for t in gone],
            )
        changed = [b for t, b in incoming.items() if known.get(t) != b]
        self.save_bars(symbol, interval, changed)
        self.conn.commit()
        return len(incoming)

    def load_bars(
        self,
        symbol: str,
        interval: str,
        limit: int | None = None,
        until: int | None = None,
    ) -> list[Bar]:
        sql = "SELECT * FROM bars WHERE symbol=? AND interval=?"
        args: list = [symbol, interval]
        if until is not None:
            sql += " AND close_time<=?"
            args.append(until)
        # The tail is taken in SQL, not in python. A live tick needs a few
        # hundred sessions per name; reading thirty-six years of them into a
        # list and throwing all but the tail away is the difference between a
        # few megabytes and a few hundred on a board that may only have one.
        if limit:
            sql += " ORDER BY open_time DESC LIMIT ?"
            args.append(limit)
        else:
            sql += " ORDER BY open_time"
        rows = self.conn.execute(sql, args).fetchall()
        if limit:
            rows = rows[::-1]
        bars = [
            Bar(
                r["open_time"],
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                r["volume"],
                r["close_time"],
            )
            for r in rows
        ]
        return bars

    def last_bar_time(self, symbol: str, interval: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(open_time) AS t FROM bars WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        return row["t"]

    # --- positions --------------------------------------------------------

    def save_position(self, pos: Position) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO positions (symbol,qty,entry_price,entry_time,"
            "stop,peak,atr_at_entry,initial_risk,scaled_out,bars_held)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                pos.symbol,
                pos.qty,
                pos.entry_price,
                pos.entry_time,
                pos.stop,
                pos.peak,
                pos.atr_at_entry,
                pos.initial_risk,
                int(pos.scaled_out),
                pos.bars_held,
            ),
        )
        self.conn.commit()

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.conn.commit()

    def load_positions(self) -> dict[str, Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return {
            r["symbol"]: Position(
                symbol=r["symbol"],
                qty=r["qty"],
                entry_price=r["entry_price"],
                entry_time=r["entry_time"],
                stop=r["stop"],
                peak=r["peak"],
                atr_at_entry=r["atr_at_entry"],
                scaled_out=bool(r["scaled_out"]),
                initial_risk=r["initial_risk"],
                bars_held=r["bars_held"],
            )
            for r in rows
        }

    # --- orders queued for the next open ----------------------------------

    def save_orders(self, orders: Iterable[Order]) -> None:
        """Replace the whole queue.

        The queue is never appended to across sessions: an order not filled at
        the next open is an order that has expired, and carrying it forward
        would have the agent buying a breakout it decided on a week ago.
        """
        self.conn.execute("DELETE FROM pending_orders")
        self.conn.executemany(
            "INSERT OR REPLACE INTO pending_orders"
            " (symbol,side,reason,signal_price,stop,atr,fraction,size_factor,"
            "created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    o.symbol,
                    o.side,
                    o.reason,
                    o.signal_price,
                    o.stop,
                    o.atr,
                    o.fraction,
                    o.size_factor,
                    o.created_ts,
                )
                for o in orders
            ],
        )
        self.conn.commit()

    def load_orders(self) -> list[Order]:
        rows = self.conn.execute(
            "SELECT * FROM pending_orders ORDER BY symbol"
        ).fetchall()
        return [
            Order(
                symbol=r["symbol"],
                side=r["side"],
                reason=r["reason"],
                signal_price=r["signal_price"],
                stop=r["stop"],
                atr=r["atr"],
                fraction=r["fraction"],
                size_factor=r["size_factor"],
                created_ts=r["created_ts"],
            )
            for r in rows
        ]

    # --- earnings calendar -------------------------------------------------

    def save_earnings(self, rows: Iterable) -> int:
        """Cache earnings dates, keeping the `fetched_at` of the first sight.

        `INSERT OR IGNORE`, for the same reason as the news archive: a filing
        date is not restated, so a row that changes is a row that was wrong,
        and overwriting it would erase the evidence rather than the error.
        """
        payload = [
            (
                r.symbol,
                r.event_date,
                r.filed_date,
                r.accepted_ts,
                r.status,
                r.source,
                r.fetched_at or int(time.time() * 1000),
            )
            for r in rows
        ]
        if not payload:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO earnings"
            " (symbol,event_date,filed_date,accepted_ts,status,source,fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            payload,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def load_earnings(self, symbol: str | None = None) -> list:
        from .events import EarningsDate

        sql = "SELECT * FROM earnings"
        args: list = []
        if symbol:
            sql += " WHERE symbol=?"
            args.append(symbol)
        sql += " ORDER BY symbol, event_date"
        return [
            EarningsDate(
                symbol=r["symbol"],
                event_date=r["event_date"],
                filed_date=r["filed_date"],
                accepted_ts=r["accepted_ts"],
                status=r["status"],
                source=r["source"],
                fetched_at=r["fetched_at"],
            )
            for r in self.conn.execute(sql, args).fetchall()
        ]

    def earnings_coverage(self) -> dict[str, tuple[int, str, str]]:
        """Per symbol: how many releases are cached, and the span they cover.

        Printed rather than assumed, because a calendar with holes blocks the
        wrong sessions and leaves the right ones open, which is worse than
        having no calendar at all.
        """
        rows = self.conn.execute(
            "SELECT symbol, COUNT(*) AS n, MIN(event_date) AS lo,"
            " MAX(event_date) AS hi FROM earnings GROUP BY symbol"
        ).fetchall()
        return {r["symbol"]: (int(r["n"]), r["lo"] or "", r["hi"] or "") for r in rows}

    # --- news archive -----------------------------------------------------

    def save_news(self, items: Iterable) -> int:
        """Add headlines to the archive, never replacing one already there.

        `INSERT OR IGNORE`, not `OR REPLACE`, and that is the whole point: an
        item keeps the `first_seen` of the tick that actually saw it. Rewriting
        it on every sync would turn the archive into a hindsight dataset, which
        is exactly the thing it exists to avoid being.
        """
        now = int(time.time() * 1000)
        rows = [
            (
                i.uid,
                i.symbol,
                i.ts,
                i.source,
                i.title,
                i.link,
                i.score,
                i.kind,
                i.matched,
                now,
            )
            for i in items
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO news"
            " (uid,symbol,ts,source,title,link,score,kind,matched,first_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def load_news(
        self, symbol: str | None = None, since: int | None = None, limit: int = 200
    ) -> list:
        from .news import NewsItem

        sql = "SELECT * FROM news"
        where: list[str] = []
        args: list = []
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if since is not None:
            where.append("ts>=?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [
            NewsItem(
                symbol=r["symbol"],
                uid=r["uid"],
                ts=r["ts"],
                source=r["source"],
                title=r["title"],
                link=r["link"],
                score=r["score"],
                kind=r["kind"],
                matched=r["matched"],
            )
            for r in self.conn.execute(sql, args).fetchall()
        ]

    def news_span(self) -> tuple[int, int, int]:
        """How much archive there is: (items, earliest ts, latest ts).

        Printed by the status report, because the only thing that makes this
        archive worth anything is its length, and the only way it gets longer
        is time.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM news"
        ).fetchone()
        return int(row["n"] or 0), int(row["lo"] or 0), int(row["hi"] or 0)

    # --- trades & decisions -----------------------------------------------

    def save_trade(self, t: Trade) -> None:
        self.conn.execute(
            "INSERT INTO trades (symbol,qty,entry_price,exit_price,entry_time,"
            "exit_time,fees,pnl,reason,partial) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                t.symbol,
                t.qty,
                t.entry_price,
                t.exit_price,
                t.entry_time,
                t.exit_time,
                t.fees,
                t.pnl,
                t.reason,
                int(t.partial),
            ),
        )
        self.conn.commit()

    def load_trades(self) -> list[Trade]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY exit_time").fetchall()
        return [
            Trade(
                symbol=r["symbol"],
                qty=r["qty"],
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                entry_time=r["entry_time"],
                exit_time=r["exit_time"],
                fees=r["fees"],
                pnl=r["pnl"],
                reason=r["reason"],
                partial=bool(r["partial"]),
            )
            for r in rows
        ]

    def save_decision(self, d: Decision) -> None:
        self.conn.execute(
            "INSERT INTO decisions (ts,symbol,action,reason,price,qty,equity,meta)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                d.ts,
                d.symbol,
                d.action,
                d.reason,
                d.price,
                d.qty,
                d.equity,
                json.dumps(d.meta, default=str),
            ),
        )
        self.conn.commit()

    def recent_decisions(self, limit: int = 50) -> list[Decision]:
        rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            Decision(
                r["ts"],
                r["symbol"],
                r["action"],
                r["reason"],
                r["price"],
                r["qty"],
                r["equity"],
                json.loads(r["meta"]),
            )
            for r in rows
        ]

    # --- equity & key/value state -----------------------------------------

    def save_equity(self, ts: int, equity: float, cash: float, exposure: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_curve VALUES (?,?,?,?)",
            (ts, equity, cash, exposure),
        )
        self.conn.commit()

    def equity_curve(self) -> list[tuple[int, float]]:
        rows = self.conn.execute(
            "SELECT ts, equity FROM equity_curve ORDER BY ts"
        ).fetchall()
        return [(r["ts"], r["equity"]) for r in rows]

    def first_equity(self) -> tuple[int, float] | None:
        """The oldest point of the equity curve, without reading the rest.

        Used to back-date a run on an account that has been trading since
        before the ledger existed: the record has to start where the data
        starts, not where the upgrade happened.
        """
        row = self.conn.execute(
            "SELECT ts, equity FROM equity_curve ORDER BY ts LIMIT 1"
        ).fetchone()
        return (row["ts"], row["equity"]) if row else None

    def last_equity(self) -> tuple[int, float] | None:
        row = self.conn.execute(
            "SELECT ts, equity FROM equity_curve ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return (row["ts"], row["equity"]) if row else None

    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO state VALUES (?,?)",
            (key, json.dumps(value, default=str)),
        )
        self.conn.commit()


    # --- the track record -------------------------------------------------
    #
    # A forward test is only worth something if it cannot be quietly restarted
    # when it looks bad. This table is the ledger that makes that visible: one
    # row per funded run, closed rather than deleted when the account is reset,
    # and deliberately left out of `reset_trading_state`. An agent with four
    # abandoned runs behind it and one flattering one in progress is a very
    # different claim from an agent with one run, and the difference should not
    # depend on anyone remembering to mention it.

    def open_run(self, ts: int, initial: float, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_ts, initial, note) VALUES (?,?,?)",
            (ts, initial, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def current_run(self) -> dict | None:
        """The run in progress: the one that was funded and never closed."""
        row = self.conn.execute(
            "SELECT * FROM runs WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def close_run(self, ts: int, final: float, trades: int, note: str | None = None) -> None:
        run = self.current_run()
        if run is None:
            return
        if note is None:
            self.conn.execute(
                "UPDATE runs SET ended_ts=?, final=?, trades=? WHERE id=?",
                (ts, final, trades, run["id"]),
            )
        else:
            self.conn.execute(
                "UPDATE runs SET ended_ts=?, final=?, trades=?, note=? WHERE id=?",
                (ts, final, trades, note, run["id"]),
            )
        self.conn.commit()

    def drop_empty_run(self) -> bool:
        """Delete the run in progress, but only if nothing ever happened in it.

        Changing your mind about the budget before the agent has taken a single
        decision is not a restart, and recording it as one would fill the
        ledger with 0% rows that hide the ones that matter. This is the only
        place a run is ever deleted, and it guards itself: a run with a trade
        or a single point of equity curve behind it cannot be dropped here.
        """
        run = self.current_run()
        if run is None:
            return False
        if self.first_equity() is not None:
            return False
        if self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]:
            return False
        self.conn.execute("DELETE FROM runs WHERE id=?", (run["id"],))
        self.conn.commit()
        return True

    def runs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY started_ts").fetchall()
        return [dict(r) for r in rows]

    def reset_trading_state(self) -> None:
        """Wipe the account, positions and history but keep the cached bars.

        The run in progress is *closed*, not deleted, and `runs` is not on the
        wipe list. Wiping the account is a legitimate thing to do; doing it
        silently and then quoting the fresh start as a track record is not, and
        the difference is one row.
        """

        last = self.last_equity()
        if self.current_run() is not None:
            count = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            self.close_run(
                last[0] if last else 0,
                last[1] if last else 0.0,
                int(count),
                note="wiped by hand",
            )

        # The news archive and the earnings calendar are deliberately not in
        # this list: they record what was public when, they are not part of the
        # account, and the archive cannot be rebuilt once discarded.
        for table in (
            "positions",
            "pending_orders",
            "trades",
            "decisions",
            "equity_curve",
            "state",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
