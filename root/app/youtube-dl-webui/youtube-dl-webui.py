import asyncio
import contextlib
import logging
import os
import sys
import time

import aiofiles
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.exception_handlers import http_exception_handler
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from pathlib import Path

import channels_file
import dashboard
import jobs
import status_events
import webui_security
import ytdlp_args

LOG_TAIL_BYTES = 256 * 1024

# Routes reachable without credentials (no data, needed before login).
PUBLIC_PATHS = frozenset({'/favicon.ico', '/static/app.js', '/static/app.css'})
# The only routes that accept the API token (the iOS shortcut).
TOKEN_ROUTES = frozenset({('POST', '/download'), ('POST', '/api/download')})


class Paths:
    """Filesystem locations. Defaults are the in-container paths; tests
    override the attributes to point at a temporary directory."""

    def __init__(self):
        self.app_dir = '/app/youtube-dl-webui'
        self.config_dir = '/config'
        self.download_log_dir = '/tmp'
        self.youtube_dl_log = '/var/log/youtube-dl/youtube-dl.log'
        self.format_file = '/config.default/format'
        self.downloads_dir = '/downloads'
        self.proc_dir = '/proc'

    @property
    def args_file(self):
        return f'{self.config_dir}/args.conf'

    @property
    def channels_file(self):
        return f'{self.config_dir}/channels.txt'

    @property
    def archive_file(self):
        return f'{self.config_dir}/archive.txt'

    @property
    def cookies_file(self):
        return f'{self.config_dir}/cookies.txt'

    @property
    def status_dir(self):
        """Dashboard state, written only by the unprivileged user (0700)."""
        return f'{self.config_dir}/.youtube-dl-status'

    @property
    def events_file(self):
        return status_events.events_path(self.status_dir)


PATHS = Paths()
# No trailing slash: '/' or '/yt/' would turn links into '//edit/args'.
BASE_PATH = os.environ.get('youtubedl_webuipath', '').rstrip('/')
youtubedl_binary = 'yt-dlp'


def _read_format_default():
    try:
        with open(PATHS.format_file) as f:
            return f.read().strip()
    except OSError:
        return ''


async def _read_file(path):
    async with aiofiles.open(path, 'r') as f:
        return await f.read()


NO_STORE = {'Cache-Control': 'no-store'}


async def _log_tail_response(path, nonce=None, names=None):
    """Serve the complete lines in the last LOG_TAIL_BYTES of a log file,
    without the dashboard's markers and nonces (dashboard.scrub_log_line)."""
    try:
        text = await asyncio.to_thread(dashboard.read_log_tail, path,
                                       LOG_TAIL_BYTES, nonce, names)
    except OSError:
        return PlainTextResponse('error reading log', status_code=500)
    return PlainTextResponse(text, headers=NO_STORE)


async def _log_since_response(path, since, nonce=None, names=None):
    """JSON {offset, reset, more, text} for the log panel."""
    try:
        data = await asyncio.to_thread(dashboard.read_log_since, path, since,
                                       nonce, names=names)
    except OSError:
        return JSONResponse({'detail': 'error reading log'}, status_code=500)
    return JSONResponse(data, headers=NO_STORE)


def _route_path(scope):
    """Request path relative to root_path, as Starlette's router sees it.
    Works both behind a prefix-stripping proxy and with the prefix kept."""
    path = scope.get('path', '')
    root = scope.get('root_path', '')
    if root and (path == root or path.startswith(root + '/')):
        path = path[len(root):] or '/'
    return path


def _validate_id(download_id):
    # Only the canonical form produced by str(uuid.uuid4()); uuid.UUID()
    # alone would also accept '{...}', 'urn:uuid:...' and other spellings.
    if not isinstance(download_id, str) or not jobs.UUID_RE.match(download_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'not found')


def _truthy(value):
    """A form flag: a checkbox sends 'on', the API '1' or 'true'."""
    return (value or '').strip().lower() in ('1', 'true', 'on', 'yes')


def _join_form_lines(lines):
    return ''.join(line.replace('\r\n', '\n') for line in lines)


def _wants_html(request):
    """True for a browser navigation (a form without JavaScript, a typed
    URL): it gets an HTML page instead of JSON. The script and API clients
    send Accept: application/json (or */*)."""
    path = _route_path(request.scope)
    if path.startswith(('/api/', '/log/', '/static/')):
        return False
    return 'text/html' in request.headers.get('accept', '').lower()


# The three configuration editors (templates/edit.html).
EDIT_PAGES = {
    'args': {'heading': 'Args', 'filename': '/config/args.conf',
             'field': 'args_new', 'action': '/edit/args/save',
             'hint': 'yt-dlp options for every scheduled run, one or more '
                     'per line. Options that run commands or write outside '
                     '/downloads and /config are refused.'},
    'channels': {'heading': 'Channels', 'filename': '/config/channels.txt',
                 'field': 'channels_new', 'action': '/edit/channels/save',
                 'hint': 'One URL per line; "URL | options" adds yt-dlp '
                         'options for that entry. Lines starting with # '
                         'are comments. Saving restarts the scheduled pass.',
                 'back_href': '/edit/channels',
                 'back_text': 'Back to channels'},
    'archive': {'heading': 'Archive', 'filename': '/config/archive.txt',
                'field': 'archive_new', 'action': '/edit/archive/save',
                'hint': 'Videos listed here ("extractor id" per line) are '
                        'not downloaded again.'},
}



def _ago(ts, now):
    """'today', 'yesterday', '5 days ago' or '' for a Unix time."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ''
    days = int(max(0, now - ts) // 86400)
    return {0: 'today', 1: 'yesterday'}.get(days, f'{days} days ago')


# Channel status (dashboard._channels) -> (chip tone, text).
CHANNEL_STATUS = {
    'ok': ('tone-ok', 'OK'), 'stale': ('tone-warn', 'No new videos'),
    'failed': ('tone-err', 'Failed'), 'error': ('tone-err', 'Error'),
}

_ERROR_TITLES = {
    400: 'Bad request', 403: 'Not allowed', 404: 'Not found',
    409: 'Please confirm', 429: 'Queue is full',
    503: 'Not available right now',
}


def create_app():
    config = webui_security.Config()
    startup_error = config.startup_error()
    if startup_error:
        sys.stderr.write(
            f'[web-ui] refusing to start: {startup_error}. Set credentials via '
            'environment variables (WEBUI_USERNAME/WEBUI_PASSWORD and/or '
            'WEBUI_API_TOKEN) or their *_FILE variants.\n')
        raise SystemExit(1)
    for note in config.notes:   # names only, never a value
        sys.stderr.write(f'[web-ui] {note}\n')

    templates = Jinja2Templates(directory=f'{PATHS.app_dir}/templates')
    format_default = _read_format_default()

    # The dashboard's state and the download queue live in this process:
    # uvicorn must run with a single worker (as supervisor starts it).
    dash = dashboard.Dashboard(
        status_dir=PATHS.status_dir, pass_log=PATHS.youtube_dl_log,
        downloads_dir=PATHS.downloads_dir, cookies_file=PATHS.cookies_file,
        proc_dir=PATHS.proc_dir,
        interval=os.environ.get('youtubedl_interval', ''))
    manager = jobs.JobManager(
        status_dir=PATHS.status_dir, args_file=PATHS.args_file,
        cookies_file=PATHS.cookies_file, archive_file=PATHS.archive_file,
        format_default=format_default, binary=youtubedl_binary,
        parallel=config.max_parallel, legacy_log_dir=PATHS.download_log_dir)
    manager.title_for = dash.known_title

    def validate_url(url):
        return webui_security.validate_download_url(url, config)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Resume the queue of the previous process; stop running downloads
        # on the way out (they become 'interrupted' and can be retried).
        await manager.start(validate_url)
        try:
            yield
        finally:
            await manager.shutdown()

    # No interactive API docs: they would be unauthenticated extra surface.
    app = FastAPI(root_path=BASE_PATH, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.dashboard = dash
    app.state.jobs = manager

    @app.middleware('http')
    async def auth_gate_and_headers(request, call_next):
        # Authenticate from the headers *before* any route runs, so request
        # bodies of unauthenticated clients are never read or spooled. The
        # route dependencies authenticate again (defense in depth) and decide
        # CSRF handling.
        path = _route_path(request.scope)
        response = None
        if path not in PUBLIC_PATHS:
            allow_token = (request.method, path) in TOKEN_ROUTES
            try:
                webui_security.authenticate(request, config, allow_token=allow_token)
            except HTTPException as exc:
                response = JSONResponse({'detail': exc.detail},
                                        status_code=exc.status_code,
                                        headers=exc.headers)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                # Answer unexpected errors here, so they also carry the
                # security headers; the traceback goes to the web UI log.
                logging.getLogger('uvicorn.error').exception(
                    'unhandled error in %s %s', request.method, path)
                response = PlainTextResponse('Internal Server Error',
                                             status_code=500)
        for header, value in webui_security.security_headers().items():
            response.headers.setdefault(header, value)
        return response

    def require_auth(request: Request):
        """Interactive endpoints: Basic credentials only (no API token)."""
        return webui_security.authenticate(request, config, allow_token=False)

    def require_download_auth(request: Request):
        """Download endpoints: Basic credentials or API token."""
        return webui_security.authenticate(request, config, allow_token=True)

    def context(request, extra=None):
        ctx = {'base_path': BASE_PATH, 'csrf_token': config.csrf_token,
               'readonly': config.readonly, 'page': 'dashboard',
               'download_id': '', 'parallel': config.max_parallel,
               'max_queued': manager.max_queued}
        if extra:
            ctx.update(extra)
        return ctx

    def error_page(request, status_code, message, headers=None, **extra):
        """An HTML error answer for a browser navigation."""
        tone = 'tone-warn' if status_code in (409, 429, 503) else 'tone-err'
        return templates.TemplateResponse(
            request, 'error.html',
            context(request, {
                'page': 'error', 'status_code': status_code,
                'title': _ERROR_TITLES.get(status_code, 'Something went wrong'),
                'message': message, 'tone': tone, **extra}),
            status_code=status_code, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def html_or_json_error(request, exc):
        # API clients and the script keep FastAPI's JSON answer.
        if not _wants_html(request) or exc.status_code == 401:
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, str) else ''
        return error_page(request, exc.status_code, detail,
                          headers=getattr(exc, 'headers', None))

    def edit_page(request, name, content, error='', status_code=200):
        return templates.TemplateResponse(
            request, 'edit.html',
            context(request, dict(EDIT_PAGES[name], page='edit',
                                  content=content, error=error)),
            status_code=status_code)

    def job_answer(ticket):
        """JSON of a job ticket: {id, state, position, message}."""
        return JSONResponse(ticket, headers=NO_STORE)

    def job_error(err):
        code = {jobs.QueueFull: status.HTTP_429_TOO_MANY_REQUESTS,
                jobs.UnknownJob: status.HTTP_404_NOT_FOUND,
                jobs.JobActive: status.HTTP_409_CONFLICT,
                jobs.NotActive: status.HTTP_409_CONFLICT,
                jobs.ShuttingDown: status.HTTP_503_SERVICE_UNAVAILABLE,
                }.get(type(err), status.HTTP_400_BAD_REQUEST)
        headers = {'Retry-After': '60'} if isinstance(
            err, (jobs.QueueFull, jobs.ShuttingDown)) else None
        return JSONResponse({'detail': str(err), 'message': str(err)},
                            status_code=code, headers=headers)

    async def queue_download(url, auth_kind, force):
        url = validate_url(url)
        return await manager.submit(url, by=auth_kind, force=_truthy(force))

    def require_writable():
        if config.readonly:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                'configuration editing is disabled (WEBUI_READONLY)')

    async def save_validated(request, name, path, lines, validator,
                             auth_kind, token):
        """Save a configuration file; None on success, else the editor
        again with the refused content and the validation error (400)."""
        webui_security.enforce_csrf(request, config, auth_kind, token)
        require_writable()
        content = _join_form_lines(lines)
        if not config.allow_unsafe_args:
            try:
                validator(content)
            except ytdlp_args.ArgsError as err:
                return edit_page(request, name, content, error=str(err),
                                 status_code=status.HTTP_400_BAD_REQUEST)
        async with aiofiles.open(path, 'w') as f:
            await f.write(content)
        return None

    @app.get('/favicon.ico')
    async def favicon():
        return FileResponse(f'{PATHS.app_dir}/static/favicon.png')

    # A plain route instead of a StaticFiles mount: a mount resolves files
    # against root_path and 404s behind a prefix-stripping reverse proxy.
    @app.get('/static/app.js')
    async def app_js():
        return FileResponse(f'{PATHS.app_dir}/static/app.js',
                            media_type='text/javascript')

    @app.get('/static/app.css')
    async def app_css():
        return FileResponse(f'{PATHS.app_dir}/static/app.css',
                            media_type='text/css')

    @app.get('/')
    async def dashboard_page(request: Request, _=Depends(require_auth)):
        return templates.TemplateResponse(request, 'dashboard.html',
                                          context(request))

    @app.post('/download')
    async def download_url(request: Request,
                           auth_kind=Depends(require_download_auth),
                           url: str = Form(...), csrf_token: str = Form(''),
                           force: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        try:
            ticket = await queue_download(url, auth_kind, force)
        except jobs.JobError as err:
            if auth_kind != 'token' and _wants_html(request):
                response = job_error(err)
                return error_page(request, response.status_code, str(err),
                                  headers={'Retry-After': '60'} if
                                  'retry-after' in response.headers else None)
            return job_error(err)
        if auth_kind == 'token':
            # API clients cannot follow the redirect to the Basic-auth status
            # page, so answer like /api/download.
            return job_answer(ticket)
        return RedirectResponse(url=f'{BASE_PATH}/download/{ticket["id"]}',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.post('/api/download')
    async def api_download(request: Request,
                           auth_kind=Depends(require_download_auth),
                           url: str = Form(...), csrf_token: str = Form(''),
                           force: str = Form('')):
        """JSON endpoint for the iOS shortcut: {id, state, position,
        message}; 429 with Retry-After when the queue is full."""
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        try:
            return job_answer(await queue_download(url, auth_kind, force))
        except jobs.JobError as err:
            return job_error(err)

    @app.post('/api/download/{download_id}/cancel')
    async def cancel_download(request: Request, download_id: str,
                              auth_kind=Depends(require_auth),
                              csrf_token: str = Form('')):
        """Cancel a queued job, or stop a running one (202: SIGTERM sent,
        SIGKILL follows if needed)."""
        _validate_id(download_id)
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        try:
            ticket = await manager.cancel(download_id)
        except jobs.JobError as err:
            return job_error(err)
        response = job_answer(ticket)
        if ticket['state'] == 'running':
            response.status_code = status.HTTP_202_ACCEPTED
        return response

    @app.post('/api/download/{download_id}/retry')
    async def retry_download(request: Request, download_id: str,
                             auth_kind=Depends(require_auth),
                             csrf_token: str = Form(''),
                             force: str = Form('')):
        """Queue a finished job's URL again (validated again); force=1 for
        'Re-download'."""
        _validate_id(download_id)
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        try:
            return job_answer(await manager.retry(
                download_id, validate_url,
                force=True if _truthy(force) else None))
        except jobs.JobError as err:
            return job_error(err)

    @app.post('/api/download-now')
    async def download_now(request: Request, auth_kind=Depends(require_auth),
                           csrf_token: str = Form(''),
                           video_id: str = Form(''), force: str = Form('')):
        """'Download now' of a failed video: a YouTube id from the problems
        list becomes a watch URL, validated like any other URL."""
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        try:
            url = jobs.watch_url(video_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                'invalid YouTube video id')
        try:
            return job_answer(await queue_download(url, auth_kind, force))
        except jobs.JobError as err:
            return job_error(err)

    @app.get('/download/{download_id}')
    async def download_status(request: Request, download_id: str,
                              _=Depends(require_auth)):
        _validate_id(download_id)
        return templates.TemplateResponse(
            request, 'dashboard.html',
            context(request, {'download_id': download_id, 'page': 'job'}))

    @app.get('/api/status')
    async def api_status(request: Request, _=Depends(require_auth)):
        """Dashboard status (Basic auth only). Answers 304 when the client's
        If-None-Match has the current ETag; nothing is cached."""
        snapshot = manager.snapshot()   # in the event loop, like the queue
        etag, body = await asyncio.to_thread(dash.status_json, snapshot)
        headers = {'ETag': etag, **NO_STORE}
        if dashboard.etag_matches(request.headers.get('if-none-match'), etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                            headers=headers)
        return Response(body, media_type='application/json', headers=headers)

    @app.get('/api/log/youtube-dl')
    async def api_youtube_dl_log(since: int = -1, _=Depends(require_auth)):
        nonce, names = await asyncio.to_thread(dash.log_context)
        return await _log_since_response(PATHS.youtube_dl_log, since, nonce,
                                         names)

    @app.get('/api/log/download/{download_id}')
    async def api_download_log(download_id: str, since: int = -1,
                               _=Depends(require_auth)):
        _validate_id(download_id)
        return await _log_since_response(manager.log_path(download_id),
                                         since)

    @app.get('/log/youtube-dl', response_class=PlainTextResponse)
    async def youtube_dl_log(_=Depends(require_auth)):
        nonce, names = await asyncio.to_thread(dash.log_context)
        return await _log_tail_response(PATHS.youtube_dl_log, nonce, names)

    @app.get('/log/download/{download_id}', response_class=PlainTextResponse)
    async def download_log(download_id: str, _=Depends(require_auth)):
        _validate_id(download_id)
        return await _log_tail_response(manager.log_path(download_id))

    @app.post('/restart-youtube-dl')
    async def restart_youtube_dl(request: Request, auth_kind=Depends(require_auth),
                                 csrf_token: str = Form(''),
                                 confirm: str = Form('')):
        """'Run now': restart the downloader. While a pass runs, that
        aborts it, so the client must confirm (409 without confirm=1)."""
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        if not _truthy(confirm) and await asyncio.to_thread(dash.pass_running):
            if _wants_html(request):
                return error_page(
                    request, status.HTTP_409_CONFLICT,
                    'A scheduled run is in progress. Abort it and start '
                    'over? Videos that are already finished stay in the '
                    'archive.', confirm_restart=True)
            return JSONResponse(
                {'detail': 'A scheduled run is in progress. Send confirm=1 '
                           'to abort it and start over.'},
                status_code=status.HTTP_409_CONFLICT)
        await asyncio.create_subprocess_exec('supervisorctl', 'restart',
                                             'youtube-dl',
                                             stdin=asyncio.subprocess.DEVNULL)
        return RedirectResponse(url=f'{BASE_PATH}/',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/edit/args')
    async def edit_args(request: Request, _=Depends(require_auth)):
        return edit_page(request, 'args', await _read_file(PATHS.args_file))

    @app.post('/edit/args/save')
    async def save_args(request: Request, auth_kind=Depends(require_auth),
                        args_new: list = Form(...), csrf_token: str = Form('')):
        refused = await save_validated(
            request, 'args', PATHS.args_file, args_new,
            ytdlp_args.validate_args_conf, auth_kind, csrf_token)
        if refused is not None:
            return refused
        return RedirectResponse(url=f'{BASE_PATH}/edit/args',
                                status_code=status.HTTP_303_SEE_OTHER)

    # The channel manager: channels.txt as a list (add, remove, undo).
    # Changes apply from the next pass; the running one is left alone.
    channels_lock = asyncio.Lock()
    removed = {}   # the last removed line, for Undo: {'line', 'pos'}

    async def read_channels():
        try:
            return await _read_file(PATHS.channels_file)
        except FileNotFoundError:
            return ''

    async def write_channels(text):
        async with aiofiles.open(PATHS.channels_file, 'w') as f:
            await f.write(text)

    async def channels_page(request, error='', form=None, notice='',
                            status_code=200):
        text = await read_channels()
        info = await asyncio.to_thread(dash.channel_info)
        now = time.time()
        rows = []
        for entry in channels_file.entries(text):
            seen = info.get(entry['url']) or {}
            tone, label = CHANNEL_STATUS.get(seen.get('status'),
                                             ('tone-idle', 'Not checked yet'))
            rows.append(dict(
                entry, name=seen.get('name') or '',
                short=dashboard.short_url(entry['url']) or entry['url'],
                link=(entry['url'] if entry['url'].lower().startswith(
                    ('http://', 'https://')) else ''),
                tone=tone, status=label,
                last_new=_ago(seen.get('last_new'), now),
                checked=bool(seen.get('last_checked'))))
        return templates.TemplateResponse(
            request, 'channels.html',
            context(request, {
                'page': 'channels', 'channels': rows,
                'comments': channels_file.comment_lines(text),
                'error': error, 'form': form or {}, 'notice': notice,
                'removed_line': removed.get('line', '')}),
            status_code=status_code)

    def channels_saved(query=''):
        return RedirectResponse(url=f'{BASE_PATH}/edit/channels{query}',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/edit/channels')
    async def edit_channels(request: Request, _=Depends(require_auth)):
        notice = ''
        if request.query_params.get('removed') and removed:
            notice = 'removed'
        elif request.query_params.get('added'):
            notice = 'added'
        return await channels_page(request, notice=notice)

    @app.get('/edit/channels/text')
    async def edit_channels_text(request: Request, _=Depends(require_auth)):
        return edit_page(request, 'channels', await read_channels())

    @app.post('/edit/channels/add')
    async def add_channel(request: Request, auth_kind=Depends(require_auth),
                          url: str = Form(''), options: str = Form(''),
                          csrf_token: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        require_writable()
        async with channels_lock:
            text = await read_channels()
            try:
                clean_url = channels_file.normalise_url(url)
                try:
                    webui_security.validate_download_url(clean_url, config)
                except HTTPException as err:
                    raise channels_file.ChannelError(
                        'This domain is not in WEBUI_ALLOWED_DOMAINS.'
                        if err.status_code == 403 else
                        'Only plain http and https URLs can be added.')
                new_text = channels_file.add(text, clean_url, options)
                if not config.allow_unsafe_args:
                    try:
                        ytdlp_args.validate_channels(new_text)
                    except ytdlp_args.ArgsError as err:
                        raise channels_file.ChannelError(
                            f'Options refused: {err}'.replace(
                                f'line {len(new_text.splitlines())}: ', ''))
            except channels_file.ChannelError as err:
                return await channels_page(
                    request, error=str(err),
                    form={'url': url, 'options': options},
                    status_code=status.HTTP_400_BAD_REQUEST)
            await write_channels(new_text)
            removed.clear()
        return channels_saved('?added=1')

    @app.post('/edit/channels/remove')
    async def remove_channel(request: Request,
                             auth_kind=Depends(require_auth),
                             line: str = Form(''), csrf_token: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        require_writable()
        async with channels_lock:
            text = await read_channels()
            try:
                new_text, pos = channels_file.remove(text, line.strip())
            except channels_file.ChannelError as err:
                return await channels_page(
                    request, error=str(err),
                    status_code=status.HTTP_409_CONFLICT)
            await write_channels(new_text)
            removed.clear()
            removed.update(line=line.strip(), pos=pos)
        return channels_saved('?removed=1')

    @app.post('/edit/channels/undo')
    async def undo_remove_channel(request: Request,
                                  auth_kind=Depends(require_auth),
                                  csrf_token: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        require_writable()
        async with channels_lock:
            if removed:
                text = await read_channels()
                await write_channels(channels_file.restore(
                    text, removed['line'], removed['pos']))
                removed.clear()
        return channels_saved()

    @app.post('/edit/channels/save')
    async def save_channels(request: Request, auth_kind=Depends(require_auth),
                            channels_new: list = Form(...),
                            csrf_token: str = Form('')):
        answer = await save_validated(
            request, 'channels', PATHS.channels_file, channels_new,
            ytdlp_args.validate_channels, auth_kind, csrf_token)
        if answer is not None:
            if answer.status_code == status.HTTP_303_SEE_OTHER:
                removed.clear()   # Undo would put the line into new text
            return answer
        return RedirectResponse(url=f'{BASE_PATH}/edit/channels',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/edit/archive')
    async def edit_archive(request: Request, _=Depends(require_auth)):
        if not Path(PATHS.archive_file).exists():
            Path(PATHS.archive_file).touch()
        return edit_page(request, 'archive',
                         await _read_file(PATHS.archive_file))

    @app.post('/edit/archive/save')
    async def save_archive(request: Request, auth_kind=Depends(require_auth),
                           archive_new: list = Form(...),
                           csrf_token: str = Form('')):
        # archive.txt holds only video IDs, never interpreted as arguments.
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        require_writable()
        async with aiofiles.open(PATHS.archive_file, 'w') as f:
            await f.write(_join_form_lines(archive_new))
        return RedirectResponse(url=f'{BASE_PATH}/edit/archive',
                                status_code=status.HTTP_303_SEE_OTHER)

    return app


# Entry point used by supervisor (uvicorn ... youtube-dl-webui:webserver).
# Created lazily on first attribute access so importing the module (e.g. in
# tests) does not build the app with production paths. uvicorn accesses
# ``webserver`` once at startup, where a missing-credentials misconfiguration
# then fails the process immediately rather than serving an unauthenticated UI.
_webserver = None


def __getattr__(name):
    global _webserver
    if name == 'webserver':
        if _webserver is None:
            _webserver = create_app()
        return _webserver
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
