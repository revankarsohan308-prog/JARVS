"""Social sign-in: real OAuth 2.0 authorization-code flow with PKCE.

Honesty contract (spec §49 / §51):
- A provider with no client credentials configured is *not* presented as
  working. The API reports "Integration not configured." plus the exact
  environment variables and redirect URI required to enable it.
- Nothing here ever simulates a provider response. Network or protocol
  failures surface as explicit errors, never as a successful login.

Security properties:
- ``state`` (256-bit) bound to an HttpOnly, SameSite=Lax cookie → CSRF-safe.
- PKCE S256 → a stolen authorization code is useless without the verifier.
- Only the provider-asserted ``sub`` + email are persisted; access tokens are
  used once for userinfo and then discarded.
- Linking an existing account by email requires the provider to assert
  ``email_verified`` (verified-email takeover defence).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

import httpx

from config.settings import settings
from database.models import OAuthIdentity, User


class OAuthError(Exception):
    """User-facing sign-in failure with an honest, actionable message."""


@dataclass(frozen=True)
class Provider:
    id: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    scopes: tuple[str, ...]
    client_id_setting: str  # env var name, e.g. ALYO_GOOGLE_CLIENT_ID
    client_secret_setting: str
    # PKCE S256 on the authorize+exchange flow. Providers without server-side
    # PKCE support (Facebook: PKCE is OIDC-only) rely on state + the
    # server-side secret exchange instead — both are still real protections.
    use_pkce: bool = True
    extra_auth_params: dict[str, str] = field(default_factory=dict)

    @property
    def client_id(self) -> str:
        # client_id_setting is the env var name (ALYO_GOOGLE_CLIENT_ID);
        # the Settings field is the name without the prefix.
        return getattr(settings, self.client_id_setting.removeprefix("ALYO_").lower())

    @property
    def client_secret(self) -> str:
        return getattr(settings, self.client_secret_setting.removeprefix("ALYO_").lower())


# Adding another provider = one registry entry (see docs/DEVELOPMENT.md).
PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        id="google",
        display_name="Google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
        scopes=("openid", "email", "profile"),
        client_id_setting="ALYO_GOOGLE_CLIENT_ID",
        client_secret_setting="ALYO_GOOGLE_CLIENT_SECRET",
        extra_auth_params={"prompt": "select_account"},
    ),
    "linkedin": Provider(
        id="linkedin",
        display_name="LinkedIn",
        authorization_endpoint="https://www.linkedin.com/oauth/v2/authorization",
        token_endpoint="https://www.linkedin.com/oauth/v2/accessToken",
        userinfo_endpoint="https://api.linkedin.com/v2/userinfo",
        scopes=("openid", "email", "profile"),
        client_id_setting="ALYO_LINKEDIN_CLIENT_ID",
        client_secret_setting="ALYO_LINKEDIN_CLIENT_SECRET",
    ),
    "facebook": Provider(
        id="facebook",
        display_name="Facebook",
        # Current official manual flow (developers.facebook.com — "Manually
        # Build a Login Flow"): authorize dialog → server-side code exchange →
        # GET /me?fields=id,name,email. Graph API v26.0 is pinned and
        # overridable via ALYO_FACEBOOK_*_URL for a future version bump.
        authorization_endpoint="https://www.facebook.com/v26.0/dialog/oauth",
        token_endpoint="https://graph.facebook.com/v26.0/oauth/access_token",
        userinfo_endpoint="https://graph.facebook.com/v26.0/me?fields=id,name,email",
        scopes=("email", "public_profile"),
        client_id_setting="ALYO_FACEBOOK_CLIENT_ID",
        client_secret_setting="ALYO_FACEBOOK_CLIENT_SECRET",
        # PKCE is only supported on Facebook's OIDC flow; the classic code flow
        # (which this registry implements) relies on state + secret exchange.
        use_pkce=False,
    ),
}

STATE_COOKIE = "alyo_oauth"
STATE_TTL_SECONDS = 600


def get_provider(provider_id: str) -> Provider:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise OAuthError(f"Unknown sign-in provider: {provider_id!r}. Available: {', '.join(PROVIDERS)}.")
    return provider


def is_configured(provider: Provider) -> bool:
    return bool(provider.client_id and provider.client_secret)


def default_redirect_base() -> str:
    """Origin used in setup instructions and the redirect URI."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    host = settings.app_host
    # app_host may be a wildcard bind address (0.0.0.0 / ::) which is NOT a
    # valid callback URL — substitute localhost for the *displayed* setup
    # instructions. The real redirect URI at runtime always comes from the
    # incoming request, so this is display-only and stays honest.
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{settings.app_port}"


def setup_requirements(provider: Provider) -> str:
    """Exact steps to enable the provider — shown verbatim to the user."""
    return (
        "Integration not configured. Set "
        f"{provider.client_id_setting} and {provider.client_secret_setting} in .env "
        f"(create an OAuth client for redirect URI {default_redirect_base()}"
        f"/api/v1/auth/oauth/{provider.id}/callback)."
    )


def provider_statuses(redirect_base: str) -> list[dict]:
    """Provider list for the UI: configured flag + honest setup instructions."""
    statuses = []
    for provider in PROVIDERS.values():
        configured = is_configured(provider)
        statuses.append(
            {
                "id": provider.id,
                "display_name": provider.display_name,
                "configured": configured,
                "redirect_uri": f"{redirect_base.rstrip('/')}/api/v1/auth/oauth/{provider.id}/callback",
                "scopes": list(provider.scopes),
                "note": "" if configured else setup_requirements(provider),
            }
        )
    return statuses


# ----------------------------------------------------------------------
# PKCE / state helpers
# ----------------------------------------------------------------------

def new_state() -> tuple[str, str]:
    """Return (state, code_verifier) — both 256-bit random."""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def encode_state_cookie(provider_id: str, state: str, verifier: str) -> str:
    payload = {"p": provider_id, "s": state, "v": verifier, "t": int(time.time())}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_state_cookie(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
        if int(time.time()) - int(payload["t"]) > STATE_TTL_SECONDS:
            return None
        return payload
    except Exception:
        return None


def endpoint(provider: Provider, kind: str) -> str:
    """Effective endpoint URL (``kind``: authorization | token | userinfo).

    Defaults to the provider's public endpoint; an ``ALYO_<PROVIDER>_<KIND>_URL``
    override lets a deployment point sign-in at any OIDC-compliant server
    (Keycloak, Authentik, Azure AD, an internal verification server).
    """
    override = getattr(settings, f"{provider.id}_{kind}_url", "")
    return override or getattr(provider, f"{kind}_endpoint")


def build_authorization_url(provider: Provider, redirect_uri: str, state: str, challenge: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        **provider.extra_auth_params,
    }
    if provider.use_pkce:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    target = endpoint(provider, "authorization")
    sep = "&" if "?" in target else "?"
    return f"{target}{sep}{urlencode(params)}"


# ----------------------------------------------------------------------
# Provider HTTP (kept in two functions so tests can stub the network)
# ----------------------------------------------------------------------

def _post_json(url: str, data: dict) -> dict:
    resp = httpx.post(url, data=data, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _get_json(url: str, headers: dict) -> dict:
    resp = httpx.get(url, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def exchange_code(provider: Provider, code: str, redirect_uri: str, verifier: str) -> str:
    """Authorization code → access token (one call, no token stored)."""
    payload_args: dict = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
    }
    if provider.use_pkce:
        payload_args["code_verifier"] = verifier
    try:
        payload = _post_json(endpoint(provider, "token"), payload_args)
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"{provider.display_name} did not respond to the token exchange ({type(exc).__name__}). "
            "Nothing was signed in — try again."
        ) from exc
    token = payload.get("access_token")
    if not token:
        raise OAuthError(
            f"{provider.display_name} refused the token exchange: "
            f"{payload.get('error_description') or payload.get('error') or 'no access_token in response'}."
        )
    return str(token)


def fetch_userinfo(provider: Provider, access_token: str) -> dict:
    try:
        info = _get_json(endpoint(provider, "userinfo"), {"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"{provider.display_name} did not respond to the identity request ({type(exc).__name__}). "
            "Nothing was signed in — try again."
        ) from exc
    # OIDC providers assert ``sub``; Facebook's Graph API asserts ``id``.
    subject = info.get("sub") or info.get("id")
    if not subject:
        raise OAuthError(f"{provider.display_name} returned no subject id (sub); sign-in aborted.")
    info["sub"] = str(subject)
    if provider.id == "facebook":
        # Meta provides NO email-verification signal (Facebook Login docs):
        # always treat the address as unverified. This keeps the verified-email
        # account-linking defence intact — a Facebook identity can never be
        # used to take over a password account.
        info.setdefault("email_verified", False)
    return info


# ----------------------------------------------------------------------
# Account resolution
# ----------------------------------------------------------------------

def sanitize_username(seed: str) -> str:
    cleaned = "".join(c for c in (seed or "") if c.isalnum() or c in "._-").strip("._-")
    return (cleaned[:48] or "member").lower()


def resolve_account(db, provider: Provider, userinfo: dict) -> tuple[User, str]:
    """Map provider identity → local account.

    Returns ``(user, action)`` where action is one of
    ``existing`` / ``linked`` / ``created``. Raises :class:`OAuthError` when
    the sign-in must be refused (unverified email collision, provisioning off,
    deactivated account).
    """
    from sqlalchemy import func, select

    from core.identity import service as identity
    from core.identity.service import IdentityError
    from core.permissions.rbac import Role
    from security.audit import audit

    subject = str(userinfo["sub"])
    email = str(userinfo.get("email") or "").strip().lower()
    email_verified = bool(userinfo.get("email_verified"))
    display_name = str(userinfo.get("name") or "").strip()[:128]

    # 1) Known subject → straight login.
    link = db.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == provider.id, OAuthIdentity.subject == subject
        )
    )
    if link is not None:
        user = db.get(User, link.user_id)
        if user is None:
            raise OAuthError("The linked account no longer exists. Sign in with your password instead.")
        if not user.is_active:
            raise OAuthError("This account is deactivated. Contact an administrator.")
        return user, "existing"

    # 2) Verified email matching an existing account → link, but only if that
    #    account's email was itself proven by a provider. A password account
    #    merely *claims* the address (no local verification exists yet);
    #    handing it a provider login would enable pre-registered takeover.
    provision_email: str | None = None
    if email and email_verified:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            if not existing.is_active:
                raise OAuthError("This account is deactivated. Contact an administrator.")
            if existing.email_verified:
                db.add(
                    OAuthIdentity(
                        user_id=existing.id,
                        provider=provider.id,
                        subject=subject,
                        email=email,
                        email_verified=True,
                    )
                )
                db.commit()
                audit(
                    db,
                    "oauth_identity_linked",
                    f"provider={provider.id} user={existing.username} email={email}",
                    actor=f"user:{existing.username}",
                    component="identity",
                )
                return existing, "linked"
            # Unverified local claim: leave it alone and open a separate,
            # provider-verified account for the real owner of the address.
            provision_email = None
        else:
            provision_email = email

    # 3) New account — only if auto-provisioning is enabled.
    if not settings.oauth_auto_provision:
        raise OAuthError(
            "No account matches this sign-in and automatic account creation is "
            "disabled (ALYO_OAUTH_AUTO_PROVISION=false). Sign in with a password "
            "first, then link this provider from an administrator."
        )

    # The bootstrap rule is channel-agnostic: the very first account on a
    # fresh deployment becomes SUPER_ADMIN, every later signup is STUDENT.
    user_count = db.scalar(select(func.count(User.id))) or 0
    role = Role.SUPER_ADMIN.value if user_count == 0 else Role.STUDENT.value

    # Username from the email local part (or display name), uniquified.
    seed = sanitize_username((email.split("@")[0] if email else "") or display_name or provider.id)
    username = seed
    for n in range(2, 50):
        if not db.scalar(select(User).where(User.username == username)):
            break
        username = f"{seed[:50]}-{n}"
    else:  # pragma: no cover - pathological collision
        username = f"{seed[:40]}-{secrets.token_hex(3)}"

    try:
        user = identity.register_user(
            db,
            username,
            secrets.token_urlsafe(32),  # unusable: social accounts cannot password-login
            role=role,
            # Only a provider-proven, unclaimed address enters the unique email
            # column; the raw claim stays on the identity row either way.
            email=provision_email,
            display_name=display_name or username,
            actor=f"oauth:{provider.id}",
        )
    except IdentityError as exc:
        raise OAuthError(f"Could not create a local account: {exc}") from exc

    if provision_email:
        user.email_verified = True
    db.add(
        OAuthIdentity(
            user_id=user.id,
            provider=provider.id,
            subject=subject,
            email=email,
            email_verified=email_verified,
        )
    )
    db.commit()
    audit(
        db,
        "oauth_account_created",
        f"provider={provider.id} username={user.username} role={role} email_verified={email_verified}",
        actor=f"user:{user.username}",
        component="identity",
    )
    return user, "created"
