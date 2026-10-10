import asyncio
import contextlib
import logging
import os
import sys

import aiofiles
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.templating import Jinja2Templates
from pathlib import Path

import dashboard
import jobs
import status_events
import webui_security
import ytdlp_args

LOG_TAIL_BYTES = 256 * 1024

# Routes reachable without credentials (no data, needed before login).
PUBLIC_PATHS = frozenset({'/favicon.ico', '/static/app.js'})
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


async def _log_tail_response(path, nonce=None):
    """Serve the complete lines in the last LOG_TAIL_BYTES of a log file,
    without the dashboard's markers and nonces (dashboard.scrub_log_line)."""
    try:
        text = await asyncio.to_thread(dashboard.read_log_tail, path,
                                       LOG_TAIL_BYTES, nonce)
    except OSError:
        return PlainTextResponse('error reading log', status_code=500)
    return PlainTextResponse(text, headers=NO_STORE)


async def _log_since_response(path, since, nonce=None):
    """JSON {offset, reset, more, text} for the log panel."""
    try:
        data = await asyncio.to_thread(dashboard.read_log_since, path, since,
                                       nonce)
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


def create_app():
    config = webui_security.Config()
    startup_error = config.startup_error()
    if startup_error:
        sys.stderr.write(
            f'[web-ui] refusing to start: {startup_error}. Set credentials via '
            'environment variables (WEBUI_USERNAME/WEBUI_PASSWORD and/or '
            'WEBUI_API_TOKEN).\n')
        raise SystemExit(1)

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
               'readonly': config.readonly}
        if extra:
            ctx.update(extra)
        return ctx

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

    async def save_validated(request, path, lines, validator, auth_kind, token):
        webui_security.enforce_csrf(request, config, auth_kind, token)
        require_writable()
        content = _join_form_lines(lines)
        if not config.allow_unsafe_args:
            try:
                validator(content)
            except ytdlp_args.ArgsError as err:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err))
        async with aiofiles.open(path, 'w') as f:
            await f.write(content)

    @app.get('/favicon.ico')
    async def favicon():
        return FileResponse(f'{PATHS.app_dir}/static/favicon.png')

    # A plain route instead of a StaticFiles mount: a mount resolves files
    # against root_path and 404s behind a prefix-stripping reverse proxy.
    @app.get('/static/app.js')
    async def app_js():
        return FileResponse(f'{PATHS.app_dir}/static/app.js',
                            media_type='text/javascript')

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
            context(request, {'download_id': download_id}))

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
        nonce = await asyncio.to_thread(dash.log_nonce)
        return await _log_since_response(PATHS.youtube_dl_log, since, nonce)

    @app.get('/api/log/download/{download_id}')
    async def api_download_log(download_id: str, since: int = -1,
                               _=Depends(require_auth)):
        _validate_id(download_id)
        return await _log_since_response(manager.log_path(download_id),
                                         since)

    @app.get('/log/youtube-dl', response_class=PlainTextResponse)
    async def youtube_dl_log(_=Depends(require_auth)):
        nonce = await asyncio.to_thread(dash.log_nonce)
        return await _log_tail_response(PATHS.youtube_dl_log, nonce)

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
        return templates.TemplateResponse(
            request, 'args.html',
            context(request, {'args': await _read_file(PATHS.args_file)}))

    @app.post('/edit/args/save')
    async def save_args(request: Request, auth_kind=Depends(require_auth),
                        args_new: list = Form(...), csrf_token: str = Form('')):
        await save_validated(request, PATHS.args_file, args_new,
                             ytdlp_args.validate_args_conf, auth_kind, csrf_token)
        return RedirectResponse(url=f'{BASE_PATH}/edit/args',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/edit/channels')
    async def edit_channels(request: Request, _=Depends(require_auth)):
        return templates.TemplateResponse(
            request, 'channels.html',
            context(request, {'channels': await _read_file(PATHS.channels_file)}))

    @app.post('/edit/channels/save')
    async def save_channels(request: Request, auth_kind=Depends(require_auth),
                            channels_new: list = Form(...),
                            csrf_token: str = Form('')):
        await save_validated(request, PATHS.channels_file, channels_new,
                             ytdlp_args.validate_channels, auth_kind, csrf_token)
        return RedirectResponse(url=f'{BASE_PATH}/edit/channels',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/edit/archive')
    async def edit_archive(request: Request, _=Depends(require_auth)):
        if not Path(PATHS.archive_file).exists():
            Path(PATHS.archive_file).touch()
        return templates.TemplateResponse(
            request, 'archive.html',
            context(request, {'archive': await _read_file(PATHS.archive_file)}))

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
