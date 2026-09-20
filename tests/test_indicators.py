import pytest

from trader import indicators as ind


def test_sma_alignment_and_value():
    out = ind.sma([1, 2, 3, 4, 5], 5)
    assert out[:4] == [None] * 4
    assert out[4] == pytest.approx(3.0)


def test_sma_rolls_forward():
    out = ind.sma([1, 2, 3, 4, 5, 6], 3)
    assert out[2] == pytest.approx(2.0)
    assert out[5] == pytest.approx(5.0)


def test_series_length_always_matches_input():
    closes = [float(i) for i in range(50)]
    for series in (
        ind.sma(closes, 10),
        ind.ema(closes, 10),
        ind.rsi(closes, 14),
        ind.atr(closes, closes, closes, 14),
    ):
        assert len(series) == len(closes)


def test_ema_is_seeded_with_the_sma():
    out = ind.ema([1, 2, 3, 4, 5, 6], 3)
    assert out[2] == pytest.approx(2.0)  # mean of 1,2,3
    assert out[3] == pytest.approx(4 * 0.5 + 2 * 0.5)


def test_ema_of_a_constant_is_that_constant():
    out = ind.ema([7.0] * 20, 5)
    assert all(v == pytest.approx(7.0) for v in out[4:])


def test_too_short_series_are_all_none():
    assert ind.ema([1, 2], 5) == [None, None]
    assert ind.rsi([1, 2], 14) == [None, None]


def test_rsi_saturates_on_a_pure_uptrend():
    closes = [float(i) for i in range(1, 40)]
    out = ind.rsi(closes, 14)
    assert out[13] is None or out[14] == pytest.approx(100.0)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_floors_on_a_pure_downtrend():
    closes = [float(i) for i in range(40, 1, -1)]
    assert ind.rsi(closes, 14)[-1] == pytest.approx(0.0)


def test_rsi_stays_in_range():
    closes = [100 + (i % 7) * 3 - (i % 5) * 2 for i in range(200)]
    assert all(0.0 <= v <= 100.0 for v in ind.rsi(closes, 14) if v is not None)


def test_true_range_uses_the_previous_close():
    highs = [10, 12]
    lows = [9, 11]
    closes = [9.5, 11.5]
    # bar 1 gaps up: the gap from the prior close is the real range.
    assert ind.true_range(highs, lows, closes)[1] == pytest.approx(2.5)


def test_atr_of_constant_range_equals_that_range():
    highs = [11.0] * 30
    lows = [10.0] * 30
    closes = [10.5] * 30
    out = ind.atr(highs, lows, closes, 14)
    assert out[-1] == pytest.approx(1.0)


def test_crossed_above_needs_an_actual_crossing():
    fast = [1.0, 3.0, 4.0]
    slow = [2.0, 2.0, 2.0]
    assert ind.crossed_above(fast, slow, 1) is True
    assert ind.crossed_above(fast, slow, 2) is False  # already above


def test_crossings_ignore_warmup_nones():
    assert ind.crossed_above([None, 1.0], [None, 0.0], 1) is False
    assert ind.crossed_below([None, 0.0], [None, 1.0], 1) is False


def test_slope_is_per_bar():
    series = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert ind.slope(series, 4, 4) == pytest.approx(1.0)
    assert ind.slope(series, 1, 5) is None


def test_percentile_interpolates():
    assert ind.percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert ind.percentile([5], 0.9) == pytest.approx(5)
    with pytest.raises(ValueError):
        ind.percentile([], 0.5)


def test_rolling_high_excludes_the_current_bar():
    """Excluding it is what makes this a breakout level rather than a running
    maximum: a close can only clear a high that was already set."""
    values = [1, 5, 3, 2, 8, 4, 6, 7]
    out = ind.rolling_high(values, 3)
    assert out[:3] == [None] * 3
    assert out[3:] == [5, 5, 8, 8, 8]


def test_rolling_high_matches_the_naive_definition():
    values = [((i * 37) % 23) + (i % 5) for i in range(200)]
    for period in (1, 3, 14, 55):
        fast = ind.rolling_high(values, period)
        for i in range(len(values)):
            expected = max(values[i - period : i]) if i >= period else None
            assert fast[i] == expected


def test_rolling_high_rejects_a_zero_period():
    with pytest.raises(ValueError):
        ind.rolling_high([1, 2, 3], 0)
