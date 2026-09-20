"""Short and medium-term trend collection, per name and per sector."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import bars_from_closes, falling_closes, trending_closes

from trader.backtest import run_backtest
from trader.engine import SymbolView
from trader.store import Store
from trader.strategy import analyze
from trader.trends import (
    HORIZONS,
    by_sector,
    collect,
    market,
    passes,
    sector_ranks,
    trend_of,
)


def view(bars, settings, i=-1):
    a = analyze(bars, settings)
    return SymbolView(a, len(bars) - 1 if i < 0 else i)


def test_every_horizon_is_measured(settings):
    bars = bars_from_closes(trending_closes(n=800))
    t = trend_of(analyze(bars, settings), "AAA", settings)
    assert t is not None
    assert set(t.returns) == {name for name, _ in HORIZONS}


def test_a_name_without_a_medium_term_reading_is_not_a_trend(settings):
    """Scoring it as zero would let a fragment be ranked against a real
    history, which is how a short listing quietly wins a ranking.

    Reached here with short moving averages, because with the shipped ones the
    warm-up is longer than the medium horizons and the guard can never fire —
    which is a good thing, and not a reason to leave it untested.
    """
    quick = replace(
        settings, regime_ema=20, trend_ema=10, slow_ema=5, fast_ema=3,
        rs_lookback=5, breakout_bars=5,
    )
    bars = bars_from_closes(trending_closes(n=200))
    a = analyze(bars, quick)
    assert a.ready(50)
    assert trend_of(a, "AAA", quick, 50) is None
    assert trend_of(a, "AAA", quick, 199) is not None


def test_the_risk_adjusted_score_prefers_the_calmer_climb(settings):
    """Two names up the same amount: the one that got there with less
    turbulence made you carry less risk for it."""
    closes = [100.0 * (1.001**i) for i in range(800)]
    calm = trend_of(
        analyze(bars_from_closes(closes, wick=0.001), settings), "A", settings
    )
    wild = trend_of(
        analyze(bars_from_closes(closes, wick=0.05), settings), "B", settings
    )
    assert calm.returns["3m"] > 0
    assert calm.returns["3m"] == pytest.approx(wild.returns["3m"])
    assert calm.risk_adjusted["3m"] > wild.risk_adjusted["3m"]


def test_a_rising_name_is_labelled_rising(settings):
    bars = bars_from_closes([100.0 * (1.001**i) for i in range(800)])
    t = trend_of(analyze(bars, settings), "AAA", settings)
    assert t.label == "rising" and t.aligned


def test_a_falling_name_is_labelled_falling(settings):
    t = trend_of(analyze(bars_from_closes(falling_closes(n=800)), settings), "A", settings)
    assert t.label == "falling" and not t.aligned


def test_a_pullback_is_not_an_uptrend(settings):
    """Up on the quarter and down on the month is a pullback, and the label
    says so rather than rounding it to one or the other."""
    closes = [100.0 * (1.002**i) for i in range(790)]
    closes += [closes[-1] * (0.99**k) for k in range(1, 11)]
    t = trend_of(analyze(bars_from_closes(closes), settings), "AAA", settings)
    assert t.medium > 0 > t.short
    assert t.label == "pullback"


def test_the_drawdown_from_the_yearly_high_is_reported(settings):
    closes = [100.0 * (1.002**i) for i in range(700)] + [100.0]
    t = trend_of(analyze(bars_from_closes(closes), settings), "AAA", settings)
    assert t.off_high > 0.5


# --- sectors ----------------------------------------------------------------


@pytest.fixture
def two_sectors(settings):
    cfg = replace(
        settings,
        universe=("AAA", "BBB", "CCC", "DDD"),
        sectors=(("Up", "AAA BBB"), ("Down", "CCC DDD")),
        market_anchor="",
    )
    rising = bars_from_closes([100.0 * (1.001**i) for i in range(800)])
    sinking = bars_from_closes(falling_closes(n=800))
    views = {
        "AAA": view(rising, cfg),
        "BBB": view(rising, cfg),
        "CCC": view(sinking, cfg),
        "DDD": view(sinking, cfg),
    }
    return cfg, views


def test_sectors_are_ranked_by_their_medium_term(two_sectors):
    cfg, views = two_sectors
    ranks = sector_ranks(collect(views, cfg), cfg)
    assert ranks["Up"] == 1 and ranks["Down"] == 2


def test_a_sector_is_equal_weighted_not_cap_weighted(two_sectors):
    """Cap weighting would quietly turn a sector into a reading of its two
    largest members."""
    cfg, views = two_sectors
    sectors = {s.name: s for s in by_sector(collect(views, cfg), cfg)}
    assert sectors["Up"].members == 2
    assert sectors["Up"].breadth == 1.0
    assert sectors["Down"].breadth == 0.0


def test_the_market_view_counts_rising_and_falling(two_sectors):
    cfg, views = two_sectors
    overall = market(collect(views, cfg), cfg)
    assert overall.rising == 2 and overall.falling == 2
    assert overall.breadth == 0.5
    assert overall.leaders[0].name == "Up"


# --- the gates, which ship off ---------------------------------------------


def test_both_trend_gates_are_off_by_default(settings):
    assert settings.min_medium_trend == 0.0
    assert settings.sector_top_k == 0


def test_an_off_gate_lets_everything_through(two_sectors):
    cfg, views = two_sectors
    trends = collect(views, cfg)
    ranks = sector_ranks(trends, cfg)
    for symbol in cfg.universe:
        assert passes(symbol, trends, ranks, cfg)[0]


def test_the_medium_gate_rejects_a_weak_name(two_sectors):
    cfg, views = two_sectors
    gated = replace(cfg, min_medium_trend=1.0)
    trends = collect(views, gated)
    ranks = sector_ranks(trends, gated)
    assert passes("AAA", trends, ranks, gated)[0]
    ok, why = passes("CCC", trends, ranks, gated)
    assert not ok and "medium-term trend" in why


def test_the_sector_gate_rejects_a_weak_sector(two_sectors):
    cfg, views = two_sectors
    gated = replace(cfg, sector_top_k=1)
    trends = collect(views, gated)
    ranks = sector_ranks(trends, gated)
    assert passes("AAA", trends, ranks, gated)[0]
    ok, why = passes("CCC", trends, ranks, gated)
    assert not ok and "outside the top" in why


def test_an_unmeasurable_name_is_let_through_not_blocked(settings):
    """Silently refusing to trade something because its history is short is a
    failure mode that hides itself."""
    gated = replace(settings, min_medium_trend=5.0, universe=("AAA",))
    assert passes("AAA", {}, {}, gated)[0]


def test_the_gates_change_nothing_while_they_are_off(settings):
    """The engine skips building the trend ladder entirely when no gate reads
    it, and the backtest must be identical either way."""
    names = ("AAA", "BBB")
    cfg = replace(settings, universe=names, min_notional=0.0)
    store = Store(":memory:")
    for k, sym in enumerate(names, 1):
        store.save_bars(sym, cfg.interval, bars_from_closes(trending_closes(seed=k)))
    store.save_bars(
        "SPY", cfg.interval, bars_from_closes(trending_closes(seed=9, amp=0.02))
    )
    off = run_backtest(store, cfg)
    wide = run_backtest(store, replace(cfg, sector_top_k=len(cfg.sectors)))
    store.close()
    assert off.total_return == pytest.approx(wide.total_return)


def test_every_name_in_the_universe_has_a_sector(settings):
    """The measured independence of this universe comes entirely from the
    sector split, so a name missing from it is a hole in the argument."""
    of = settings.sector_of
    assert [s for s in settings.universe if s not in of] == []
    assert [s for s in of if s not in settings.universe] == []
