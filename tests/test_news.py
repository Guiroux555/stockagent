"""Headlines: scoring, archiving, and the line they are not allowed to cross.

The most important test in this file asserts an absence — that no news reaches
a trading decision. An absence is easy to lose in a refactor, so it is pinned
here rather than left to the docstrings.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace

import pytest

from trader import engine as engine_module
from trader.config import Settings
from trader.news import NewsItem, _to_items, assess, score_headline
from trader.store import Store

HOUR = 3_600_000


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def item(symbol="AAPL", uid="u1", ts=1_000_000, title="Apple beats estimates"):
    score, kind, matched = score_headline(title)
    return NewsItem(symbol, uid, ts, "Reuters", title, "http://x", score, kind, matched)


# --- the line ---------------------------------------------------------------


def test_no_trading_decision_can_read_a_headline():
    """`Engine.step` takes no news argument, and the engine does not import the
    news module at all. The guarantee is structural, not a default someone can
    flip — which is why it is asserted on the import graph rather than trusted
    to a docstring."""
    assert "news" not in inspect.signature(engine_module.Engine.step).parameters

    tree = ast.parse(inspect.getsource(engine_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [a.name for a in node.names] + [node.module or ""]
    assert "news" not in imported, "the engine reached for the news module"


def test_collection_is_off_by_default():
    assert Settings().news_enabled is False


# --- scoring ----------------------------------------------------------------


def test_a_good_headline_scores_positive():
    score, kind, matched = score_headline("Apple beats estimates and raises guidance")
    assert score > 0 and kind == "earnings" and "+beats" in matched


def test_a_bad_headline_scores_negative():
    score, kind, _ = score_headline("Regulators open antitrust probe, shares tumble")
    assert score < 0 and kind == "legal"


def test_a_headline_matching_nothing_scores_exactly_zero():
    """Silence is not neutrality. A score of zero means "no information", and
    the aggregation below drops it rather than averaging it in as balance."""
    score, kind, matched = score_headline("Apple unveils new product line")
    assert score == 0.0 and kind == "general" and matched == ""


def test_every_score_can_be_traced_to_the_words_that_made_it():
    """The only claim this scoring makes: not that it is good, that it is
    auditable."""
    _, _, matched = score_headline("Downgrade follows weak demand and a recall")
    assert "-downgrade" in matched and "-recall" in matched


def test_single_words_match_on_a_boundary():
    """Otherwise "win" fires on "winding down", which is the opposite."""
    assert score_headline("Winding down the unit")[0] == 0.0
    assert score_headline("Company wins the contract")[0] > 0


def test_a_mixed_headline_lands_between():
    score, _, _ = score_headline("Beats on revenue but warns on guidance")
    assert -1.0 < score < 1.0


# --- the archive ------------------------------------------------------------


def test_an_archived_headline_is_never_rewritten(store):
    """It keeps the first_seen of the tick that actually saw it. Rewriting that
    on every sync would turn the archive into a hindsight dataset, which is
    exactly the thing it exists to avoid being."""
    assert store.save_news([item()]) == 1
    assert store.save_news([item()]) == 0
    assert len(store.load_news("AAPL")) == 1


def test_two_names_can_carry_the_same_story(store):
    """The feed files one article under several tickers, and the archive keeps
    both rows: which ticker it arrived under is part of the record."""
    store.save_news([item(symbol="AAPL"), item(symbol="MSFT")])
    assert len(store.load_news()) == 2


def test_the_archive_reports_its_own_span(store):
    """The only thing that makes this archive worth anything is its length."""
    store.save_news([item(uid="a", ts=1000), item(uid="b", ts=5000)])
    count, lo, hi = store.news_span()
    assert (count, lo, hi) == (2, 1000, 5000)


def test_resetting_the_account_does_not_discard_the_archive(store):
    """It is a record of what was public when, not part of the account, and it
    cannot be rebuilt once thrown away."""
    store.save_news([item()])
    store.reset_trading_state()
    assert store.load_news()


# --- reading it back --------------------------------------------------------


def test_stale_headlines_fall_out_of_the_window(settings):
    now = 100 * HOUR
    old = item(uid="old", ts=now - 200 * HOUR)
    assert assess([old], now, settings).items == 0


def test_a_recent_headline_outweighs_an_older_one(settings):
    now = 100 * HOUR
    fresh = item(uid="f", ts=now - HOUR, title="Apple beats estimates")
    stale = item(uid="s", ts=now - 70 * HOUR, title="Apple hit by a recall")
    view = assess([fresh, stale], now, settings)
    assert view.items == 2 and view.score > 0


def test_unscored_headlines_do_not_dilute_the_score(settings):
    now = 100 * HOUR
    rows = [item(uid=f"n{k}", ts=now - HOUR, title="Apple unveils a product")
            for k in range(5)]
    rows.append(item(uid="bad", ts=now - HOUR, title="Apple hit by fraud probe"))
    view = assess(rows, now, settings)
    assert view.items == 1 and view.score < 0


def test_a_name_with_nothing_scored_is_not_measured(settings):
    now = 100 * HOUR
    view = assess([item(uid="n", ts=now - HOUR, title="Apple unveils")], now, settings)
    assert not view.measured


def test_a_zero_length_window_measures_nothing(settings):
    cfg = replace(settings, news_max_age_hours=0)
    assert not assess([item(ts=1)], 1, cfg).measured


# --- parsing ----------------------------------------------------------------


def test_rows_without_a_timestamp_or_title_are_dropped():
    rows = [
        {"uuid": "a", "title": "Apple beats", "providerPublishTime": 1000,
         "publisher": "Reuters", "link": "http://x"},
        {"uuid": "b", "title": "", "providerPublishTime": 1000},
        {"uuid": "c", "title": "No time here"},
    ]
    assert [i.uid for i in _to_items("AAPL", rows)] == ["a"]


def test_the_publication_time_is_carried_in_milliseconds():
    rows = [{"uuid": "a", "title": "Apple beats", "providerPublishTime": 1_700_000_000}]
    assert _to_items("AAPL", rows)[0].ts == 1_700_000_000_000
