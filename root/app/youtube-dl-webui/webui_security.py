"""Authentication, CSRF protection, security headers and URL validation.

Credentials come only from environment variables and are compared with
``secrets.compare_digest``. If the web UI is enabled but no credentials are
configured, the app fails closed (see ``load_credentials`` / ``Config``).
"""

import base64
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

# Methods that change state and therefore require CSRF protection.
_UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _env(name):
    value = os.environ.get(name)
    return value if value else None


class Config:
    """Resolved auth configuration, built once at startup."""

    def __init__(self):
        self.username = _env('WEBUI_USERNAME')
        self.password = _env('WEBUI_PASSWORD')
        self.api_token = _env('WEBUI_API_TOKEN')
        self.readonly = os.environ.get('WEBUI_READONLY', '').lower() == 'true'
        self.allow_unsafe_args = (
            os.environ.get('WEBUI_ALLOW_UNSAFE_ARGS', '').lower() == 'true')
        allowed = _env('WEBUI_ALLOWED_DOMAINS')
        self.allowed_domains = tuple(
            d.strip().lower().lstrip('.') for d in allowed.split(',') if d.strip()
        ) if allowed else ()
        # New random token per process start; embedded in forms, never stored.
        self.csrf_token = secrets.token_urlsafe(32)

    @property
    def has_basic_auth(self):
        return bool(self.username and self.password)

    def startup_error(self):
        """Return a human-readable reason the UI must not start, or None."""
        if not (self.has_basic_auth or self.api_token):
            return ('no credentials configured: set WEBUI_USERNAME and '
                    'WEBUI_PASSWORD, and/or WEBUI_API_TOKEN')
        if self.username and not self.password:
            return 'WEBUI_USERNAME is set but WEBUI_PASSWORD is empty'
        if self.password and not self.username:
            return 'WEBUI_PASSWORD is set but WEBUI_USERNAME is empty'
        return None


def _constant_eq(a, b):
    return secrets.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def _check_bearer(request, config):
    """Return True if a valid API token is presented, False if none is, and
    raise 401 if one is presented but wrong."""
    header = request.headers.get('authorization', '')
    if not header.lower().startswith('bearer '):
        return False
    if not config.api_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid token')
    presented = header[7:].strip()
    if _constant_eq(presented, config.api_token):
        return True
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid token')


def _check_basic(request, config):
    """Return True on valid Basic credentials, else raise 401 with a challenge
    so browsers prompt for a username and password."""
    header = request.headers.get('authorization', '')
    challenge = {'WWW-Authenticate': 'Basic realm="youtube-dl"'}
    if not config.has_basic_auth or not header.lower().startswith('basic '):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'authentication required',
                            headers=challenge)
    try:
        decoded = base64.b64decode(header[6:].strip()).decode('utf-8')
        user, _, pw = decoded.partition(':')
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'authentication required',
                            headers=challenge)
    # Evaluate both comparisons regardless, to avoid leaking which one failed.
    ok_user = _constant_eq(user, config.username)
    ok_pw = _constant_eq(pw, config.password)
    if ok_user and ok_pw:
        return True
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'authentication required',
                        headers=challenge)


def authenticate(request, config, *, allow_token):
    """Authenticate a request.

    Returns the authentication kind used: 'token' or 'basic'. ``allow_token``
    is True only for the download endpoints; everything else (config editing,
    logs, restart) requires the interactive Basic credentials.
    """
    if allow_token and _check_bearer(request, config):
        return 'token'
    return 'basic' if _check_basic(request, config) else 'basic'


def enforce_csrf(request, config, auth_kind, form_token):
    """Enforce CSRF protection for state-changing browser requests.

    Requests authenticated by API token are exempt: browsers never attach an
    ``Authorization: Bearer`` header cross-site on their own, and there is no
    CORS policy that would let script read a token and replay it. Browser
    (Basic) requests must carry the per-process CSRF token *and* originate
    same-origin, verified via the Fetch metadata header where present.
    """
    if request.method not in _UNSAFE_METHODS:
        return
    if auth_kind == 'token':
        return
    # Fetch metadata: reject anything the browser marks as not same-origin.
    fetch_site = request.headers.get('sec-fetch-site')
    if fetch_site is not None and fetch_site != 'same-origin':
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            'cross-origin request rejected')
    if not form_token or not _constant_eq(form_token, config.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'invalid or missing CSRF token')


def validate_download_url(url, config):
    """Validate a user-supplied download URL. Returns the cleaned URL or raises
    HTTPException(400). Only http/https with a hostname are accepted; an
    optional domain allowlist further restricts the host. Surrounding
    whitespace (e.g. a trailing newline from the iOS share sheet) is removed,
    as yt-dlp itself would do."""
    url = (url or '').strip()
    if not url or len(url) > 2048:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid URL')
    if any(c.isspace() for c in url) or '\\' in url or any(ord(c) < 0x20 for c in url):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid URL')
    try:
        # Raises ValueError e.g. for an unbalanced IPv6 bracket or a netloc
        # with characters that NFKC-normalise to URL delimiters.
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid URL')
    if parts.scheme not in ('http', 'https') or not hostname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            'only http(s) URLs are allowed')
    # Credentials in the authority (user:pass@host) are an SSRF/log-leak risk.
    if '@' in parts.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            'credentials in URL are not allowed')
    if config.allowed_domains:
        host = hostname.lower()
        if not any(host == d or host.endswith('.' + d) for d in config.allowed_domains):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                'domain not in WEBUI_ALLOWED_DOMAINS')
    return url


def security_headers():
    """Response headers applied to every response.

    Scripts are served as static files ('self' only), so no inline-script
    nonce is needed. Inline styles stay allowed for the template style
    attributes. The Semantic UI stylesheet on the CDN imports the Lato font
    from Google Fonts and embeds its icon fonts as data: URIs."""
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
        "https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; "
        "object-src 'none'"
    )
    return {
        'Content-Security-Policy': csp,
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'same-origin',
    }
