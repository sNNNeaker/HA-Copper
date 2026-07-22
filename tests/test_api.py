"""Unit tests for api.py: helpers and CopperClient with mocked HTTP.

Response shapes come from the documented captures (see README "Endpoints
used"); no real tokens or account data appear here.
"""

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import api


# --------------------------------------------------------------------- helpers
def _fake_jwt(exp: int) -> str:
    """Build header.payload.sig with only the payload being real base64 JSON."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"xxx.{payload.decode()}.yyy"


def test_jwt_exp_reads_exp():
    assert api._jwt_exp(_fake_jwt(1234567890)) == 1234567890


def test_jwt_exp_garbage_returns_zero():
    # Malformed tokens must read as "expired", never raise.
    assert api._jwt_exp("not-a-jwt") == 0
    assert api._jwt_exp("") == 0


def test_iso_z_naive_treated_as_utc():
    dt = datetime(2026, 7, 22, 6, 0, 0)
    assert api._iso_z(dt) == "2026-07-22T06:00:00.000Z"


def test_iso_z_aware_normalised_to_utc():
    # 08:00 at +02:00 is 06:00 UTC — the 'Z' must be truthful.
    from datetime import timedelta, timezone as tz

    dt = datetime(2026, 7, 22, 8, 0, 0, tzinfo=tz(timedelta(hours=2)))
    assert api._iso_z(dt) == "2026-07-22T06:00:00.000Z"


def test_iso_z_string_passthrough():
    assert api._iso_z("2026-01-01T00:00:00.000Z") == "2026-01-01T00:00:00.000Z"


def test_b64url_no_padding():
    out = api._b64url(b"\x00\x01\x02")
    assert "=" not in out and "+" not in out and "/" not in out


def test_pkce_pair_challenge_matches_verifier():
    verifier, challenge = api._pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    # Two calls must never produce the same secret.
    assert api._pkce_pair()[0] != verifier


# --------------------------------------------------------------------- refresh
def _token_response(access="new-access", refresh="new-refresh"):
    resp = MagicMock(status_code=200)
    body = {"access_token": access}
    if refresh is not None:
        body["refresh_token"] = refresh
    resp.json.return_value = body
    return resp


def test_refresh_updates_tokens_and_fires_callback():
    client = api.CopperClient(refresh_token="old-refresh")
    seen = []
    client.token_callback = seen.append
    with patch.object(api.requests, "post", return_value=_token_response()) as post:
        client.refresh()
    assert client.access_token == "new-access"
    assert client.refresh_token == "new-refresh"
    # Rotation must be reported exactly once, with the new token.
    assert seen == ["new-refresh"]
    # The refresh grant must carry the old token, not the new one.
    assert post.call_args.kwargs["json"]["refresh_token"] == "old-refresh"


def test_refresh_without_rotation_keeps_token_and_stays_quiet():
    client = api.CopperClient(refresh_token="same-token")
    seen = []
    client.token_callback = seen.append
    with patch.object(api.requests, "post", return_value=_token_response(refresh=None)):
        client.refresh()
    assert client.refresh_token == "same-token"
    assert seen == []  # no rotation -> no persistence churn


def test_refresh_callback_errors_are_swallowed():
    # Persistence problems must never break the API call itself.
    client = api.CopperClient(refresh_token="old")
    client.token_callback = MagicMock(side_effect=RuntimeError("disk full"))
    with patch.object(api.requests, "post", return_value=_token_response()):
        client.refresh()
    assert client.access_token == "new-access"


def test_refresh_http_error_raises_auth_error():
    resp = MagicMock(status_code=403, text='{"error":"invalid_grant"}')
    client = api.CopperClient(refresh_token="revoked")
    with patch.object(api.requests, "post", return_value=resp):
        with pytest.raises(api.CopperAuthError):
            client.refresh()


def test_refresh_without_token_raises():
    with pytest.raises(api.CopperAuthError):
        api.CopperClient().refresh()


def test_store_tokens_requires_refresh_token():
    client = api.CopperClient()
    with pytest.raises(api.CopperAuthError):
        client._store_tokens({"access_token": "a"})  # no offline_access granted


# ------------------------------------------------------------------- data GET
def test_get_retries_once_on_401():
    client = api.CopperClient(
        access_token=_fake_jwt(int(time.time()) + 3600),  # currently valid
        refresh_token="r",
    )
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"results": []}
    dead = MagicMock(status_code=401)
    client.session = MagicMock()
    client.session.get.side_effect = [dead, ok]
    with patch.object(client, "refresh") as refresh:
        out = client._get("usage/12%3A0000000000")
    refresh.assert_called_once()          # token died early -> one refresh
    assert client.session.get.call_count == 2  # and exactly one retry
    assert out == {"results": []}


# ------------------------------------------------------------------ login flow
def test_complete_email_login_falls_back_only_when_grant_disabled():
    client = api.CopperClient()
    grant_disabled = MagicMock(
        status_code=403, text='{"error":"unauthorized_client","error_description":"Grant type ... not allowed for the client."}'
    )
    with patch.object(api.requests, "post", return_value=grant_disabled):
        with patch.object(client, "_verify_and_authorize") as fallback:
            client.complete_email_login("you@example.com", "123456")
    fallback.assert_called_once()  # disabled grant -> replay the app's flow


def _redirect(location):
    resp = MagicMock(status_code=302)
    resp.headers = {"location": location}
    resp.url = "https://auth.copperlabs.com/hop"
    return resp


def test_verify_and_authorize_completes_via_verify_redirect():
    client = api.CopperClient()
    client._pending = {"email": "you@example.com", "verifier": "v", "challenge": "c", "state": "s"}
    client.session = MagicMock()
    client.session.post.return_value = MagicMock(status_code=200)  # /verify ok
    client.session.get.return_value = _redirect(
        "com.copperlabs.copper.rn://auth.copperlabs.com/cb?code=AUTHCODE&state=s"
    )
    with patch.object(api.requests, "post", return_value=_token_response()) as post:
        client._verify_and_authorize("you@example.com", "123456")
    assert client.refresh_token == "new-refresh"
    # The first GET must be the hosted verify_redirect completion, carrying the
    # code and OUR PKCE challenge...
    first_get = client.session.get.call_args_list[0]
    assert "passwordless/verify_redirect" in first_get.args[0]
    assert first_get.kwargs["params"]["verification_code"] == "123456"
    assert first_get.kwargs["params"]["code_challenge"] == "c"
    # ...and the token exchange must use the matching verifier + auth code.
    exchanged = post.call_args.kwargs["json"]
    assert exchanged["code"] == "AUTHCODE"
    assert exchanged["code_verifier"] == "v"


def test_verify_and_authorize_falls_back_to_silent_authorize():
    client = api.CopperClient()
    client._pending = {"email": "you@example.com", "verifier": "v", "challenge": "c", "state": "s"}
    client.session = MagicMock()
    client.session.post.return_value = MagicMock(status_code=200)
    # verify_redirect fails outright (400) -> silent /authorize succeeds.
    bad = MagicMock(status_code=400, text="unsupported")
    good = _redirect("com.copperlabs.copper.rn://auth.copperlabs.com/cb?code=X&state=s")
    client.session.get.side_effect = [bad, good]
    with patch.object(api.requests, "post", return_value=_token_response()):
        client._verify_and_authorize("you@example.com", "123456")
    second_get = client.session.get.call_args_list[1]
    assert "authorize" in second_get.args[0]
    assert second_get.kwargs["params"]["prompt"] == "none"


def test_follow_to_code_rejects_state_mismatch():
    # A tampered/replayed redirect must not be accepted (CSRF check).
    client = api.CopperClient()
    client.session = MagicMock()
    client.session.get.return_value = _redirect(
        "com.copperlabs.copper.rn://auth.copperlabs.com/cb?code=X&state=EVIL"
    )
    with pytest.raises(api.CopperAuthError, match="State mismatch"):
        client._follow_to_code(api.AUTHORIZE_URL, {}, expected_state="GOOD")


def test_complete_email_login_wrong_code_surfaces_no_fallback():
    client = api.CopperClient()
    wrong_code = MagicMock(
        status_code=403, text='{"error":"invalid_grant","error_description":"Wrong email or verification code."}'
    )
    with patch.object(api.requests, "post", return_value=wrong_code):
        with patch.object(client, "_verify_and_authorize") as fallback:
            with pytest.raises(api.CopperAuthError):
                client.complete_email_login("you@example.com", "000000")
    fallback.assert_not_called()  # a bad code must reach the user, not loop
