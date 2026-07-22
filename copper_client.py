"""
copper_client.py — minimal client for the Copper Labs consumer app API
(api.copperlabs.com), reverse-engineered from the iOS app's traffic.

Auth: Auth0 (auth.copperlabs.com), RS256 bearer JWT, ~24h lifetime.
The app requests the `offline_access` scope, so a refresh token is available
and this client can renew the access token on its own — no daily re-capture.

Quick start:
    export COPPER_TOKEN="<bearer access token>"
    export COPPER_REFRESH_TOKEN="<refresh token>"   # optional, enables auto-renew
    python copper_client.py
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

API_BASE = "https://api.copperlabs.com/api/v2/app"
AUTH_TOKEN_URL = "https://auth.copperlabs.com/oauth/token"
# Public OAuth client id — the `azp` claim in the app's JWT (not a secret):
CLIENT_ID = "l8aJ85JrIRq44CWDEcAlHLqR5wUl8Hjh"


def _jwt_exp(token: str) -> int:
    """Read the `exp` (unix seconds) from a JWT without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # fix base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


def _iso_z(dt: datetime | str) -> str:
    """Format a datetime as 2026-07-22T06:00:00.000Z (strings pass through)."""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class CopperClient:
    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str = CLIENT_ID,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth
    def _token_valid(self, skew: int = 120) -> bool:
        """True if we hold a token that won't expire within `skew` seconds."""
        return bool(self.access_token) and _jwt_exp(self.access_token) - skew > time.time()

    def refresh(self) -> None:
        """Exchange the refresh token for a fresh access token (Auth0)."""
        if not self.refresh_token:
            raise RuntimeError(
                "No refresh_token set. Capture the POST to "
                "auth.copperlabs.com/oauth/token in the proxy to obtain one, "
                "then pass it as refresh_token."
            )
        resp = requests.post(
            AUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        # Auth0 rotates refresh tokens by default — keep the new one if present:
        self.refresh_token = data.get("refresh_token", self.refresh_token)

    def _auth_header(self) -> dict:
        if not self._token_valid():
            self.refresh()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, path: str, **params) -> dict:
        url = f"{API_BASE}/{path.lstrip('/')}"
        r = self.session.get(url, headers=self._auth_header(), params=params, timeout=30)
        if r.status_code == 401:  # token died early -> refresh once and retry
            self.refresh()
            r = self.session.get(url, headers=self._auth_header(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------- confirmed calls
    def get_state(self) -> dict:
        """Bootstrap payload: user, premise_list, meter_list, gateway_list."""
        return self._get("state")

    def get_forecast(self, premise_id: str) -> dict:
        return self._get("usage/forecast", premise_id=premise_id)

    def get_notifications(self, premise_id: str, limit: int = 20, offset: int = 0) -> dict:
        return self._get(
            "notification/history",
            include_channels="events",
            limit=limit,
            offset=offset,
            premise_id=premise_id,
        )

    def average_series(
        self,
        meter_id: str,
        start: datetime | str,
        end: datetime | str,
        granularity: str = "fifteenminute",
        include_start: bool = True,
    ) -> dict:
        """
        Interval consumption for a single meter.

            GET /average-series/<meter_id>?start=&end=&granularity=&include_start=

        Returns top-level aggregates (min/max/avg/sum of power & energy) plus a
        `series` of {time, energy, power} buckets, where per bucket:
          - `energy` = consumption during the interval, in the meter's native
            unit (electric: kWh, gas: therms/ccf, water: gallons — CONFIRM per
            meter; the API reuses these field names for every meter type).
          - `power`  = the same usage as a per-hour rate (energy x 4 for
            fifteenminute buckets).

        `granularity` observed: "fifteenminute" (hour/day/month likely valid too).
        The meter_id is URL-encoded automatically (e.g. "12:0000000000").
        """
        return self._get(
            f"average-series/{quote(meter_id, safe='')}",
            start=_iso_z(start),
            end=_iso_z(end),
            granularity=granularity,
            include_start=str(include_start).lower(),
        )

    def usage_last_24h(self, meter_id: str, granularity: str = "fifteenminute") -> dict:
        now = datetime.now(timezone.utc)
        return self.average_series(meter_id, now - timedelta(hours=24), now, granularity)

    # -------------------------------------------------------- convenience
    def premises(self) -> list[dict]:
        return self.get_state().get("premise_list", [])

    def meters(self, premise_id: str | None = None) -> list[dict]:
        for p in self.premises():
            if premise_id is None or p["id"] == premise_id:
                return p.get("meter_list", [])
        return []


if __name__ == "__main__":
    import os

    client = CopperClient(
        access_token=os.environ.get("COPPER_TOKEN"),
        refresh_token=os.environ.get("COPPER_REFRESH_TOKEN"),
    )

    state = client.get_state()
    for p in state["premise_list"]:
        print(f'\nPremise: {p["name"]}  ({p["id"]})  tz={p["timezone"]}')
        for m in p["meter_list"]:
            series = client.usage_last_24h(m["id"])
            print(
                f'  {m["type"]:14} id={m["id"]:16} state={m["state"]:10} '
                f'24h sum_energy={series.get("sum_energy", 0):.3f}'
            )
