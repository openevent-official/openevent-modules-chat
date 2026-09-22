"""Same-origin browser HTTP transport for ChatService."""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from importlib.resources import files
import json
import mimetypes
import re
import ssl
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit

from multipart import MultipartError, MultipartSegment, PushMultipartParser, parse_options_header

from .config import strict_json
from .service import AppError, bad_request


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, *, origin=None):
        self.service = service
        self.origin = origin.rstrip("/") if origin else None
        self._serving = threading.Event()
        super().__init__(address, ChatRequestHandler)
        service.on_fatal = self.request_stop

    def request_stop(self):
        if self._serving.is_set():
            threading.Thread(target=self.shutdown, daemon=True).start()

    def serve_forever(self, poll_interval=0.1):
        self._serving.set()
        try:
            if not self.service.fatal.is_set():
                super().serve_forever(poll_interval)
        finally:
            self._serving.clear()


class ChatRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenEventChat"

    def log_message(self, format, *args):
        # HTTP request targets and headers are not part of the diagnostic log.
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": {"code": "invalid_request", "message": "HTTP request is invalid"}})

    def _handle(self):
        try:
            self._dispatch()
        except AppError as exc:
            self.close_connection = True
            self._json(exc.status, exc.as_json())
        except (ConnectionError, BrokenPipeError, TimeoutError):
            return
        except (ValueError, UnicodeError):
            self.close_connection = True
            self._json(400, bad_request().as_json())
        except Exception as exc:
            from openevent.chat_sdk.errors import make_failure
            error = self.server.service._fail(make_failure("HTTP", exc, detail="unexpected HTTP processing failure"))
            self._json(error.status, error.as_json())

    def _bytes(self, status, content, content_type, *, headers=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status, value):
        content = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._bytes(status, content, "application/json; charset=utf-8")

    def _static(self, name):
        if "/" in name or "\\" in name or name.startswith("."):
            raise AppError(404, "not_found", "resource does not exist")
        resource = files("openevent.chat_app").joinpath("static", name)
        if not resource.is_file():
            raise AppError(404, "not_found", "resource does not exist")
        mime = "text/javascript" if name.endswith((".js", ".mjs")) else mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._bytes(200, resource.read_bytes(), mime)

    def _authenticate(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            raw = cookie["web_token"].value
            if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError("invalid cookie encoding")
            value = unquote(raw, encoding="utf-8", errors="strict")
        except (KeyError, ValueError, UnicodeError):
            raise AppError(401, "unauthorized", "access token is required") from None
        if not hmac.compare_digest(value.encode("utf-8"), self.server.service.config.web_token.encode("utf-8")):
            raise AppError(401, "unauthorized", "access token is invalid")

    def _origin(self):
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        expected = self.server.origin or scheme + "://" + self.headers.get("Host", "")
        if self.headers.get("Origin") != expected:
            raise AppError(403, "invalid_origin", "request origin is not allowed")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise bad_request("transfer encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise AppError(411, "content_length_required", "a content length is required")
        length = int(lengths[0])
        content = self.rfile.read(length)
        if len(content) != length:
            raise bad_request("incomplete request body")
        return content

    def _json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise AppError(415, "unsupported_media_type", "JSON request required")
        try:
            return strict_json(self._body().decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError):
            raise bad_request("invalid JSON request") from None

    def _upload(self, session_id):
        content_type, options = parse_options_header(self.headers.get("Content-Type", ""))
        if content_type != "multipart/form-data":
            raise AppError(415, "unsupported_media_type", "multipart file required")
        boundary = options.get("boundary")
        if not boundary:
            raise bad_request("multipart boundary is required")
        body = self._body()
        part, chunks = None, []
        try:
            with PushMultipartParser(boundary, content_length=len(body), max_segment_count=1) as parser:
                for event in parser.parse(body):
                    if isinstance(event, MultipartSegment):
                        if event.disposition != "form-data" or event.name != "file" or event.filename is None:
                            raise bad_request("one file part is required")
                        part = event
                    elif isinstance(event, bytes):
                        chunks.append(event)
        except MultipartError:
            raise bad_request("invalid multipart body") from None
        if part is None:
            raise bad_request("one file part is required")
        raw_type = part.header("Content-Type", "").split(";", 1)[0].strip()
        file_type = raw_type if re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", raw_type) else "application/octet-stream"
        result = self.server.service.upload(session_id, name=part.filename, content_type=file_type,
                                            data=b"".join(chunks))
        self._json(201, result)

    @staticmethod
    def _query(query, required, optional=()):
        try:
            data = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise bad_request("invalid query") from None
        if not set(required) <= set(data) or set(data) - set(required) - set(optional) or any(len(v) != 1 for v in data.values()):
            raise bad_request("query fields are invalid")
        return {key: value[0] for key, value in data.items()}

    def _dispatch(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.command in ("GET", "HEAD") and path == "/":
            self._bytes(302, b"", "text/plain", headers={"Location": "/api/chat/"})
            return
        if self.command in ("GET", "HEAD") and path == "/api/chat/":
            self._static("index.html")
            return
        if self.command in ("GET", "HEAD") and path.startswith("/static/"):
            self._static(unquote(path[len("/static/"):]))
            return
        if not path.startswith("/api/chat/"):
            raise AppError(404, "not_found", "resource does not exist")
        self._authenticate()
        service = self.server.service
        service.ensure_running()
        if self.command == "POST":
            self._origin()
        parts = path[len("/api/chat/"):].split("/")
        if parts == ["sessions"]:
            if parsed.query:
                raise bad_request("query is not supported")
            if self.command == "GET":
                self._json(200, service.list_sessions())
                return
            if self.command == "POST":
                status, result = service.create_session(self._json_body())
                self._json(status, result)
                return
        if len(parts) < 3 or parts[0] != "sessions":
            raise AppError(404, "not_found", "resource does not exist")
        sid, operation = parts[1:3]
        if self.command == "GET" and operation == "history" and len(parts) == 3:
            query = self._query(parsed.query, {"fetch_seq"}, {"limit"})
            limit_text = query.get("limit", "100")
            if not re.fullmatch(r"[1-9][0-9]*", limit_text):
                raise bad_request("invalid history limit")
            self._json(200, service.history(sid, query["fetch_seq"], int(limit_text)))
            return
        if self.command == "GET" and operation == "submissions" and len(parts) == 4:
            if parsed.query:
                raise bad_request("query is not supported")
            status, result = service.submission(sid, parts[3])
            self._json(status, result)
            return
        if self.command == "GET" and operation == "attachments" and (len(parts) == 4 or len(parts) == 5 and parts[4] == "metadata"):
            query = self._query(parsed.query, {"event_seq"})
            if len(parts) == 5:
                self._json(200, service.metadata(sid, parts[3], query["event_seq"]))
            else:
                metadata, data = service.download(sid, parts[3], query["event_seq"])
                disposition = "attachment; filename*=UTF-8''" + quote(metadata["name"], safe="")
                self._bytes(200, data, "application/octet-stream", headers={"Content-Disposition": disposition})
            return
        if self.command == "POST" and len(parts) == 3:
            if parsed.query:
                raise bad_request("query is not supported")
            if operation == "attachments":
                self._upload(sid)
                return
            method = {"submissions": service.allocate, "turns": service.send, "cancellations": service.cancel}.get(operation)
            if method is not None:
                self._json(201, method(sid, self._json_body()))
                return
        raise AppError(404, "not_found", "resource does not exist")
