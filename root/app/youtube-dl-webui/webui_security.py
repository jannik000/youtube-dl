"""Authentication, CSRF protection, security headers and URL validation.

Credentials come only from environment variables, or from files named by
``<NAME>_FILE`` variables (Docker/compose secrets), and are compared with
``secrets.compare_digest``. If the web UI is enabled but no credentials are
configured, the app fails closed (see ``load_credentials`` / ``Config``).
"""

import base64
import os
import secrets
import stat
import sys
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

# Methods that change state and therefore require CSRF protection.
_UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _env(name):
    value = os.environ.get(name)
    return value if value else None


# Credentials that may also come from a file: <NAME>_FILE holds a path (e.g.
# /run/secrets/...), so the value never appears in the container's
# environment (docker inspect). Read by the web UI process itself, i.e. as
# the unprivileged user with PUID/PGID: the file must be readable by it.
SECRET_NAMES = ('WEBUI_USERNAME', 'WEBUI_PASSWORD', 'WEBUI_API_TOKEN')
SECRET_FILE_MAX_BYTES = 64 * 1024


def _read_secret_file(name, path):
    """The file's content without trailing whitespace. Raises ValueError with
    a message that names the variable and the path, never the content."""
    var = name + '_FILE'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ValueError(f'{var} points to {path}, which does not exist')
    except PermissionError:
        raise ValueError(
            f'{var} points to {path}, which is not readable by the web UI '
            f'user (uid {os.getuid()}, gid {os.getgid()}); make it readable '
            f'for that user, e.g. mode 644') from None
    except OSError as err:
        raise ValueError(f'{var}: cannot open {path} ({err.strerror})') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'{var} points to {path}, which is not a regular file')
        data = b''
        while len(data) <= SECRET_FILE_MAX_BYTES:
            chunk = os.read(fd, SECRET_FILE_MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as err:
        raise ValueError(f'{var}: cannot read {path} ({err.strerror})') from None
    finally:
        os.close(fd)
    if len(data) > SECRET_FILE_MAX_BYTES:
        raise ValueError(f'{var}: {path} is larger than '
                         f'{SECRET_FILE_MAX_BYTES // 1024} KiB')
    try:
        value = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError(f'{var}: {path} is not valid UTF-8') from None
    value = value.rstrip()   # trailing newline / whitespace from editors
    if not value:
        raise ValueError(f'{var} points to {path}, which is empty')
    return value


def _secret(name, notes, errors):
    """<NAME> from the environment, or from the file <NAME>_FILE names.
    Setting both is an error; so is a file that cannot be used (fail closed:
    the caller refuses to start)."""
    value = os.environ.get(name, '')
    path = os.environ.get(name + '_FILE', '')
    if value and path:
        errors.append(f'{name} and {name}_FILE are both set; set only one '
                      f'of them')
        return None
    if not path:
        return value or None
    try:
        value = _read_secret_file(name, path)
    except ValueError as err:
        errors.append(str(err))
        return None
    notes.append(f'{name} set from {name}_FILE')
    return value


def _parallel(value):
    value = value.strip()
    if not (value.isascii() and value.isdigit()):   # '\u00b2'.isdigit()
        return 1
    return max(1, min(3, int(value)))


class Config:
    """Resolved auth configuration, built once at startup."""

    def __init__(self):
        # Messages about the credentials' sources and problems; they name
        # variables and paths only, never a value.
        self.notes = []
        self.errors = []
        self.username = _secret('WEBUI_USERNAME', self.notes, self.errors)
        self.password = _secret('WEBUI_PASSWORD', self.notes, self.errors)
        self.api_token = _secret('WEBUI_API_TOKEN', self.notes, self.errors)
        self.readonly = os.environ.get('WEBUI_READONLY', '').lower() == 'true'
        self.allow_unsafe_args = (
            os.environ.get('WEBUI_ALLOW_UNSAFE_ARGS', '').lower() == 'true')
        allowed = _env('WEBUI_ALLOWED_DOMAINS')
        self.allowed_domains = tuple(
            d.strip().lower().lstrip('.') for d in allowed.split(',') if d.strip()
        ) if allowed else ()
        # Manual downloads running at once (1-3); anything else means 1.
        self.max_parallel = _parallel(os.environ.get(
            'WEBUI_MAX_PARALLEL_DOWNLOADS', ''))
        # New random token per process start; embedded in forms, never stored.
        self.csrf_token = secrets.token_urlsafe(32)

    @property
    def has_basic_auth(self):
        return bool(self.username and self.password)

    def startup_error(self):
        """Return a human-readable reason the UI must not start, or None."""
        if self.errors:
            return '; '.join(self.errors)
        if not (self.has_basic_auth or self.api_token):
            return ('no credentials configured: set WEBUI_USERNAME and '
                    'WEBUI_PASSWORD (or WEBUI_PASSWORD_FILE), and/or '
                    'WEBUI_API_TOKEN (or WEBUI_API_TOKEN_FILE)')
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

    The CSP allows nothing by default and only same-origin scripts, styles,
    images and fetches: the stylesheet and the script are served by the app
    itself, the templates have no inline scripts, style attributes or
    <style> blocks, and the pages use system fonts (no font-src)."""
    csp = (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )
    return {
        'Content-Security-Policy': csp,
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'same-origin',
    }


def _main(argv):
    """``check-credentials``: used by the container's init script, run as the
    web UI user, to decide before startup whether the web UI may start.
    Prints which credentials come from files and why it must not start;
    never a value. Exit status 0 = may start, 1 = must not."""
    if argv[1:] != ['check-credentials']:
        sys.stderr.write('usage: webui_security.py check-credentials\n')
        return 2
    config = Config()
    for note in config.notes:
        print(note)
    error = config.startup_error()
    if error:
        print(f'ERROR: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
