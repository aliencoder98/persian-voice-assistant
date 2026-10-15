import datetime as dt

import pytest

from app.util.persian import (
    gregorian_to_jalali,
    jalali_long_fa,
    jalali_to_gregorian,
    normalize,
    to_fa_digits,
)

KNOWN = [
    (dt.date(2025, 3, 21), (1404, 1, 1)),  # Nowruz 1404
    (dt.date(2026, 3, 21), (1405, 1, 1)),  # Nowruz 1405
    (dt.date(2026, 10, 14), (1405, 7, 22)),
    (dt.date(2024, 2, 29), (1402, 12, 10)),
    (dt.date(2026, 3, 20), (1404, 12, 29)),
]


@pytest.mark.parametrize("g,j", KNOWN)
def test_round_trip_known_dates(g, j):
    assert gregorian_to_jalali(g.year, g.month, g.day) == j
    assert jalali_to_gregorian(*j) == (g.year, g.month, g.day)


def test_long_format():
    assert jalali_long_fa(dt.date(2026, 10, 14)) == "چهارشنبه ۲۲ مهر ۱۴۰۵"


def test_normalize_folds_variants():
    assert normalize("عَلي") == normalize("علی")
    assert normalize("می‌خواهم") == normalize("میخواهم")
    assert normalize("  ALI ") == "ali"


def test_digits():
    assert to_fa_digits("15:30") == "۱۵:۳۰"
