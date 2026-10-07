import asyncio
import logging
import os
import re
import sys
import uuid

import aiofiles
from fastapi import (BackgroundTasks, Depends, FastAPI, Form, HTTPException,
                     Request, status)
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.templating import Jinja2Templates
from pathlib import Path

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

    @property
    def args_file(self):
        return f'{self.config_dir}/args.conf'

    @property
    def channels_file(self):
        return f'{self.config_dir}/channels.txt'

    @property
    def archive_file(self):
        return f'{self.config_dir}/archive.txt'


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


async def _log_tail_response(path):
    """Serve at most the last LOG_TAIL_BYTES of a log file."""
    try:
        async with aiofiles.open(path, 'rb') as f:
            await f.seek(0, os.SEEK_END)
            size = await f.tell()
            await f.seek(max(0, size - LOG_TAIL_BYTES))
            data = await f.read()
        return PlainTextResponse(data.decode(errors='replace'))
    except FileNotFoundError:
        return PlainTextResponse('')
    except OSError:
        return PlainTextResponse('error reading log', status_code=500)


def _route_path(scope):
    """Request path relative to root_path, as Starlette's router sees it.
    Works both behind a prefix-stripping proxy and with the prefix kept."""
    path = scope.get('path', '')
    root = scope.get('root_path', '')
    if root and (path == root or path.startswith(root + '/')):
        path = path[len(root):] or '/'
    return path


_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _validate_id(download_id):
    # Only the canonical form produced by str(uuid.uuid4()); uuid.UUID()
    # alone would also accept '{...}', 'urn:uuid:...' and other spellings.
    if not isinstance(download_id, str) or not _UUID_RE.fullmatch(download_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'not found')


def _join_form_lines(lines):
    return ''.join(line.replace('\r\n', '\n') for line in lines)


async def download_bg(url, download_id, format_args):
    log_file_path = f'{PATHS.download_log_dir}/download_{download_id}.log'
    argv = [youtubedl_binary, '--no-playlist-reverse', '--playlist-end', '-1',
            '--config-location', PATHS.args_file, *format_args, '--', url]
    log_file = await aiofiles.open(log_file_path, 'w')
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for line in process.stdout:
            await log_file.write(line.decode(errors='replace'))
            await log_file.flush()
        await process.wait()
        await log_file.write('[youtube-dl] Download process ended\n')
    except Exception as err:
        await log_file.write(f'Error: {err}\n')
    finally:
        await log_file.close()


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

    # No interactive API docs: they would be unauthenticated extra surface.
    app = FastAPI(root_path=BASE_PATH, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.config = config

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

    def format_args_for(args_conf_text):
        if re.search(r'(--format |-f )', args_conf_text, flags=re.I | re.MULTILINE):
            return []
        return ['--format', format_default]

    async def start_download(url):
        url = webui_security.validate_download_url(url, config)
        download_id = str(uuid.uuid4())
        async with aiofiles.open(PATHS.args_file) as f:
            format_args = format_args_for(await f.read())
        return download_id, url, format_args

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
    async def dashboard(request: Request, _=Depends(require_auth)):
        return templates.TemplateResponse(request, 'dashboard.html',
                                          context(request))

    @app.post('/download')
    async def download_url(request: Request, background_tasks: BackgroundTasks,
                           auth_kind=Depends(require_download_auth),
                           url: str = Form(...), csrf_token: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        download_id, url, format_args = await start_download(url)
        background_tasks.add_task(download_bg, url, download_id, format_args)
        if auth_kind == 'token':
            # API clients cannot follow the redirect to the Basic-auth status
            # page, so answer like /api/download.
            return JSONResponse({'id': download_id})
        return RedirectResponse(url=f'{BASE_PATH}/download/{download_id}',
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.post('/api/download')
    async def api_download(request: Request, background_tasks: BackgroundTasks,
                           auth_kind=Depends(require_download_auth),
                           url: str = Form(...), csrf_token: str = Form('')):
        """JSON endpoint for the iOS shortcut: returns {"id": ...}."""
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        download_id, url, format_args = await start_download(url)
        background_tasks.add_task(download_bg, url, download_id, format_args)
        return JSONResponse({'id': download_id})

    @app.get('/download/{download_id}')
    async def download_status(request: Request, download_id: str,
                              _=Depends(require_auth)):
        _validate_id(download_id)
        return templates.TemplateResponse(
            request, 'dashboard.html',
            context(request, {'download_id': download_id}))

    @app.get('/log/youtube-dl', response_class=PlainTextResponse)
    async def youtube_dl_log(_=Depends(require_auth)):
        return await _log_tail_response(PATHS.youtube_dl_log)

    @app.get('/log/download/{download_id}', response_class=PlainTextResponse)
    async def download_log(download_id: str, _=Depends(require_auth)):
        _validate_id(download_id)
        return await _log_tail_response(
            f'{PATHS.download_log_dir}/download_{download_id}.log')

    @app.post('/restart-youtube-dl')
    async def restart_youtube_dl(request: Request, auth_kind=Depends(require_auth),
                                 csrf_token: str = Form('')):
        webui_security.enforce_csrf(request, config, auth_kind, csrf_token)
        await asyncio.create_subprocess_exec('supervisorctl', 'restart',
                                             'youtube-dl')
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
