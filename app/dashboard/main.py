"""Read-only usage and quality dashboard. Sign-in required for everything except /healthz."""

import csv
import datetime as dt
import io
import json
import logging
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app import budget
from app.config import get_settings
from app.dashboard import formatting as fmt
from app.dashboard import queries as q
from app.dashboard.auth import LoginThrottle, verify_password
from app.dashboard.demo import has_demo
from app.db.models import RunStatus
from app.logging_config import setup_logging

log = logging.getLogger("dashboard")
settings = get_settings()
setup_logging(settings.log_level)

BASE = Path(__file__).parent
CONFIGURED = bool(settings.dashboard_password_hash and settings.dashboard_secret_key and settings.dashboard_db_password)
PAGE_SIZE = 25
RANGES = (7, 14, 30, 60, 90)

app = FastAPI(title="Usage dashboard", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))
for name in ("fa_num", "fa_pct", "fa_usd", "fa_ms", "fa_date", "fa_dt", "short_day"):
    templates.env.filters[name] = getattr(fmt, name)
throttle = LoginThrottle()

_SessionLocal: sessionmaker | None = None


def get_session() -> Iterator[Session]:
    """A session on the read-only role (separate credentials from the bot)."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            create_engine(get_settings().dashboard_database_url, pool_pre_ping=True), expire_on_commit=False
        )
    with _SessionLocal() as session:
        yield session


class NotAuthenticated(Exception):
    pass


def require_login(request: Request) -> None:
    if not request.session.get("auth"):
        raise NotAuthenticated


Auth = Depends(require_login)


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated):
    if request.url.path.endswith(".csv"):
        return PlainTextResponse("Sign in required.", status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return PlainTextResponse("Internal server error.", status_code=500)


# Middleware order: the last one added runs first. Sessions must wrap everything.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    if not CONFIGURED and request.url.path != "/healthz":
        return PlainTextResponse(
            "The dashboard is not configured: set DASHBOARD_PASSWORD_HASH, DASHBOARD_SECRET_KEY and "
            "DASHBOARD_DB_PASSWORD (see README).",
            status_code=503,
        )
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"  # pages hold private data
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.dashboard_secret_key or "unconfigured",
    session_cookie="dash_session",
    max_age=8 * 3600,
    same_site="strict",
    https_only=settings.dashboard_cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- public


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, password: str = Form(..., max_length=200)):
    key = client_key(request)
    if throttle.blocked(key):
        log.warning("Login throttled", extra={"client": key})
        return page(request, "login.html", 429, error="تلاش‌های ناموفق زیاد بود. چند دقیقه بعد دوباره امتحان کنید.")
    if verify_password(password, get_settings().dashboard_password_hash):
        throttle.success(key)
        request.session.clear()
        request.session["auth"] = True
        log.info("Dashboard login", extra={"client": key})
        return RedirectResponse("/", status_code=303)
    throttle.failure(key)
    log.warning("Dashboard login failed", extra={"client": key})
    return page(request, "login.html", 401, error="رمز عبور نادرست است.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- pages


def _chart_data(ov: q.Overview) -> dict:
    s = ov.series
    return {
        "labels": [fmt.short_day(d) for d in s["labels"]],
        "isoLabels": s["labels"],
        "tokensByModel": s["tokens_by_model"],
        "tokensByPurpose": s["tokens_by_purpose"],
        "costByModel": s["cost_by_model"],
        "latencyP50": s["latency_p50"],
        "latencyP95": s["latency_p95"],
        "successRate": s["success_rate"],
        "editRate": s["edit_rate"],
        "confirmationRate": s["confirmation_rate"],
        "expensive": [{"id": r.id, "cost": round(r.cost, 6), "model": r.model} for r in ov.expensive],
    }


@app.get("/", response_class=HTMLResponse, dependencies=[Auth])
def overview(request: Request, session: Session = Depends(get_session), days: int = Query(30)):
    days = days if days in RANGES else 30
    ov = q.overview(session, days)
    return page(
        request,
        "overview.html",
        ov=ov,
        ranges=RANGES,
        budgets=budget.status(session),
        hard_stop=settings.budget_hard_stop,
        chart_data=_chart_data(ov),
        demo=has_demo(session),
    )


def _filters(
    date_from: str | None, date_to: str | None, model: str | None, status: str | None, intent: str | None
) -> q.LogFilters:
    def parse(value: str | None) -> dt.date | None:
        try:
            return dt.date.fromisoformat(value) if value else None
        except ValueError:
            raise HTTPException(422, "Dates must be YYYY-MM-DD.")

    if status and status not in {s.value for s in RunStatus}:
        raise HTTPException(422, "Unknown status.")
    if intent and intent not in {"schedule", "email", "unknown"}:
        raise HTTPException(422, "Unknown intent.")
    return q.LogFilters(parse(date_from), parse(date_to), model or None, status or None, intent or None)


@app.get("/logs", response_class=HTMLResponse, dependencies=[Auth])
def logs(
    request: Request,
    session: Session = Depends(get_session),
    date_from: str | None = None,
    date_to: str | None = None,
    model: str | None = None,
    status: str | None = None,
    intent: str | None = None,
    p: int = Query(1, ge=1, le=100_000),
):
    f = _filters(date_from, date_to, model, status, intent)
    rows, total = q.list_logs(session, f, PAGE_SIZE, (p - 1) * PAGE_SIZE)
    query = {k: v for k, v in dict(date_from=date_from, date_to=date_to, model=model, status=status, intent=intent).items() if v}
    return page(
        request,
        "logs.html",
        rows=rows,
        total=total,
        p=p,
        pages=max(1, -(-total // PAGE_SIZE)),
        query=query,
        qs=urlencode(query),
        values=dict(date_from=date_from or "", date_to=date_to or "", model=model or "", status=status or "", intent=intent or ""),
        options=q.filter_options(session),
        statuses=[s.value for s in RunStatus],
    )


@app.get("/logs.csv", dependencies=[Auth])
def logs_csv(
    session: Session = Depends(get_session),
    date_from: str | None = None,
    date_to: str | None = None,
    model: str | None = None,
    status: str | None = None,
    intent: str | None = None,
):
    rows = q.export_logs(session, _filters(date_from, date_to, model, status, intent))
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads the Persian text as UTF-8
    writer = csv.writer(buf)
    writer.writerow(["run_id", "created_at_utc", "intent", "status", "model", "tokens", "latency_ms", "cost_usd", "input_preview", "error"])
    for r in rows:
        writer.writerow(
            [r["id"], r["created_at"].astimezone(dt.timezone.utc).isoformat(timespec="seconds"), r["intent"], r["status"],
             fmt.csv_safe(r["model"]), r["tokens"] or 0, r["latency_ms"] or 0, f"{r['cost']:.6f}",
             fmt.csv_safe(r["preview"]), fmt.csv_safe((r["error"] or "")[:200])]
        )  # fmt: skip
    name = f"logs-{dt.date.today():%Y%m%d}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Auth])
def run_page(request: Request, run_id: int, session: Session = Depends(get_session)):
    detail = q.run_detail(session, run_id)
    if detail is None:
        raise HTTPException(404, "Run not found.")
    return page(request, "run.html", d=detail, pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str))


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

