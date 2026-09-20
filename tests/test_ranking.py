from dataclasses import replace

import pytest
from conftest import bars_from_closes, trending_closes

from trader.engine import Engine, SymbolView
from trader.portfolio import Portfolio
from trader.ranking import momentum_score, passes, rank_universe, score_universe
from trader.strategy import analyze


def view(closes, settings):
    return SymbolView(analyze(bars_from_closes(closes), settings), len(closes) - 1)


def flat_then(gain: float, n: int = 300, lookback: int = 42):
    """A flat series that moves by `gain` over the last `lookback` bars."""
    return [100.0] * (n - lookback) + [
        100.0 * (1 + gain * (k + 1) / lookback) for k in range(lookback)
    ]


# --- momentum_score ---------------------------------------------------------


def test_the_score_is_the_return_over_the_lookback(settings):
    closes = [100.0] * 200 + [110.0]
    a = analyze(bars_from_closes(closes), settings)
    raw = momentum_score(a, len(closes) - 1, lookback=42, vol_normalise=False)
    assert raw == pytest.approx(0.10)


def test_a_rising_symbol_scores_above_a_falling_one(settings):
    up = analyze(bars_from_closes(flat_then(+0.20)), settings)
    down = analyze(bars_from_closes(flat_then(-0.20)), settings)
    i = len(up.closes) - 1
    assert momentum_score(up, i, 42, False) > 0 > momentum_score(down, i, 42, False)


def test_too_little_history_scores_nothing(settings):
    a = analyze(bars_from_closes([100.0] * 300), settings)
    assert momentum_score(a, 10, lookback=42, vol_normalise=False) is None


def test_negative_indices_count_from_the_end(settings):
    a = analyze(bars_from_closes(flat_then(+0.20)), settings)
    assert momentum_score(a, -1, 42, False) == pytest.approx(
        momentum_score(a, len(a.closes) - 1, 42, False)
    )


def test_volatility_normalisation_favours_the_calmer_climb(settings):
    """Two symbols up the same amount: the one that got there with less
    turbulence made you carry less risk for it, and must rank higher."""
    calm = analyze(bars_from_closes(flat_then(+0.20), wick=0.002), settings)
    wild = analyze(bars_from_closes(flat_then(+0.20), wick=0.05), settings)
    i = len(calm.closes) - 1

    assert momentum_score(calm, i, 42, False) == pytest.approx(
        momentum_score(wild, i, 42, False)
    )
    assert momentum_score(calm, i, 42, True) > momentum_score(wild, i, 42, True)


def test_a_zero_volatility_symbol_cannot_be_normalised(settings):
    a = analyze(bars_from_closes([100.0] * 300, wick=0.0), settings)
    assert momentum_score(a, -1, 42, True) is None


# --- ranking ----------------------------------------------------------------


def test_rank_one_is_the_strongest(settings):
    views = {
        "STRONG": view(flat_then(+0.30), settings),
        "MIDDLE": view(flat_then(+0.10), settings),
        "WEAK": view(flat_then(-0.10), settings),
    }
    ranks = rank_universe(views, settings)
    assert ranks == {"STRONG": 1, "MIDDLE": 2, "WEAK": 3}


def test_every_scored_symbol_gets_a_distinct_rank(settings):
    views = {
        n: view(trending_closes(seed=s), settings)
        for s, n in enumerate(("A", "B", "C", "D"), 1)
    }
    ranks = rank_universe(views, settings)
    assert sorted(ranks.values()) == [1, 2, 3, 4]


def test_ties_are_broken_by_name_so_runs_are_reproducible(settings):
    same = flat_then(+0.15)
    views = {"ZZZ": view(same, settings), "AAA": view(same, settings)}
    assert rank_universe(views, settings)["AAA"] == 1
    assert rank_universe(views, settings) == rank_universe(views, settings)


def test_unscorable_symbols_are_left_out_of_the_ranking(settings):
    views = {
        "GOOD": view(flat_then(+0.20), settings),
        "SHORT": SymbolView(analyze(bars_from_closes([100.0] * 300), settings), 5),
    }
    scores = score_universe(views, settings)
    assert "GOOD" in scores and "SHORT" not in scores


# --- the gate ---------------------------------------------------------------


def test_a_zero_k_disables_the_filter_entirely(settings):
    assert settings.rs_top_k == 0
    ok, note = passes("ANYTHING", {"ANYTHING": 99}, settings)
    assert ok and note == ""


def test_only_the_top_k_pass(settings):
    cfg = replace(settings, rs_top_k=2)
    ranks = {"A": 1, "B": 2, "C": 3}
    assert passes("A", ranks, cfg)[0] is True
    assert passes("B", ranks, cfg)[0] is True
    assert passes("C", ranks, cfg)[0] is False


def test_the_rejection_says_why(settings):
    cfg = replace(settings, rs_top_k=2)
    _, note = passes("C", {"A": 1, "B": 2, "C": 3}, cfg)
    assert "rank 3/3" in note and "top 2" in note


def test_an_unranked_symbol_is_let_through_not_silently_dropped(settings):
    """Refusing to trade a symbol because its history is too short to score is
    a failure mode that hides itself; the single-symbol gates already vetted
    it."""
    cfg = replace(settings, rs_top_k=1)
    ok, note = passes("NEW", {"A": 1}, cfg)
    assert ok and note == "unranked"


# --- engine integration -----------------------------------------------------


def _run(cfg, series):
    pf = Portfolio(cfg)
    engine = Engine(cfg, pf)
    analyses = {s: analyze(b, cfg) for s, b in series.items()}
    decisions = []
    for i in range(cfg.warmup_bars, len(next(iter(series.values())))):
        views = {s: SymbolView(a, i) for s, a in analyses.items()}
        decisions += engine.step(views, i, 1000.0, 1000.0).decisions
    return decisions


@pytest.fixture
def three_series(settings):
    return {
        n: bars_from_closes(trending_closes(seed=s))
        for s, n in enumerate(("AAA", "BBB", "CCC"), 1)
    }


def test_the_filter_never_queues_outside_the_top_k(settings, three_series):
    """The rank is recorded where the decision is taken — at the close — not
    where it is filled. By the time the order reaches the opening auction the
    ranking it was chosen on belongs to yesterday."""
    cfg = replace(settings, universe=tuple(three_series), rs_top_k=1)
    queued = [d for d in _run(cfg, three_series) if "queued to buy" in d.reason]
    assert queued, "nothing was queued, so the assertion below proves nothing"
    assert all(d.meta["rs_rank"] == 1 for d in queued)


def test_rejections_are_logged_with_their_rank(settings, three_series):
    cfg = replace(settings, universe=tuple(three_series), rs_top_k=1)
    rejected = [
        d for d in _run(cfg, three_series) if "rejected on relative strength" in d.reason
    ]
    assert rejected
    assert all(d.meta["rs_rank"] > 1 for d in rejected)


def test_the_filter_can_only_reduce_the_number_of_trades(settings, three_series):
    """It rejects candidates that already passed every other gate, so it can
    never create an entry the unfiltered agent would not have taken."""
    off = replace(settings, universe=tuple(three_series), rs_top_k=0)
    on = replace(off, rs_top_k=1)
    n_off = len([d for d in _run(off, three_series) if "queued to buy" in d.reason])
    n_on = len([d for d in _run(on, three_series) if "queued to buy" in d.reason])
    assert n_on <= n_off


def test_a_k_covering_the_whole_universe_excludes_nobody(settings, three_series):
    wide = replace(settings, universe=tuple(three_series), rs_top_k=len(three_series))
    assert not [
        d for d in _run(wide, three_series) if "rejected on relative strength" in d.reason
    ]


def test_queued_buys_record_their_rank_even_when_the_filter_is_off(
    settings, three_series
):
    cfg = replace(settings, universe=tuple(three_series), rs_top_k=0)
    queued = [d for d in _run(cfg, three_series) if "queued to buy" in d.reason]
    assert queued and all(d.meta["rs_rank"] is not None for d in queued)
