"""Minimal Google Calendar client: refresh-token OAuth + events.insert / events.list."""

import datetime as dt
import time
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.tools.http_retry import retry_connect

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
TZ_NAME = "Asia/Tehran"


class GoogleError(RuntimeError):
    pass


class GoogleCalendar:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.settings = get_settings()
        self._client = client or httpx.Client(timeout=20)
        self._token: str | None = None
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        s = self.settings
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not (s.google_client_id and s.google_client_secret and s.google_refresh_token):
            raise GoogleError("Google credentials are not configured (see README).")
        resp = self._send(
            "POST",
            TOKEN_URL,
            data={
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "refresh_token": s.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise GoogleError(f"Google token refresh failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        return retry_connect(lambda: self._client.request(method, url, **kw))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    @property
    def _calendar(self) -> str:
        return self.settings.google_calendar_id or "primary"

    def create_event(
        self,
        *,
        date: dt.date,
        time_: dt.time,
        duration_min: int,
        title: str,
        description: str | None,
        guests: list[str],
        event_id: str,
    ) -> dict:
        """Create an event in Asia/Tehran time. `event_id` makes the call idempotent."""
        start = dt.datetime.combine(date, time_)
        end = start + dt.timedelta(minutes=duration_min)
        body = {
            "id": event_id,
            "summary": title,
            "description": description or "",
            "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": TZ_NAME},
            "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": TZ_NAME},
            "attendees": [{"email": g} for g in guests],
        }
        url = f"{API}/calendars/{self._calendar}/events"
        resp = self._send(
            "POST", url, params={"sendUpdates": "all"}, json=body, headers=self._headers()
        )
        if resp.status_code == 409:  # same id already created: a retry, not a new event
            resp = self._send("GET", f"{url}/{event_id}", headers=self._headers())
        if resp.status_code >= 400:
            raise GoogleError(f"Calendar API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return {"id": data["id"], "html_link": data.get("htmlLink")}

    def list_events(self, date: dt.date) -> list[dict]:
        return self.list_range(date, 1)

    def list_range(self, first_day: dt.date, days: int) -> list[dict]:
        """Events from the start of `first_day` for `days` days, in Asia/Tehran time."""
        tz = ZoneInfo(TZ_NAME)
        start = dt.datetime.combine(first_day, dt.time.min, tzinfo=tz)
        end = start + dt.timedelta(days=days)
        resp = self._send(
            "GET",
            f"{API}/calendars/{self._calendar}/events",
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeZone": TZ_NAME,
            },
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise GoogleError(f"Calendar API error {resp.status_code}: {resp.text[:300]}")
        events = []
        for item in resp.json().get("items", []):
            start_raw = item.get("start", {})
            events.append(
                {
                    "title": item.get("summary", ""),
                    "start": start_raw.get("dateTime") or start_raw.get("date"),
                    "end": (item.get("end") or {}).get("dateTime"),
                }
            )
        return events
