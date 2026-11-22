import contextlib
import importlib.resources
import json
import logging
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .backends import PrintError
from .documents import settings, validate_file
from .network import hostport
from .store import Problem

MAX_PDF = 25 * 1024**2
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Application:
    def __init__(self, store, backend, network, tmp):
        self.store = store
        self.backend = backend
        self.net = network
        self.tmp = tmp
        self.wake = threading.Event()
        self.uploads = threading.BoundedSemaphore(4)

    def worker(self, stop):
        while not stop.is_set():
            job = self.store.take()
            if job is None:
                self.wake.wait(2)
                self.wake.clear()
                continue
            try:
                opts = settings(json.loads(job["options"]))
                with tempfile.TemporaryDirectory(dir=self.tmp, prefix="job-") as folder:
                    source = Path(folder) / ("input" + Path(job["name"]).suffix.lower())
                    source.write_bytes(job["pdf"])
                    try:
                        process = subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "tailprint.render",
                                str(source),
                                json.dumps(opts),
                            ],
                            capture_output=True,
                            timeout=145,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise PrintError(
                            "Превышено время подготовки документа."
                        ) from exc
                    if process.returncode:
                        message = (
                            process.stderr.decode("utf8", "replace")
                            .strip()
                            .splitlines()
                        )
                        raise PrintError(
                            message[-1][:350]
                            if message
                            else "Ошибка подготовки документа."
                        )
                    detail = self.backend.submit(
                        job["printer"], job["copies"], Path(folder) / "sheets.pdf", opts
                    )
                state = "demo" if self.net.demo else "submitted"
            except subprocess.TimeoutExpired:
                state, detail = (
                    "uncertain",
                    "Истекло время передачи. Проверьте системную очередь перед повтором.",
                )
            except (PrintError, OSError, ValueError) as exc:
                state, detail = "failed", str(exc)
            self.store.finish(job, state, detail[:350])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, ip, port, app, admin=False):
        self.admin = admin
        self.app = app
        self.address_family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        self.slots = threading.BoundedSemaphore(32)
        super().__init__((ip, port), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Tailprint/1"
    sys_version = ""
    timeout = 35

    def log_message(self, fmt, *args):
        pass

    def output(self, code, value, mime="application/json; charset=utf-8"):
        body = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, ensure_ascii=False).encode()
        )
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorize(self):
        net = self.server.app.net
        port = self.server.server_address[1]
        names = (
            {"localhost", "127.0.0.1"}
            if self.server.admin
            else {self.server.server_address[0], *net.dns}
        )
        hosts = {hostport(n, port) for n in names}
        if port == 80:
            hosts.update(n if ":" not in n else "[" + n + "]" for n in names)
        host = self.headers.get("Host", "").lower()
        if host not in hosts or any(
            h.lower().startswith(("x-forwarded-", "tailscale-user-"))
            or h.lower() == "forwarded"
            for h in self.headers
        ):
            raise Problem(403, "Откройте прямой адрес агента.")
        if self.command == "POST" and (
            self.headers.get("Origin") != "http://" + host
            or self.headers.get("X-Tailprint") != "1"
        ):
            raise Problem(403, "Откройте форму печати на этом узле.")
        if self.server.admin:
            return "localhost"
        ip = self.client_address[0]
        if ip in net.ips:
            return "self"
        with net.lock:
            actor = net.known.get(ip)
        if not actor:
            raise Problem(
                403, "Устройство пока не найдено в Tailscale. Обновите через 20 секунд."
            )
        return actor

    def body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= limit or self.headers.get("Transfer-Encoding"):
            raise Problem(413, "Недопустимый размер запроса.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise Problem(400, "Передача файла прервана.")
        return data

    def handle_api(self):
        app = self.server.app
        net = app.net
        actor = self.authorize()
        path = urlsplit(self.path).path
        admin = self.server.admin
        if self.command == "GET":
            if path in ASSETS:
                file, mime = ASSETS[path]
                self.output(
                    200,
                    importlib.resources.files("tailprint")
                    .joinpath("web", file)
                    .read_bytes(),
                    mime,
                )
            elif path == "/api/v1/catalog":
                self.output(
                    200, {"service": "tailprint/1", "printers": app.store.published()}
                )
            elif path == "/api/network":
                with net.lock:
                    peers, error = list(net.peers.values()), net.error
                self.output(
                    200,
                    {
                        "host": net.name,
                        "admin": admin,
                        "demo": net.demo,
                        "peers": peers,
                        "printers": app.store.published(),
                        "error": error,
                        "url": "http://" + hostport(net.bound[0], net.port)
                        if net.bound
                        else "",
                    },
                )
            elif path == "/api/jobs":
                self.output(200, app.store.history(actor))
            elif path == "/api/local" and admin:
                self.output(
                    200,
                    {"queues": app.backend.list(), "published": app.store.published()},
                )
            else:
                raise Problem(404, "Страница не найдена.")
        elif path == "/api/publish" and admin:
            obj = json.loads(self.body(4096))
            if (
                not isinstance(obj, dict)
                or type(obj.get("enabled")) is not bool
                or not isinstance(obj.get("name"), str)
            ):
                raise Problem(400, "Неверные параметры публикации.")
            if obj["enabled"] and obj["name"] not in {
                p["name"] for p in app.backend.list()
            }:
                raise Problem(409, "Такой очереди больше нет на компьютере.")
            app.store.publish(obj["name"], obj["enabled"])
            self.output(200, {"ok": True})
        elif path == "/api/jobs":
            self.submit(actor)
        else:
            raise Problem(404, "Действие недоступно.")

    def submit(self, actor):
        app = self.server.app
        params = parse_qs(urlsplit(self.path).query)

        def field(key):
            values = params.get(key, [])
            if len(values) != 1 or not 0 < len(values[0]) <= 256:
                raise Problem(400, "Проверьте параметры задания.")
            return values[0]

        printer, name = field("printer"), field("name")
        key = str(uuid.UUID(field("id")))
        copies = int(field("copies"))
        if not 1 <= copies <= 20:
            raise Problem(400, "Можно выбрать от 1 до 20 копий.")
        try:
            opts = settings(json.loads(self.headers.get("X-Print-Options", "{}")))
        except ValueError as exc:
            raise Problem(400, str(exc))
        if self.headers.get_content_type() not in {
            "application/octet-stream",
            "application/pdf",
        }:
            raise Problem(415, "Выберите документ для печати.")
        if not app.uploads.acquire(blocking=False):
            raise Problem(
                429,
                "Агент занят загрузкой документов. Повторите через несколько секунд.",
            )
        try:
            pdf = self.body(MAX_PDF)
            try:
                validate_file(Path(name).suffix.lower(), pdf)
            except ValueError as exc:
                raise Problem(415, str(exc))
            app.store.enqueue(actor, key, printer, copies, name, pdf, opts)
        finally:
            app.uploads.release()
        app.wake.set()
        self.output(202, {"id": key})

    def dispatch(self):
        try:
            self.handle_api()
        except Problem as exc:
            self.output(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError):
            self.output(400, {"error": "Неверный формат запроса."})
        except (PrintError, subprocess.TimeoutExpired) as exc:
            self.output(503, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("Ошибка обработки запроса")
            with contextlib.suppress(OSError):
                self.output(500, {"error": "Ошибка агента. Проверьте его журнал."})

    do_GET = dispatch
    do_POST = dispatch
