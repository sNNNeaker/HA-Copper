"""Thin *synchronous* client for the Copper Labs consumer app API.

Everything here was reverse-engineered from the iOS app's network traffic,
because Copper publishes no official developer docs for the consumer API.

Design notes:
  * Auth is Auth0 (auth.copperlabs.com): an RS256 bearer JWT that lives ~24h.
    Login is passwordless email code; we request `offline_access` so we also get
    a refresh token and can mint new access tokens without re-login.
  * This module is deliberately synchronous (uses `requests`). Home Assistant is
    async, so the coordinator/config-flow call these methods in the executor to
    avoid blocking the event loop. Sync also means it runs as a plain script.
"""

from __future__ import annotations

import base64          # decode JWT payloads; build PKCE values
import hashlib         # SHA-256 for the PKCE code_challenge
import json            # JWT payload + API responses are JSON
import logging         # debug logs into HA's standard logging (or stderr as a script)
import secrets         # cryptographically-random PKCE verifier + state
import time            # compare token `exp` against "now"
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

# NOTE for all logging in this module: log endpoints, status codes and events —
# NEVER token values, the emailed code, or PKCE secrets.
_LOGGER = logging.getLogger(__name__)

# --- endpoints & fixed client parameters --------------------------------------
API_BASE = "https://api.copperlabs.com/api/v2/app"
AUTH_TOKEN_URL = "https://auth.copperlabs.com/oauth/token"
AUTHORIZE_URL = "https://auth.copperlabs.com/authorize"
PASSWORDLESS_START_URL = "https://auth.copperlabs.com/passwordless/start"
PASSWORDLESS_VERIFY_URL = "https://auth.copperlabs.com/passwordless/verify"
# Auth0's hosted completion endpoint: verifies the code, LOGS THE SESSION IN,
# and redirects through /authorize back to the app callback. This is what
# auth0.js navigates to after /passwordless/verify — unlike verify alone, it
# actually establishes the session that /authorize needs.
PASSWORDLESS_VERIFY_REDIRECT_URL = "https://auth.copperlabs.com/passwordless/verify_redirect"

# The app's public OAuth client id (`azp` in its JWT). Public client, no secret.
CLIENT_ID = "l8aJ85JrIRq44CWDEcAlHLqR5wUl8Hjh"
# The custom-scheme URL the app registers; Auth0 redirects the auth code here.
REDIRECT_URI = "com.copperlabs.copper.rn://auth.copperlabs.com/ios/com.copperlabs.copper.rn/callback"
# Auth0's passwordless one-time-code grant type (the "OTP grant").
PASSWORDLESS_OTP_GRANT = "http://auth0.com/oauth/grant-type/passwordless/otp"
# Access token must be valid for this API (the app's JWT `aud`).
API_AUDIENCE = "https://api.copperlabs.com"
# offline_access is what makes Auth0 return a refresh token we can persist.
LOGIN_SCOPE = "openid profile email offline_access"


def _jwt_exp(token: str) -> int:
    """Return a JWT's `exp` (unix seconds) WITHOUT verifying the signature.

    We only need to know when to refresh; verifying is the server's job. Returns
    0 on any problem, which callers treat as "expired" -> refresh.
    """
    try:
        payload_b64 = token.split(".")[1]                 # header.PAYLOAD.sig
        payload_b64 += "=" * (-len(payload_b64) % 4)      # restore base64 padding
        return int(json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp", 0))
    except Exception:
        return 0


def _iso_z(dt: datetime | str) -> str:
    """Format a datetime as the API's exact shape: 2026-07-22T06:00:00.000Z."""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:                    # treat naive input as UTC...
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)         # ...and normalise so 'Z' is truthful
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _b64url(raw: bytes) -> str:
    """base64url-encode without padding (PKCE + state formatting)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for an OAuth PKCE exchange.

    verifier = random secret; challenge = base64url(SHA256(verifier)). PKCE lets a
    public client prove it initiated the auth without needing a client secret.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class CopperAuthError(Exception):
    """Auth-specific failure (bad code, rejected/expired token, disabled grant).

    Separate from network errors so the config flow can show a precise message.
    """


class CopperClient:
    def __init__(self, access_token=None, refresh_token=None, client_id=CLIENT_ID):
        self.access_token = access_token      # short-lived (~24h) bearer
        self.refresh_token = refresh_token    # long-lived; the value we persist
        self.client_id = client_id
        self.session = requests.Session()     # pooled connections + shared cookies
        self._pending: dict | None = None     # PKCE/state stashed between login steps
        # Optional hook fired with the new refresh token whenever Auth0 rotates
        # it, so the owner can persist it IMMEDIATELY. Auth0's reuse detection
        # can revoke the whole token family if a stale token is replayed, so a
        # rotated-but-unpersisted token must never be lost to a crash.
        self.token_callback = None

    # ------------------------------------------------------------------ tokens
    def _set_refresh_token(self, new_token: str | None) -> None:
        """Adopt a (possibly rotated) refresh token and notify the owner."""
        if not new_token or new_token == self.refresh_token:
            return
        _LOGGER.debug("Refresh token rotated by Auth0; adopting the new one")
        self.refresh_token = new_token
        if self.token_callback:
            try:
                self.token_callback(new_token)
            except Exception:  # noqa: BLE001 — persistence must never break an API call
                pass

    def _token_valid(self, skew: int = 120) -> bool:
        """True if the access token won't expire within `skew` seconds."""
        return bool(self.access_token) and _jwt_exp(self.access_token) - skew > time.time()

    def refresh(self) -> None:
        """Swap the refresh token for a fresh access token (Auth0 refresh grant)."""
        if not self.refresh_token:
            raise CopperAuthError("No refresh_token set.")
        _LOGGER.debug("Refreshing access token via refresh_token grant")
        resp = requests.post(
            AUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise CopperAuthError(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self.access_token = data["access_token"]
        # Auth0 rotates refresh tokens: adopt the new one if returned (and let
        # the owner persist it via token_callback), else retain the current one.
        self._set_refresh_token(data.get("refresh_token"))

    def _auth_header(self) -> dict:
        """Authorization header, refreshing first if the token is stale."""
        if not self._token_valid():
            self.refresh()
        return {"Authorization": f"Bearer {self.access_token}"}

    # ------------------------------------------------------------ email login
    def start_email_login(self, email: str) -> None:
        """Passwordless step 1: ask Auth0 to email a 6-digit code.

        Also generates the PKCE + state values now, so whichever completion path
        we use later (OTP grant or verify+authorize) has what it needs. The body
        mirrors the app's /passwordless/start (connection "email", send "code").
        """
        verifier, challenge = _pkce_pair()
        state = _b64url(secrets.token_bytes(16))
        self._pending = {"email": email, "verifier": verifier,
                         "challenge": challenge, "state": state}
        _LOGGER.debug("Requesting sign-in code email via passwordless/start")
        resp = requests.post(
            PASSWORDLESS_START_URL,
            json={
                "client_id": self.client_id,
                "connection": "email",
                "email": email,
                "send": "code",
                # authParams echo the app; used if the verify+authorize path runs.
                "authParams": {
                    "response_type": "code",
                    "redirect_uri": REDIRECT_URI,
                    "state": state,
                },
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise CopperAuthError(f"Could not send code: {resp.status_code} {resp.text[:200]}")

    def complete_email_login(self, email: str, code: str) -> None:
        """Passwordless step 2: exchange the emailed code for tokens.

        Tries the clean OTP grant first. If Copper's client has that grant
        disabled (its app uses the browser passwordless flow), falls back to
        replaying that flow: verify the code, then /authorize with PKCE to get an
        auth code, then exchange it. Either way we end up with a refresh token.
        """
        try:
            self._otp_grant(email, code)
            return
        except CopperAuthError as err:
            msg = str(err).lower()
            # Only fall back when the grant itself is disallowed — NOT when the
            # code was simply wrong (that should surface to the user as-is).
            grant_disabled = "unauthorized_client" in msg or "not allowed for the client" in msg
            if not grant_disabled:
                raise
            _LOGGER.debug(
                "OTP grant not enabled for this client; falling back to the "
                "verify + authorize flow"
            )
        # Fallback: the app's exact verify -> authorize(PKCE) -> token sequence.
        self._verify_and_authorize(email, code)

    def _otp_grant(self, email: str, code: str) -> None:
        """Path A: Auth0 passwordless OTP grant (code -> tokens in one call)."""
        resp = requests.post(
            AUTH_TOKEN_URL,
            json={
                "grant_type": PASSWORDLESS_OTP_GRANT,
                "client_id": self.client_id,
                "username": email,     # the address the code went to
                "otp": code,           # the 6-digit code the user typed
                "realm": "email",      # the passwordless connection
                "audience": API_AUDIENCE,
                "scope": LOGIN_SCOPE,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            # 403 unauthorized_client => grant disabled (caller falls back);
            # 403 invalid_grant "Wrong ... verification code" => bad code (surfaced).
            raise CopperAuthError(f"Code exchange failed: {resp.status_code} {resp.text[:200]}")
        self._store_tokens(resp.json())

    def _verify_and_authorize(self, email: str, code: str) -> None:
        """Path B: replay Auth0's hosted passwordless completion.

        1) POST /passwordless/verify — validates the code (clean "wrong code"
           error), but does NOT log a session in on its own.
        2) GET /passwordless/verify_redirect with the full authorize params —
           Auth0 re-verifies the code, establishes the session, and 302s
           through /authorize to the app callback with ?code=... (this is the
           navigation auth0.js performs after verify; the code stays valid for
           this pair of calls). If that yields nothing, fall back to the old
           silent prompt=none /authorize as a last resort.
        3) Trade the auth code (+ PKCE verifier) for tokens.
        """
        p = self._pending or {}
        verifier, challenge = p.get("verifier"), p.get("challenge")
        if not verifier or not challenge:
            # No pending login (fresh client) -> mint ONE matched pair. Verifier
            # and challenge must come from the same pair or the exchange fails.
            verifier, challenge = _pkce_pair()
        state = p.get("state") or _b64url(secrets.token_bytes(16))

        # 1) Validate the code (shared session so any cookie carries forward).
        r = self.session.post(
            PASSWORDLESS_VERIFY_URL,
            json={"connection": "email", "email": email, "verification_code": code},
            timeout=30,
        )
        if r.status_code >= 400:
            raise CopperAuthError(f"Code rejected: {r.status_code} {r.text[:200]}")
        _LOGGER.debug("passwordless/verify accepted the code")

        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": LOGIN_SCOPE,
            "audience": API_AUDIENCE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        # 2) Hosted completion (primary), silent authorize (last resort).
        try:
            auth_code = self._follow_to_code(
                PASSWORDLESS_VERIFY_REDIRECT_URL,
                {
                    **auth_params,
                    "connection": "email",
                    "email": email,
                    "verification_code": code,
                },
                expected_state=state,
            )
        except CopperAuthError as err:
            _LOGGER.debug(
                "verify_redirect did not yield an auth code (%s); "
                "trying silent /authorize",
                err,
            )
            auth_code = self._follow_to_code(
                AUTHORIZE_URL,
                {
                    **auth_params,
                    "prompt": "none",      # only works if a session now exists
                    "login_hint": email,
                    "connection": "email",
                },
                expected_state=state,
            )

        # 3) Trade the auth code (+ PKCE verifier) for tokens.
        resp = requests.post(
            AUTH_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise CopperAuthError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")
        self._store_tokens(resp.json())

    def _follow_to_code(
        self,
        url: str,
        params: dict,
        expected_state: str | None = None,
        max_hops: int = 10,
    ) -> str:
        """GET an Auth0 endpoint and follow redirects until the auth code appears.

        Auth0 answers with 302 chains that eventually redirect to the app's
        custom-scheme REDIRECT_URI carrying ?code=... We follow http(s) hops
        manually (requests can't follow the custom scheme) and read the code off
        the first custom-scheme Location.
        """
        endpoint = urlparse(url).path
        resp = self.session.get(url, params=params, allow_redirects=False, timeout=30)
        for _ in range(max_hops):
            if resp.status_code not in (301, 302, 303, 307, 308):
                raise CopperAuthError(
                    f"{endpoint} did not redirect ({resp.status_code}) {resp.text[:200]}"
                )
            loc = resp.headers.get("location", "")
            if loc.startswith("com.copperlabs"):        # reached the app callback
                q = parse_qs(urlparse(loc).query)
                if "code" in q:
                    # CSRF check: the state we sent must come back unchanged.
                    if expected_state and q.get("state", [None])[0] != expected_state:
                        raise CopperAuthError("State mismatch in auth redirect.")
                    _LOGGER.debug("Auth code obtained via %s", endpoint)
                    return q["code"][0]
                # e.g. ?error=login_required when no session was established
                raise CopperAuthError(f"No auth code in redirect: {loc}")
            resp = self.session.get(urljoin(resp.url, loc), allow_redirects=False, timeout=30)
        raise CopperAuthError("Auth code not found in redirect chain.")

    def _store_tokens(self, data: dict) -> None:
        """Save tokens from any successful grant; require a refresh token."""
        self.access_token = data["access_token"]
        if not data.get("refresh_token"):
            raise CopperAuthError("No refresh_token returned; offline_access may be disabled.")
        self._set_refresh_token(data["refresh_token"])

    # --------------------------------------------------------------- data API
    def _get(self, path: str, **params) -> dict:
        """GET a JSON endpoint under API_BASE with auth + one 401 refresh-retry."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        r = self.session.get(url, headers=self._auth_header(), params=params, timeout=30)
        if r.status_code == 401:              # token died early -> refresh once, retry
            _LOGGER.debug("GET %s -> 401; refreshing token and retrying once", path)
            self.refresh()
            r = self.session.get(url, headers=self._auth_header(), params=params, timeout=30)
        _LOGGER.debug("GET %s -> %s", path, r.status_code)
        r.raise_for_status()
        return r.json()

    def get_state(self) -> dict:
        """Bootstrap: user, premises, meter list, gateways. Also validates auth."""
        return self._get("state")

    def average_series(self, meter_id, start, end,
                       granularity="fifteenminute", include_start=True) -> dict:
        """Interval consumption for a meter (kept for reference; usage() is used)."""
        return self._get(
            f"average-series/{quote(meter_id, safe='')}",  # encode ':' in meter ids
            start=_iso_z(start),
            end=_iso_z(end),
            granularity=granularity,
            include_start=str(include_start).lower(),       # API wants "true"/"false"
        )

    def usage(self, meter_id, start, end, granularity="auto", include_start=True) -> dict:
        """Cumulative register + interval usage for one meter.

            GET /usage/<meter_id>?start=&end=&granularity=&include_start=

        Returns {"results": [{time, power, usage, value, actual}, ...]}:
          - `value`  = cumulative meter reading (gas: CCF, water: gallons).
                       Monotonic; ideal for a total_increasing sensor.
          - `usage`  = consumption during the interval.
          - `power`  = usage as a per-hour rate (already normalised, any bucket).
          - `actual` = value*100 (integer register; coarser — prefer `value`).
        Trailing buckets with no data yet come back as value/power = null.
        """
        return self._get(
            f"usage/{quote(meter_id, safe='')}",
            start=_iso_z(start),
            end=_iso_z(end),
            granularity=granularity,
            include_start=str(include_start).lower(),
        )
