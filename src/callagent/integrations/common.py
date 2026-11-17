"""What every integration shares: the shared-secret check, and reading a lead from a request."""

import hmac
from datetime import datetime
from typing import Any

from fastapi import Header, HTTPException, Query

from callagent.book import Lead
from callagent.service import CallService, UnknownCall, UnknownLead

LEAD_FIELDS = (
    "lead_id",
    "first",
    "last",
    "phone",
    "timezone",
    "consent",
    "inquiry",
    "inquiry_date",
)


def require_key(api_key: str | None):
    """A dependency that checks ``Authorization: Bearer <key>`` or ``X-Api-Key: <key>``.

    With no key configured every request is let through, which is fine on a laptop and not
    on the internet: set ``CALLAGENT_API_KEY`` before exposing the server.
    """

    def check(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        sent = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
        if not key_matches(api_key, sent):
            raise HTTPException(401, "Missing or wrong API key")

    return check


def require_key_anywhere(api_key: str | None):
    """For webhooks: the key may also come as ``?key=`` or in ``X-Vapi-Secret``.

    Some platforms cannot add a header to the webhooks they send, so the secret has to go
    in the URL. A URL can end up in logs, so prefer a header where the platform allows one.
    """

    def check(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
        x_vapi_secret: str | None = Header(default=None),
        key: str | None = Query(default=None),
    ) -> None:
        bearer = (authorization or "").removeprefix("Bearer ").strip()
        if not any(key_matches(api_key, sent) for sent in (x_api_key, x_vapi_secret, key, bearer)):
            raise HTTPException(401, "Missing or wrong API key")

    return check


def key_matches(api_key: str | None, sent: str | None) -> bool:
    """True when no key is configured, or ``sent`` is the configured key."""
    return not api_key or hmac.compare_digest((sent or "").encode(), api_key.encode())


def dig(body: Any, path: tuple[str, ...]) -> Any:
    """``body[a][b][c]`` for a path ``(a, b, c)``, or None if any step is missing."""
    value = body
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def gather(body: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    """The per-call values found at ``paths``, the first path listed winning."""
    values: dict[str, Any] = {}
    for path in reversed(paths):
        found = dig(body, path)
        if isinstance(found, dict):
            values.update({k: v for k, v in found.items() if v not in (None, "")})
    return values


def lead_from(service: CallService, data: dict[str, Any]) -> Lead:
    try:
        return service.lead({key: data[key] for key in LEAD_FIELDS if key in data})
    except UnknownLead as error:
        raise HTTPException(422, f"Unknown lead: {error.args[0]}") from None


def moment(value: Any) -> datetime | None:
    """An optional ISO time with a zone, for tests and replays. None means now."""
    if not value:
        return None
    at = datetime.fromisoformat(str(value))
    if at.tzinfo is None:
        raise HTTPException(422, "now must include a time zone, for example 2026-11-18T10:30-05:00")
    return at


def not_found(error: UnknownCall) -> HTTPException:
    return HTTPException(404, f"No call in progress with id {error.args[0]}")
