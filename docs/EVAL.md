# Agent evaluation

- Model: `deepseek-chat` · prompt `v3` · 30 Persian transcripts · fixed now: Wed 2026-10-14 10:30 Asia/Tehran (22 Mehr 1405)
- Date generated: 2026-10-15

| Metric | Accuracy |
|---|---|
| **Intent** (schedule / email / clarify) | **100.0% (30/30)** |
| **Date** (schedule cases) | **100.0% (16/16)** |
| **Time** (schedule cases) | **100.0% (16/16)** |
| Duration (schedule cases) | 100.0% (16/16) |
| Guests (schedule cases) | 100.0% (16/16) |
| Recipient (email cases) | 100.0% (8/8) |
| Clarifying question asked (clarify cases) | 100.0% (6/6) |
| Cases with every check passed | 30/30 |

Tokens: 177504 total, 5917 per case · median latency 2.9 s · 2.0 LLM steps per case.

All cases passed every check.

## History and honesty note

| Prompt | Intent | Date | Time | Failures |
|---|---|---|---|---|
| v1 (first version, untuned) | 96.7% (29/30) | 87.5% (14/16) | 93.8% (15/16) | "دوشنبه آینده" resolved to the Monday after next; "ده و ربع" triggered an AM/PM question |
| v2 | 96.7% | 87.5% | 93.8% | asked for a meeting title; "هفته بعد سه‌شنبه" resolved one week too late |
| v3 (current) | 100% (30/30) | 100% (16/16) | 100% (16/16) | none |

**v1 is the honest baseline.** v2 and v3 changed the prompt after looking at the failures of this very set (weekday wording, default hours, generic title), so the 100% is a development-set score and will be optimistic on new phrasing. Real voice input adds speech-recognition errors that this text-only set does not include. Re-run after every prompt change: `docker compose run --rm bot python -m app.evals.run --out docs/EVAL.md`.

Remaining known weak spots are not covered by the set: dates more than 15 days ahead without a Jalali date, ambiguous names shared by two contacts, and mixed Persian/English addresses spoken aloud.
