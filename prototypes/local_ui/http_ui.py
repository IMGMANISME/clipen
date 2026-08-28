"""Loopback-only, authenticated prototype UI; no HTTP agent or file APIs."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import threading
import time
import webbrowser

from .store import Rejected

MAX_BODY = 32768
STATIC = Path(__file__).parent / 'static'


def decode_json(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate_key')
            obj[key] = value
        return obj

    def invalid(value):
        raise ValueError('non_finite_number')

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('non_finite_number')
        return number

    def unicode_valid(value):
        if isinstance(value, str):
            value.encode('utf-8')
        elif isinstance(value, dict):
            for key, item in value.items():
                unicode_valid(key)
                unicode_valid(item)
        elif isinstance(value, list):
            for item in value:
                unicode_valid(item)

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)
    unicode_valid(value)
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, runtime):
        self.runtime = runtime
        self.slots = threading.BoundedSemaphore(16)
        self.security_lock = threading.Lock()
        self.bootstrap = secrets.token_urlsafe(32)
        # The terminal link can reopen this instance after the one-time browser
        # bootstrap has been consumed. Never return it in API bodies or logs.
        self.entry = secrets.token_urlsafe(32)
        self.closing = False
        self.expires = time.monotonic() + 60
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.cookie_name = 'clipen_' + secrets.token_hex(8)
        super().__init__(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.host = f'127.0.0.1:{self.server_port}'
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def start(self, open_browser=True, opener=None):
        self.thread.start()
        if open_browser:
            return (opener or webbrowser.open)(self.bootstrap_url(), new=2)
        return True

    def bootstrap_url(self):
        return self.origin + '/#' + self.bootstrap

    def reopen_url(self):
        return self.origin + '/#' + self.entry

    def close(self):
        with self.security_lock:
            self.closing = True
            self.bootstrap = self.entry = self.session = self.csrf = ''
        if self.thread.is_alive():
            self.shutdown()
        self.server_close()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ClipenPrototype'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Never log cookies, bootstrap secrets, text, or paths.

    def check_site(self, mutation=False):
        if self.headers.get_all('Host') != [self.server.host]:
            raise Rejected('invalid_host', 403)
        origins = self.headers.get_all('Origin') or []
        if (origins and origins != [self.server.origin]) or (mutation and not origins):
            raise Rejected('invalid_origin', 403)
        if self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
            raise Rejected('cross_site_request', 403)

    def authenticate(self, mutation=False):
        if self.server.closing or not self.server.session or self.server.runtime.stop.is_set():
            raise Rejected('service_stopped', 503)
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie[self.server.cookie_name].value
        except (KeyError, ValueError):
            raise Rejected('authentication_required', 401) from None
        if not secrets.compare_digest(value.encode('utf-8'), self.server.session.encode()):
            raise Rejected('authentication_required', 401)
        if mutation:
            supplied = self.headers.get_all('X-Clipen-CSRF') or []
            if len(supplied) != 1 or not secrets.compare_digest(supplied[0].encode('utf-8'), self.server.csrf.encode()):
                raise Rejected('invalid_csrf', 403)

    def body(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length') or []) != 1:
            raise Rejected('content_length_required', 400)
        if self.headers.get('Content-Type') != 'application/json':
            raise Rejected('json_required', 415)
        try:
            length = int(self.headers['Content-Length'])
        except ValueError:
            raise Rejected('invalid_content_length', 400) from None
        if length < 0 or length > MAX_BODY:
            raise Rejected('body_too_large', 413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise Rejected('incomplete_body', 400)
        try:
            return decode_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Rejected('invalid_json', 400) from None

    def respond(self, status, content, content_type='application/json; charset=utf-8', cookie=None, etag=None):
        raw = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if status != 304:
            self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('Connection', 'close')
        if etag:
            self.send_header('ETag', etag)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, mutation):
        try:
            self.check_site(mutation)
            if self.server.closing or self.server.runtime.stop.is_set():
                raise Rejected('service_stopped', 503)
            if not mutation:
                static = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                          '/ui-state.js': ('ui-state.js', 'text/javascript'),
                          '/app.css': ('app.css', 'text/css')}
                if self.path in static:
                    name, mime = static[self.path]
                    self.respond(200, (STATIC / name).read_bytes(), mime + '; charset=utf-8')
                elif self.path == '/health':
                    self.respond(200, {'service': 'clipen-prototype'})
                elif self.path == '/api/state':
                    self.authenticate()
                    etag, raw = self.server.runtime.encoded_state(self.server.csrf)
                    if self.headers.get_all('If-None-Match') == [etag]:
                        self.respond(304, b'', etag=etag)
                    else:
                        self.respond(200, raw, etag=etag)
                else:
                    raise Rejected('not_found', 404)
            elif self.path == '/api/session':
                body = self.body()
                token = body.get('token')
                with self.server.security_lock:
                    # Use bytes so malformed non-ASCII credentials fail closed
                    # rather than raising TypeError from compare_digest(str).
                    supplied = token.encode('utf-8') if isinstance(token, str) else b''
                    reopen = bool(self.server.entry) and secrets.compare_digest(supplied, self.server.entry.encode())
                    bootstrap = (bool(self.server.bootstrap) and time.monotonic() <= self.server.expires
                                 and secrets.compare_digest(supplied, self.server.bootstrap.encode()))
                    if self.server.closing or self.server.runtime.stop.is_set():
                        raise Rejected('service_stopped', 503)
                    if not (reopen or bootstrap):
                        raise Rejected('bootstrap_expired_or_used', 401)
                    if bootstrap:
                        self.server.bootstrap = ''
                cookie = f'{self.server.cookie_name}={self.server.session}; HttpOnly; SameSite=Strict; Path=/'
                self.respond(200, {'status': 'authenticated'}, cookie=cookie)
            else:
                self.authenticate(mutation=True)
                self.respond(202, self.server.runtime.command(self.path, self.body()))
        except Rejected as error:
            self.respond(error.status, {'error': error.code})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.respond(500, {'error': 'internal_error'})
