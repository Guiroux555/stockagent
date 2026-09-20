"""The market gate: the question no single name can answer."""

from __future__ import annotations

from dataclasses import replace

from conftest import bars_from_closes, falling_closes, trending_closes

from trader.engine import SymbolView
from trader.regime import assess
from trader.strategy import analyze


def views(series, settings):
    return {
        sym: SymbolView(analyze(bars, settings), len(bars) - 1)
        for sym, bars in series.items()
    }


def healthy(settings, seed=1):
    return bars_from_closes(trending_closes(seed=seed, amp=0.02))


def sick(settings):
    return bars_from_closes(falling_closes(n=700))


def test_a_falling_anchor_shuts_the_whole_universe(settings):
    cfg = replace(settings, universe=("AAA",), market_anchor="SPY")
    v = views({"AAA": healthy(cfg), "SPY": sick(cfg)}, cfg)
    result = assess(v, cfg)
    assert not result.risk_on and "SPY" in result.reason


def test_a_rising_anchor_lets_it_through(settings):
    cfg = replace(settings, universe=("AAA",), market_anchor="SPY")
    v = views({"AAA": healthy(cfg), "SPY": healthy(cfg, seed=2)}, cfg)
    assert assess(v, cfg).risk_on


def test_a_missing_anchor_says_so_instead_of_silently_not_applying(settings):
    """A gate configured but never evaluated is worse than no gate: it looks
    like protection in the config and does nothing in the log."""
    cfg = replace(settings, universe=("AAA",), market_anchor="SPY")
    result = assess(views({"AAA": healthy(cfg)}, cfg), cfg)
    assert result.risk_on and "not applied" in result.reason


def test_breadth_counts_the_tradable_universe_not_the_anchor(settings):
    """The anchor is the market. Counting it as one more healthy name would
    double-count the thing being measured."""
    cfg = replace(settings, universe=("AAA",), market_anchor="SPY")
    v = views({"AAA": sick(cfg), "SPY": healthy(cfg)}, cfg)
    assert assess(v, cfg).breadth == 0.0


def test_a_breadth_floor_blocks_entries_when_set(settings):
    cfg = replace(
        settings, universe=("AAA", "BBB"), market_anchor="", min_breadth=0.6
    )
    v = views({"AAA": healthy(cfg), "BBB": sick(cfg)}, cfg)
    result = assess(v, cfg)
    assert not result.risk_on and "above its EMA200" in result.reason


def test_breadth_is_off_by_default(settings):
    assert settings.min_breadth == 0.0
