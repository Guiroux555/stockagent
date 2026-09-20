"""SQLite persistence: one file holds the cached sessions, the account, the
open positions, the orders queued for the next open, every closed trade and the
full decision log.

One file means the whole state of the agent can be backed up by copying it, and
inspected with any SQLite browser — which matters when you need to answer "why
did it buy that?" three weeks later.
"""

from __future__ import annotations

import json
import sqlite3
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
    created_ts   INTEGER NOT NULL
);

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
"""


class Store:
    def __init__(self, path: str | Path = "data/trader.db"):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
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
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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

    def replace_bars(self, symbol: str, interval: str, bars: Iterable[Bar]) -> int:
        """Overwrite a symbol's whole series.

        Needed because equity prices are *retroactively* restated: a split or
        a dividend rewrites every bar before it. Merging a freshly adjusted
        series into stale rows would leave a discontinuity at the join, and the
        agent would read it as a gap that never happened.
        """
        self.conn.execute(
            "DELETE FROM bars WHERE symbol=? AND interval=?", (symbol, interval)
        )
        return self.save_bars(symbol, interval, bars)

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
        sql += " ORDER BY open_time"
        rows = self.conn.execute(sql, args).fetchall()
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
        return bars[-limit:] if limit else bars

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
            " (symbol,side,reason,signal_price,stop,atr,created_ts)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (o.symbol, o.side, o.reason, o.signal_price, o.stop, o.atr, o.created_ts)
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
                created_ts=r["created_ts"],
            )
            for r in rows
        ]

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

    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO state VALUES (?,?)",
            (key, json.dumps(value, default=str)),
        )
        self.conn.commit()

    def reset_trading_state(self) -> None:
        """Wipe the account, positions and history but keep the cached bars."""
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
