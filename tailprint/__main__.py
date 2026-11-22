import argparse
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .backends import Backend, DemoBackend, PrintError
from .network import Network, hostport, read_status
from .server import Application, Server
from .store import Store


@contextmanager
def instance_lock(directory):
    file = (directory / "agent.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if not file.tell():
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise SystemExit("Другой агент уже использует этот каталог данных.")
    try:
        yield
    finally:
        file.close()


def main():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if (
                stream is not None
                and not stream.isatty()
                and hasattr(stream, "reconfigure")
            ):
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="TailPrint · печать документов в своей сети Tailscale"
    )
    parser.add_argument(
        "--port", type=int, default=8787, help="порт на всех узлах (8787)"
    )
    parser.add_argument("--data-dir", type=Path, help="каталог локальной очереди")
    parser.add_argument("--sumatra", help="путь к SumatraPDF.exe на Windows")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="локальный пример без Tailscale и физической печати",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Укажите порт в диапазоне 1–65535.")
    os.umask(0o077)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with ExitStack() as stack:
        if args.demo:
            directory = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="tailprint-demo-")
                )
            )
            status = {"Self": {"HostName": "studio-demo", "TailscaleIPs": []}}
        else:
            directory = args.data_dir or Path.home() / ".tailprint"
            status = read_status()
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        stack.enter_context(instance_lock(directory))
        tmp = directory / "spool"
        tmp.mkdir(mode=0o700, exist_ok=True)
        for old in tmp.glob("job-*"):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
        backend = DemoBackend() if args.demo else Backend(args.sumatra)
        net = Network(status, args.port, args.demo)
        store = Store(directory)
        app = Application(store, backend, net, tmp)
        servers = [stack.enter_context(Server("127.0.0.1", args.port, app, True))]
        if args.demo:
            store.publish("Studio Laser", True)
        else:
            # Every tailnet has at least one address. Some platforms only bind one family.
            for ip in net.ips:
                try:
                    servers.append(stack.enter_context(Server(ip, args.port, app)))
                except OSError as exc:
                    logging.warning("Адрес %s недоступен: %s", ip, exc)
            if len(servers) == 1:
                raise SystemExit(
                    "Не удалось открыть порт на адресе Tailscale. Проверьте порт и сетевой интерфейс."
                )
            allowed_ips = [s.server_address[0] for s in servers[1:]]
            net.bound = allowed_ips
        stop = threading.Event()
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

        if not args.demo:
            threading.Thread(target=net.run, args=(stop,), daemon=True).start()
        worker = threading.Thread(target=app.worker, args=(stop,), daemon=True)
        worker.start()
        print(
            "Управление на этом компьютере: http://127.0.0.1:" + str(args.port),
            flush=True,
        )
        for s in servers[1:]:
            print(
                "Печать из Tailscale: http://"
                + hostport(s.server_address[0], args.port),
                flush=True,
            )
        if args.demo:
            print("Демонстрационный режим. Физическая печать отключена.", flush=True)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            while not stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            app.wake.set()
            for s in servers:
                s.shutdown()
            worker.join()


if __name__ == "__main__":
    try:
        main()
    except (OSError, PrintError) as exc:
        sys.exit(str(exc))
