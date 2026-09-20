"""The forward record, and the things that stop it flattering itself.

The backtest cannot say whether this strategy works — its parameters were
chosen on the same thirty-six years it is scored over. Only a run forward can, and
these tests are about the devices that keep that run honest: a starting line
that cannot move, a ledger of restarts, and a verdict that refuses to be given
early.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import bars_from_closes, session_opens, trending_closes

from trader import agent, cli, track
from trader.config import Settings
from trader.store import Store

NOW = datetime.fromtimestamp(session_opens(700)[-1] / 1000, timezone.utc) + timedelta(
    hours=7
)


@pytest.fixture
def cfg(settings) -> Settings:
    return replace(settings, universe=("AAA", "BBB"), min_notional=0.0)


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "t.db")


@pytest.fixture
def store(db, cfg):
    s = Store(db)
    for seed, sym in enumerate(cfg.universe, 1):
        s.save_bars(sym, cfg.interval, bars_from_closes(trending_closes(seed=seed)))
    s.save_bars(
        "SPY", cfg.interval, bars_from_closes(trending_closes(seed=9, amp=0.02))
    )
    yield s
    s.close()


# --- the starting line ------------------------------------------------------


def test_the_first_tick_pre_registers_the_run(store, cfg):
    """Written down when the account is funded, not inferred afterwards from
    whichever point makes the curve look best."""
    assert store.current_run() is None
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)

    run = store.current_run()
    assert run is not None
    assert run["initial"] == cfg.initial_capital
    assert run["ended_ts"] is None


def test_an_account_that_predates_the_ledger_is_back_dated(store, cfg):
    """An agent that has been trading for months must not have its record start
    on the day it was upgraded — that would silently drop whatever it did in
    the meantime, which is the cherry pick this file exists to prevent."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    first_ts, first_equity = store.first_equity()

    store.conn.execute("DELETE FROM runs")  # as if the ledger had never existed
    store.conn.commit()

    agent.run_tick(store, cfg, now=NOW + timedelta(days=1), do_sync=False, log=lambda *_: None)
    run = store.current_run()
    assert run["started_ts"] == first_ts
    assert run["initial"] == first_equity
    assert "back-dated" in run["note"]


def test_a_second_tick_does_not_open_a_second_run(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    agent.run_tick(store, cfg, now=NOW + timedelta(days=1), do_sync=False, log=lambda *_: None)
    assert len(store.runs()) == 1


# --- the ledger -------------------------------------------------------------


def test_wiping_the_account_closes_the_run_rather_than_deleting_it(store, cfg):
    """Wiping the account is a legitimate thing to do. Doing it silently and
    then quoting the fresh start as a track record is not."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.reset_trading_state()

    runs = store.runs()
    assert len(runs) == 1 and runs[0]["ended_ts"] is not None
    assert store.current_run() is None
    assert runs[0]["note"] == "wiped by hand"


def test_the_ledger_survives_everything_the_wipe_touches(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.reset_trading_state()
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.reset_trading_state()
    assert len(store.runs()) == 2, "both abandoned runs are still on the record"


def test_the_report_prints_the_abandoned_runs(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.reset_trading_state()
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)

    text = track.report_text(store, cfg, now=NOW)
    assert "THE LEDGER" in text
    assert "IN PROGRESS" in text


# --- funding ----------------------------------------------------------------


def test_funding_sets_the_budget_and_starts_the_clock(db, store, capsys):
    store.close()
    assert cli.main(["--db", db, "fund", "2500"]) == 0

    with Store(db) as s:
        assert s.get_state("cash") == 2500.0
        assert s.current_run()["initial"] == 2500.0
    assert "2,500.00" in capsys.readouterr().out


def test_refunding_a_traded_account_is_refused(db, store, cfg, capsys):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.conn.execute(
        "INSERT INTO trades (symbol, qty, entry_price, exit_price, entry_time,"
        " exit_time, fees, pnl, reason) VALUES ('AAA',1,1,2,0,1,0,1,'test')"
    )
    store.conn.commit()
    store.close()

    assert cli.main(["--db", db, "fund", "5000"]) == 1
    assert "starting line" in capsys.readouterr().out
    with Store(db) as s:
        assert s.get_state("cash") != 5000.0, "the account was left alone"


def test_a_restart_is_allowed_but_never_quiet(db, store, cfg, capsys):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    store.conn.execute(
        "INSERT INTO trades (symbol, qty, entry_price, exit_price, entry_time,"
        " exit_time, fees, pnl, reason) VALUES ('AAA',1,1,2,0,1,0,1,'test')"
    )
    store.conn.commit()
    store.close()

    assert cli.main(["--db", db, "fund", "5000", "--restart"]) == 0
    with Store(db) as s:
        assert s.get_state("cash") == 5000.0
        runs = s.runs()
        assert len(runs) == 2
        assert runs[0]["ended_ts"] is not None and runs[0]["note"] == "restarted"
        assert runs[1]["initial"] == 5000.0 and runs[1]["ended_ts"] is None


def test_changing_the_budget_before_the_first_decision_leaves_no_trace(db, store):
    """Changing your mind about the budget before the agent has taken a single
    decision is not a restart, and recording it as one would fill the ledger
    with 0% rows that hide the ones that matter."""
    store.close()
    cli.main(["--db", db, "fund", "1500"])
    cli.main(["--db", db, "fund", "3000"])

    with Store(db) as s:
        assert len(s.runs()) == 1
        assert s.current_run()["initial"] == 3000.0


def test_a_run_that_traded_can_never_be_dropped(store, cfg):
    """The only place a run is deleted guards itself."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    assert store.drop_empty_run() is False
    assert len(store.runs()) == 1

def test_a_budget_has_to_be_positive(db, store):
    store.close()
    assert cli.main(["--db", db, "fund", "0"]) == 1


# --- the verdict ------------------------------------------------------------


def test_a_young_record_is_never_established(store, cfg):
    """Two months of daily returns carry an enormous standard error, and while
    a position is open its daily moves are the same trend sampled repeatedly.
    Both push the t up."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rec = track.record(store, cfg, now=NOW)

    assert not rec.established
    assert "ran long enough" in rec.missing


def test_every_gate_is_reported_with_how_far_it_is(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rec = track.record(store, cfg, now=NOW)

    names = [name for name, _, _ in rec.gates]
    assert "made money" in names
    assert any(n.startswith("beat") for n in names)
    assert all(detail for _, _, detail in rec.gates), "each one says how far"


def test_a_window_with_no_session_in_it_has_no_benchmark(store, cfg):
    """`Benchmark.available` is false when the index does not cover the window,
    and reporting a 0% benchmark there would hand the agent a spurious edge."""
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rec = track.record(store, cfg, now=NOW)
    if rec.benchmark is None:
        beat = [g for g in rec.gates if g[0].startswith("beat")][0]
        assert beat[1] is False and "no benchmark" in beat[2]
        assert rec.to_dict()["benchmark_return"] is None

def test_the_wait_is_quadratic_in_the_sharpe():
    """Halving the Sharpe quadruples the wait. This is the single most
    important number in the whole project and it is the least welcome."""
    assert track.years_for_significance(1.0) == pytest.approx(4.0)
    assert track.years_for_significance(0.5) == pytest.approx(16.0)
    assert track.years_for_significance(2.0) == pytest.approx(1.0)


def test_a_losing_run_is_never_given_a_date():
    """At a Sharpe that is not positive no amount of waiting clears the bar,
    and saying "check back in N years" would be a lie."""
    assert track.years_for_significance(0.0) is None
    assert track.years_for_significance(-0.8) is None


def test_the_estimated_date_is_never_sooner_than_the_minimum_run(store, cfg):
    """A Sharpe of 5 measured over two months would otherwise announce that the
    work is already done."""
    rec = track.record(store, cfg, now=NOW)
    if rec.years_needed is not None:
        assert rec.years_needed >= track.MIN_YEARS


# --- the numbers themselves -------------------------------------------------


def test_the_record_is_scored_by_the_backtester_own_yardstick(store, cfg):
    """A forward test scored by its own kinder yardstick is not a test."""
    from trader.backtest import Report

    rec = track.record(store, cfg, now=NOW)
    assert isinstance(rec.report, Report)


def test_the_curve_t_is_the_sharpe_scaled_by_elapsed_time(store, cfg):
    import math

    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rec = track.record(store, cfg, now=NOW)
    assert rec.t_stat == pytest.approx(rec.sharpe * math.sqrt(rec.years))


def test_the_sharpe_is_annualised_so_the_two_agents_are_comparable(store, cfg):
    """`backtest.py` reports it per session and must not scale it — the
    deflation there is defined on the raw statistic. This file has to compare
    against an agent on 4h candles, which it cannot do in per-session units."""
    import math

    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    rec = track.record(store, cfg, now=NOW)
    assert rec.sharpe == pytest.approx(
        rec.report.sharpe * math.sqrt(track.SESSIONS_PER_YEAR)
    )


def test_the_benchmark_is_scaled_to_the_budget_that_was_actually_funded(db, store, cfg):
    """`_passive` deploys the configured capital; the account may have been
    funded with something else, and the comparison has to be like for like."""
    store.close()
    cli.main(["--db", db, "fund", str(cfg.initial_capital * 4)])
    with Store(db) as s:
        rec = track.record(s, cfg, now=NOW)
        assert rec.report.initial == cfg.initial_capital * 4
        # A return, not an absolute, is what survives a change of budget.
        for b in rec.report.benchmarks:
            assert -1.0 < rec.report.benchmark_return(b) < 100.0


def test_the_json_is_enough_to_consolidate_two_agents(store, cfg):
    agent.run_tick(store, cfg, now=NOW, do_sync=False, log=lambda *_: None)
    blob = track.record(store, cfg, now=NOW).to_dict()

    for key in ("agent", "quote", "initial", "equity", "daily_equity", "past_runs"):
        assert key in blob
    assert blob["daily_equity"], "one point per UTC day, so curves can be added"
    day, equity = blob["daily_equity"][0]
    assert len(day) == 10 and isinstance(equity, float)
