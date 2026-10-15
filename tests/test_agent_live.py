"""Live eval against the real DeepSeek API: LIVE_LLM=1 pytest tests/test_agent_live.py -v

Replays the ten sample transcripts (text only) and checks the outcome kind and key fields.
Skipped by default; it costs a few cents and needs DEEPSEEK_API_KEY.
"""

import os

import pytest

from app.agent.agent import process_message
from app.agent.context import AgentContext
from app.agent.llm import DeepSeekProvider
from tests.conftest import FIXED_NOW
from tests.samples import samples

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="set LIVE_LLM=1 to run")

# The "hallucinated" sample only makes sense with a scripted bad model; live it equals unknown_contact.
LIVE = [s for s in samples() if s.id != "hallucinated_address_is_rejected"]


@pytest.mark.parametrize("sample", LIVE, ids=lambda s: s.id)
def test_live(session, user, sample):
    agent_ctx = AgentContext(owner_name="Owner")
    run, outcome = process_message(
        session, DeepSeekProvider(), user, sample.transcript, agent_ctx, now=lambda: FIXED_NOW
    )
    print(f"\n{sample.id}: {outcome.kind} steps={outcome.steps} tokens={outcome.tokens}")
    print(run.action_json)
    assert outcome.kind == sample.kind, (outcome, run.action_json)
    if sample.kind in ("schedule", "email"):
        for key, expected in sample.expect.items():
            if key == "guests":
                assert [g["email"] for g in run.action_json["guests"]] == expected
            else:
                assert run.action_json[key] == expected
