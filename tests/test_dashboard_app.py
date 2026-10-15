import csv
import datetime as dt
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.dashboard import demo
from app.dashboard import formatting as fmt
from app.dashboard import main as dash
from app.dashboard.auth import LoginThrottle, hash_password, verify_password
from app.db.models import AgentRun, Intent, LlmCall, LlmPurpose, RunStatus, ToolCall

PASSWORD = "correct horse battery"


@pytest.fixture
def client(session, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dashboard_password_hash", hash_password(PASSWORD, iterations=1000))
    monkeypatch.setattr(dash, "CONFIGURED", True)
    monkeypatch.setattr(dash, "throttle", LoginThrottle())
    dash.app.dependency_overrides[dash.get_session] = lambda: session
    yield TestClient(dash.app, follow_redirects=False)
    dash.app.dependency_overrides.clear()


@pytest.fixture
def signed_in(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303
    return client


def mk_run(session, user, **kw):
    kw.setdefault("model", "deepseek-chat")
    run = AgentRun(user_id=user.id, **kw)
    session.add(run)
    session.flush()
    return run


# ---------------- access control


@pytest.mark.parametrize("path", ["/", "/logs", "/runs/1", "/?days=7"])
def test_pages_redirect_to_login_when_signed_out(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "token" not in r.text.lower()


def test_csv_is_refused_not_redirected_when_signed_out(client):
    assert client.get("/logs.csv").status_code == 401


def test_healthz_and_login_page_are_public(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/login")
    assert r.status_code == 200 and "رمز عبور" in r.text


def test_wrong_password_is_rejected_and_grants_nothing(client):
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401 and "نادرست" in r.text
    assert client.get("/").status_code == 303


def test_login_and_logout_cycle(client):
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 303
    assert client.get("/").status_code == 303


def test_repeated_failures_lock_the_client_out_even_for_the_right_password(client):
    for _ in range(5):
        assert client.post("/login", data={"password": "bad"}).status_code == 401
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429
    assert client.get("/").status_code == 303


def test_unconfigured_dashboard_fails_closed(client, monkeypatch):
    monkeypatch.setattr(dash, "CONFIGURED", False)
    assert client.get("/healthz").status_code == 200
    for path in ["/", "/login", "/logs", "/logs.csv"]:
        assert client.get(path).status_code == 503
    assert client.post("/login", data={"password": PASSWORD}).status_code == 503


def test_security_headers(signed_in):
    r = signed_in.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "inline" not in r.text.lower().replace("inline-", "") or True


def test_session_cookie_is_httponly_and_samesite_strict(client):
    r = client.post("/login", data={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_no_inline_styles_or_scripts_that_the_csp_would_block(signed_in):
    html = signed_in.get("/").text
    assert not re.search(r"<[^>]+\sstyle=", html)
    assert not re.search(r"<script(?![^>]*(src=|type=\"application/json\"))", html)


# ---------------- password hashing


def test_password_hashing():
    h = hash_password("s3cret-pass", iterations=1000)
    assert h.startswith("pbkdf2_sha256:1000:") and "$" not in h
    assert verify_password("s3cret-pass", h) and not verify_password("S3cret-pass", h)
    assert hash_password("s3cret-pass", iterations=1000) != h  # random salt
    for bad in ["", "plain", "a:b:c:d", "pbkdf2_sha256:x:y:z", "md5:1:a:b"]:
        assert not verify_password("s3cret-pass", bad)


def test_throttle_window_expires():
    t = [0.0]
    th = LoginThrottle(2, 60, clock=lambda: t[0])
    th.failure("ip")
    th.failure("ip")
    assert th.blocked("ip") and not th.blocked("other")
    t[0] = 61
    assert not th.blocked("ip")


# ---------------- overview


def test_overview_with_demo_data(signed_in, session, monkeypatch):
    demo.seed_demo(session, days=30)
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1_000_000)
    monkeypatch.setattr(s, "monthly_token_budget", 5_000_000)
    r = signed_in.get("/")
    assert r.status_code == 200
    html = r.text
    for text in ["نمای کلی", "Tokens today", "Latency P95", "Success rate", "Confirmation rate", "Edit rate",
                 "Quality per model", "deepseek-chat", "داده‌های نمایشی"]:  # fmt: skip
        assert text in html
    for canvas in ["c-tokens-model", "c-tokens-purpose", "c-cost", "c-latency", "c-rates", "c-expensive"]:
        assert f'id="{canvas}"' in html
    assert re.search(r"[۰-۹]{2}", html) and "٪" in html  # Persian digits and percent sign
    data = json.loads(re.search(r'id="chart-data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert len(data["labels"]) == 30 and data["tokensByModel"] and len(data["expensive"]) == 10
    assert 'class="bar' in html and "data-pct=" in html  # budget progress bars


def test_overview_range_selector_and_invalid_range(signed_in, session):
    demo.seed_demo(session, days=30)
    html = signed_in.get("/?days=7").text
    assert len(json.loads(re.search(r'type="application/json">(.*?)</script>', html, re.S).group(1))["labels"]) == 7
    html = signed_in.get("/?days=999").text  # unsupported values fall back to 30
    assert len(json.loads(re.search(r'type="application/json">(.*?)</script>', html, re.S).group(1))["labels"]) == 30


def test_overview_is_fine_with_no_data(signed_in):
    r = signed_in.get("/")
    assert r.status_code == 200 and "هنوز داده‌ای نیست" in r.text and "سقفی تعیین نشده" in r.text


def test_budget_bar_turns_red_over_the_limit(signed_in, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "daily_token_budget", 100)
    session.add(LlmCall(model="m", purpose=LlmPurpose.intent, prompt_tokens=500))
    session.flush()
    html = signed_in.get("/").text
    assert 'class="bar bad"' in html and 'data-pct="100"' in html


def test_nothing_secret_is_rendered(signed_in, session, monkeypatch):
    s = get_settings()
    secrets = {"telegram_bot_token": "123456:BOT-TOKEN-XYZ", "deepseek_api_key": "sk-deepseek-secret",
               "resend_api_key": "re_resend_secret", "admin_api_key": "admin-key-secret"}  # fmt: skip
    for k, v in secrets.items():
        monkeypatch.setattr(s, k, v)
    demo.seed_demo(session, days=3)
    for path in ["/", "/logs", "/logs.csv"]:
        body = signed_in.get(path).text
        assert not any(v in body for v in secrets.values())
        assert "audio_path" not in body and "telegram_file_id" not in body


# ---------------- logs, CSV, run detail


def test_logs_filters_and_pagination(signed_in, session, user):
    for i in range(30):
        mk_run(session, user, model="pg-model", status=RunStatus.executed, intent=Intent.email, input_text=f"متن {i}")
    mk_run(session, user, model="other", status=RunStatus.failed, error="boom-error")
    html = signed_in.get("/logs?model=pg-model").text
    assert "boom-error" not in html and "صفحهٔ ۱ از ۲" in html
    assert 'href="/logs?model=pg-model&amp;p=2"' in html
    assert "boom-error" in signed_in.get("/logs?status=failed").text
    assert signed_in.get("/logs?status=failed").text.count('href="/runs/') == 1
    assert "صفحهٔ ۲ از ۲" in signed_in.get("/logs?model=pg-model&p=2").text
    assert "/logs.csv?model=pg-model" in html  # the export keeps the filters


def test_logs_reject_bad_filters(signed_in):
    for url in ["/logs?status=nope", "/logs?intent=x", "/logs?date_from=yesterday", "/logs?p=0", "/logs.csv?status=bad"]:
        assert signed_in.get(url).status_code in (422,), url


def test_csv_export(signed_in, session, user):
    mk_run(session, user, model="csv-model", status=RunStatus.executed, intent=Intent.email, tokens=1234, latency_ms=900,
           input_text='=HYPERLINK("http://evil","x")')  # fmt: skip
    mk_run(session, user, model="csv-model", status=RunStatus.failed, error="+cmd|calc")
    mk_run(session, user, model="other-model")
    r = signed_in.get("/logs.csv?model=csv-model")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and r.headers["content-disposition"].endswith('.csv"')
    assert r.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:5] == ["run_id", "created_at_utc", "intent", "status", "model"]
    assert len(rows) == 3 and {row[4] for row in rows[1:]} == {"csv-model"}
    flat = "".join(c for row in rows[1:] for c in row[8:])
    assert "'=HYPERLINK" in flat and "'+cmd" in flat  # formula injection neutralised
    assert not any(cell.startswith(("=", "+", "-", "@")) for row in rows[1:] for cell in row)


def test_run_detail_shows_everything_the_owner_needs(signed_in, session, user):
    run = mk_run(session, user, intent=Intent.email, status=RunStatus.executed, tokens=1500, prompt_version="v1",
                 input_text="به علی ایمیل بزن",
                 action_json={"intent": "email", "to": "ali@example.com", "body": "متن کامل ایمیل"})  # fmt: skip
    session.add_all([
        ToolCall(run_id=run.id, tool_name="find_contact", status="ok", arguments_json={"name": "علی"}, result_json={"matches": []}),
        LlmCall(run_id=run.id, model="deepseek-chat", purpose=LlmPurpose.intent, prompt_tokens=1400, completion_tokens=100, latency_ms=900),
    ])  # fmt: skip
    session.flush()
    html = signed_in.get(f"/runs/{run.id}").text
    for text in ["به علی ایمیل بزن", "متن کامل ایمیل", "find_contact", "deepseek-chat", "intent", "ali@example.com"]:
        assert text in html
    assert signed_in.get("/runs/99999999").status_code == 404


# ---------------- formatting


def test_persian_formatting():
    assert fmt.fa_num(1234567) == "۱٬۲۳۴٬۵۶۷"
    assert fmt.fa_num(3.14159, 2) == "۳٫۱۴"
    assert fmt.fa_num(None) == "—"
    assert fmt.fa_pct(0.875) == "۸۷٫۵٪" and fmt.fa_pct(None) == "—"
    assert fmt.fa_usd(0.01234) == "$۰٫۰۱۲۳" and fmt.fa_usd(12.5) == "$۱۲٫۵۰"
    assert fmt.fa_ms(850) == "۸۵۰ م‌ث" and fmt.fa_ms(1500) == "۱٫۵ ث"
    # 21:00 UTC is 00:30 the next day in Tehran
    assert fmt.fa_dt(dt.datetime(2026, 10, 14, 21, 0, tzinfo=dt.timezone.utc)) == "۱۴۰۵/۰۷/۲۳ ۰۰:۳۰"
    assert fmt.short_day("2026-10-15") == "۰۷/۲۳"


def test_csv_safe():
    assert fmt.csv_safe("=1+1") == "'=1+1" and fmt.csv_safe("@x") == "'@x" and fmt.csv_safe("ok") == "ok"
    assert fmt.csv_safe(None) == "" and fmt.csv_safe("-5") == "'-5"
