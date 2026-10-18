from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.config import Settings


class GoogleOAuthError(Exception):
    pass


def authorization_url(settings: Settings, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_code(settings: Settings, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            token = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise GoogleOAuthError("Google did not accept the sign-in code.") from exc
    access = token.get("access_token")
    if not access:
        raise GoogleOAuthError("Google did not return an access token.")
    info_request = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access}"},
    )
    try:
        with urllib.request.urlopen(info_request, timeout=20) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise GoogleOAuthError("Google did not return a profile.") from exc
    return profile
