import datetime as dt
import json
from collections import Counter

import pytest

from app.evals.run import EVAL_CONTACTS, EVAL_NOW, load_dataset, render_report, run_eval, score, summarize
from app.util.persian import jalali_str
from tests.fakes import call, turn

CASES = load_dataset()
BY_TEXT = {c["transcript"]: c for c in CASES}


class OracleLLM:
    """Answers each case the way a perfect model would, so the harness itself can be tested offline."""

    model = "oracle"

    def __init__(self):
        self.script: list = []

    def chat(self, messages, tools):
        if not self.script:
            transcript = messages[1]["content"].split("<transcript>\n")[1].split("\n</transcript>")[0]
            self.script = self._script_for(BY_TEXT[transcript])
        return self.script.pop(0)

    @staticmethod
    def _script_for(case):
        e = case["expect"]
        if e["kind"] == "schedule":
            guests = [{"email": g} for g in e["guests"]]
            lookups = [call("find_contact", name=n) for n, _, em in EVAL_CONTACTS if em in e["guests"]]
            return [
                turn(call("get_current_datetime"), *lookups),
                turn(call("propose_schedule", date=e["date"], time=e["time"], duration_min=e["duration_min"], title="جلسه", guests=guests)),
            ]  # fmt: skip
        if e["kind"] == "email":
            return [
                turn(call("find_contact", name="x")),
                turn(call("propose_email", to=e["to"], subject="موضوع", body="متن")),
            ]  # fmt: skip
        return [turn(call("ask_clarification", question=f"{e['question_contains_any'][0]} را بگویید؟"))]


def test_dataset_has_30_valid_unique_cases():
    assert len(CASES) == 30
    assert len({c["id"] for c in CASES}) == 30 and len({c["transcript"] for c in CASES}) == 30
    kinds = Counter(c["expect"]["kind"] for c in CASES)
    assert kinds == {"schedule": 16, "email": 8, "clarify": 6}
    known = {e for _, _, e in EVAL_CONTACTS} | {"reza@example.com"}
    for c in CASES:
        e = c["expect"]
        if e["kind"] == "schedule":
            assert dt.date.fromisoformat(e["date"]) >= EVAL_NOW.date()
            dt.time.fromisoformat(e["time"])
            assert 5 <= e["duration_min"] <= 1440 and set(e["guests"]) <= known
        elif e["kind"] == "email":
            assert e["to"] in known
        else:
            assert e["question_contains_any"]


def test_expected_jalali_dates_are_consistent_with_the_converter():
    """The Jalali sentences in the dataset must really mean the expected Gregorian dates."""
    expected_jalali = {
        "s07_jalali_25_mehr": "1405/07/25",
        "s08_jalali_30_mehr": "1405/07/30",
        "s09_jalali_1_aban": "1405/08/01",
        "s10_jalali_quarter": "1405/08/15",
    }
    by_id = {c["id"]: c for c in CASES}
    for case_id, jalali in expected_jalali.items():
        assert jalali_str(dt.date.fromisoformat(by_id[case_id]["expect"]["date"])) == jalali


def test_expected_weekdays_match_the_wording():
    weekdays = {"s04_weekday_thursday": 3, "s05_saturday_half_hour": 5, "s06_next_monday": 0, "s12_next_week_tuesday": 1,
                "s14_friday_short": 4, "s15_long_meeting": 0}  # fmt: skip
    by_id = {c["id"]: c for c in CASES}
    for case_id, wd in weekdays.items():
        assert dt.date.fromisoformat(by_id[case_id]["expect"]["date"]).weekday() == wd


def test_a_perfect_model_scores_100_percent(engine):
    results = run_eval(OracleLLM(), CASES, engine=engine)
    s = summarize(results)
    assert s["all_checks_passed"] == 30
    for metric in ("intent", "date", "time", "duration", "guests", "recipient", "clarification"):
        assert s[metric]["accuracy"] == 1.0, metric
    assert (s["date"]["total"], s["recipient"]["total"], s["clarification"]["total"]) == (16, 8, 6)


def test_a_model_that_always_asks_questions_is_scored_low(engine):
    class AlwaysAsk:
        model = "ask"

        def chat(self, messages, tools):
            return turn(call("ask_clarification", question="چه ساعتی؟ ساعت"))

    s = summarize(run_eval(AlwaysAsk(), CASES, engine=engine))
    assert s["intent"]["correct"] == 6 and s["date"]["correct"] == 0 and s["time"]["correct"] == 0


def test_cases_do_not_leak_into_each_other(engine):
    """Each case runs in a rolled-back transaction: recent-message memory must start empty every time."""
    seen = []

    class Spy:
        model = "spy"

        def chat(self, messages, tools):
            seen.append(messages[1]["content"])
            return turn(call("ask_clarification", question="؟"))

    run_eval(Spy(), CASES[:3], engine=engine)
    assert all("Recent messages" not in m for m in seen)


def test_score_details():
    case = {"expect": {"kind": "schedule", "date": "2026-10-15", "time": "10:00", "duration_min": 60, "guests": ["a@x.com"]}}
    good = {"date": "2026-10-15", "time": "10:00:00", "duration_min": 60, "guests": [{"email": "A@x.com"}]}
    assert all(score(case, "schedule", good).values())
    assert not score(case, "schedule", {**good, "time": "10:30:00"})["time"]
    assert not score(case, "schedule", {**good, "guests": []})["guests"]
    assert not score(case, "clarify", {"question": "?"})["intent"]
    clar = {"expect": {"kind": "clarify", "question_contains_any": ["ساعت"]}}
    assert score(clar, "clarify", {"question": "چه ساعتی؟"})["question"]  # substring match
    assert not score(clar, "clarify", {"question": "چه روزی؟"})["question"]
    assert not score(clar, "clarify", {"question": ""})["question"]
    assert score(clar, "clarify", {"question": "ساعت چند؟"})["question"]
    assert not score(clar, "email", {"to": "a@x.com"})["intent"]


def test_report_lists_failures_and_headline_numbers(engine):
    class AlwaysAsk:
        model = "ask"

        def chat(self, messages, tools):
            return turn(call("ask_clarification", question="ساعت؟"))

    report = render_report(run_eval(AlwaysAsk(), CASES[:4], engine=engine), "ask", "v1")
    assert "**Intent**" in report and "**Date**" in report and "**Time**" in report and "## Failures" in report
    assert "s01_tomorrow_guest" in report


def test_dataset_file_is_valid_jsonl_utf8():
    from app.evals.run import DATASET

    for line in DATASET.read_text(encoding="utf-8").splitlines():
        json.loads(line)
    _ = pytest
