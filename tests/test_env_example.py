import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Settings that are deliberately not in .env.example (internal defaults, documented elsewhere)
INTERNAL = {"BOT_HEARTBEAT_FILE", "MCP_OWNER_TELEGRAM_ID", "GOOGLE_CREDENTIALS_FILE"}


def documented() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if m:
            names.add(m.group(1))
    return names


def test_every_setting_is_listed_in_env_example():
    missing = {name.upper() for name in Settings.model_fields} - documented() - INTERNAL
    assert not missing, f".env.example lacks: {sorted(missing)}"


def test_env_example_holds_no_secrets():
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.endswith("_BUDGET"):
            continue  # token budgets are numbers, not credentials
        if any(w in key for w in ("TOKEN", "API_KEY", "SECRET", "PASSWORD_HASH")):
            assert value.strip() == "", f"{key} must be empty in .env.example"
    assert "change-me" in ENV_EXAMPLE.read_text(encoding="utf-8")  # the placeholder password, not a real one
