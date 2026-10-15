import subprocess
import sys


def test_bot_entrypoint_registers_all_tools():
    """Regression: the bot never imported app.tools.impl, so the agent had no tools and
    confirming an event failed with 'Unknown tool'. Checked in a fresh interpreter."""
    code = (
        "import app.bot.main\n"
        "from app.tools.registry import REGISTRY\n"
        "print(sorted(REGISTRY))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    for name in ["create_calendar_event", "send_email", "find_contact", "get_current_datetime", "save_contact"]:
        assert name in out
