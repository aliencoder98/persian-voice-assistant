"""Persian text helpers and the Jalali (Solar Hijri) calendar."""

import datetime as dt
import re

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # Mon..Sun
JALALI_MONTHS_FA = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]  # fmt: skip

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "ى": "ی"})
_STRIP = re.compile("[‌‍ـً-ٰٟ]")  # ZWNJ, ZWJ, tatweel, diacritics


def to_fa_digits(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def normalize(text: str) -> str:
    """Fold spelling variants so 'علی', 'عَلي' and 'Ali' compare predictably."""
    text = text.translate(_ARABIC_TO_PERSIAN)
    text = _STRIP.sub("", text)
    return " ".join(text.lower().split())


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
        + gd + g_d_m[gm - 1]
    )  # fmt: skip
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_lengths = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > month_lengths[gm]:
        gd -= month_lengths[gm]
        gm += 1
    return gy, gm, gd


def jalali_date(d: dt.date) -> tuple[int, int, int]:
    return gregorian_to_jalali(d.year, d.month, d.day)


def jalali_str(d: dt.date) -> str:
    jy, jm, jd = jalali_date(d)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def jalali_long_fa(d: dt.date) -> str:
    """e.g. 'سه‌شنبه ۲۸ مهر ۱۴۰۵'"""
    jy, jm, jd = jalali_date(d)
    return f"{WEEKDAYS_FA[d.weekday()]} {to_fa_digits(jd)} {JALALI_MONTHS_FA[jm - 1]} {to_fa_digits(jy)}"
