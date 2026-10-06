"""Social sign-in routes: provider list, authorization start, callback.

The flow is a real OAuth 2.0 authorization-code exchange with PKCE. None of
these endpoints require an existing session — they *create* one. Every outcome
(success, creation, linking, refusal) is audited; forged or replayed ``state``
values feed the threat detector (spec §15.3).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.deps import DbDep
from config.settings import settings
from core.identity import oauth
from core.identity.oauth import OAuthError
from core.identity.service import create_session
from security.audit import audit
from security.monitoring import record_threat_event
from security.ratelimit import limiter
from timeutil import utcnow

router = APIRouter(prefix="/auth/oauth", tags=["auth"])


def _base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _redirect_uri(request: Request, provider: oauth.Provider) -> str:
    return f"{_base_url(request)}/api/v1/auth/oauth/{provider.id}/callback"


def _failure_response(db, request: Request, provider_id: str, reason: str, *, security: bool):
    """Audit a callback failure; score state failures as possible CSRF/replay."""
    host = request.client.host if request.client else "unknown"
    audit(
        db,
        "oauth_callback_failed",
        f"provider={provider_id} reason={reason} source={host}",
        actor="anonymous",
        component="identity",
    )
    if not security:
        raise HTTPException(status_code=400, detail=reason)

    key = f"oauth_fail:{host}"
    allowed, retry = limiter.check(key, 5, 300)
    failures = limiter.failures(key, 300)
    record_threat_event(
        db,
        "oauth_state_mismatch",
        component="auth",
        risk_score=min(35 + failures * 5, 75),
        evidence=[
            f"{failures} failed OAuth callback validations from {host} within 300s",
            f"provider={provider_id}",
            f"cause: {reason} (possible causes: CSRF attempt, replayed link, or blocked cookies)",
        ],
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed sign-in callbacks. Try again in {retry}s.",
        )
    raise HTTPException(status_code=400, detail=reason)


@router.get("/providers")
def providers(request: Request):
    """Which sign-in providers exist, and exactly what each one needs."""
    base = _base_url(request)
    return {"providers": oauth.provider_statuses(f"{base}")}


@router.get("/{provider_id}/start")
def start(provider_id: str, request: Request):
    """Redirect to the provider with a CSRF state + PKCE challenge."""
    try:
        provider = oauth.get_provider(provider_id)
    except OAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not oauth.is_configured(provider):
        raise HTTPException(status_code=400, detail=oauth.setup_requirements(provider))

    allowed, retry = limiter.check(
        f"oauth_start:{request.client.host if request.client else 'unknown'}", 20, 300
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry in {retry}s.")

    state, verifier = oauth.new_state()
    url = oauth.build_authorization_url(
        provider, _redirect_uri(request, provider), state, oauth.pkce_challenge(verifier)
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        oauth.STATE_COOKIE,
        oauth.encode_state_cookie(provider.id, state, verifier),
        max_age=oauth.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        path="/api/v1/auth/oauth",
    )
    return response


@router.get("/{provider_id}/callback")
def callback(provider_id: str, request: Request, db: DbDep, code: str = "", state: str = "", error: str = ""):
    try:
        provider = oauth.get_provider(provider_id)
    except OAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not oauth.is_configured(provider):
        raise HTTPException(status_code=400, detail=oauth.setup_requirements(provider))

    # --- state / cookie validation (CSRF + replay protection) --------------
    cookie = oauth.decode_state_cookie(request.cookies.get(oauth.STATE_COOKIE))
    reason = None
    if cookie is None:
        reason = "missing or expired sign-in cookie (cookies blocked, or link reused after 10 minutes)"
    elif cookie.get("p") != provider.id:
        reason = "sign-in cookie belongs to a different provider"
    elif not state or cookie.get("s") != state:
        reason = "state mismatch (possible CSRF attempt or replayed link)"
    if reason:
        _failure_response(db, request, provider.id, reason, security=True)

    base = _base_url(request)
    error_url = f"{base}/?oauth_error="
    response_delete = {"path": "/api/v1/auth/oauth"}

    # --- provider said no (user cancelled / denied) -----------------------
    if error:
        audit(
            db,
            "oauth_consent_denied",
            f"provider={provider.id} error={error}",
            actor="anonymous",
            component="identity",
        )
        response = RedirectResponse(
            error_url + quote(f"{provider.display_name} sign-in was cancelled ({error})."),
            status_code=302,
        )
        response.delete_cookie(oauth.STATE_COOKIE, **response_delete)
        return response

    if not code:
        _failure_response(db, request, provider.id, "no authorization code in callback", security=True)

    # --- real exchange with the provider ----------------------------------
    try:
        access_token = oauth.exchange_code(
            provider, code, _redirect_uri(request, provider), str(cookie["v"])
        )
        userinfo = oauth.fetch_userinfo(provider, access_token)
        user, action = oauth.resolve_account(db, provider, userinfo)
    except OAuthError as exc:
        audit(
            db,
            "oauth_login_refused",
            f"provider={provider.id} reason={exc}",
            actor="anonymous",
            component="identity",
        )
        response = RedirectResponse(error_url + quote(str(exc)), status_code=302)
        response.delete_cookie(oauth.STATE_COOKIE, **response_delete)
        return response

    # --- success: issue a normal ALYO session, hand the token to the UI ----
    token, expires = create_session(db, user, client_label=f"oauth:{provider.id}")
    user.last_login_at = utcnow()
    db.commit()
    audit(
        db,
        "oauth_login_success",
        f"provider={provider.id} user={user.username} action={action}",
        actor=f"user:{user.username}",
        component="identity",
    )
    response = RedirectResponse(f"{base}/#alyo_token={token}", status_code=302)
    response.delete_cookie(oauth.STATE_COOKIE, **response_delete)
    return response
