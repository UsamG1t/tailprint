"""Discover only the Tailprint catalog on peers reported by the local Tailscale client."""

import http.client
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backends import PrintError, command


def executable():
    candidates = [
        shutil.which("tailscale"),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]
    if os.environ.get("ProgramFiles"):
        candidates.append(
            Path(os.environ["ProgramFiles"]) / "Tailscale" / "tailscale.exe"
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise PrintError("Установите Tailscale и подключите этот компьютер к своей сети.")


def tail_ips(node):
    v4 = ipaddress.ip_network("100.64.0.0/10")
    v6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
    ips = [ipaddress.ip_address(s) for s in (node.get("TailscaleIPs") or [])]
    return [
        str(ip) for ip in sorted(ips, key=lambda x: x.version) if ip in v4 or ip in v6
    ]


def hostport(ip, port):
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def read_status():
    status = json.loads(command([executable(), "status", "--json"]))
    if status.get("BackendState") != "Running" or not tail_ips(status["Self"]):
        raise PrintError("Tailscale не подключён. Проверьте tailscale status.")
    return status


def peer_catalog(ip, port):
    conn = http.client.HTTPConnection(ip, port, timeout=2)
    try:
        conn.request("GET", "/api/v1/catalog")
        response = conn.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(65537))
        if not isinstance(data, dict) or data.get("service") != "tailprint/1":
            return None
        queues = data.get("printers")
        if not isinstance(queues, list) or len(queues) > 512:
            return None
        if any(not isinstance(p, str) or not 0 < len(p) <= 256 for p in queues):
            return None
        return queues
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


class Network:
    def __init__(self, status, port, demo=False):
        self.port = port
        self.demo = demo
        self.bound = []
        self.lock = threading.Lock()
        self.peers = {}
        self.error = ""
        self.known = {}
        self.update_nodes(status)

    def update_nodes(self, status):
        own = status["Self"]
        self.name = own.get("HostName") or socket.gethostname()
        self.ips = tail_ips(own)
        self.dns = set(
            filter(
                None, (self.name.lower(), own.get("DNSName", "").lower().rstrip("."))
            )
        )
        nodes = []
        known = {}
        for node in (status.get("Peer") or {}).values():
            ips = tail_ips(node)
            if not ips:
                continue
            stable_id = node.get("ID") or ips[0]
            for ip in ips:
                known[ip] = stable_id
            nodes.append(
                (
                    stable_id,
                    node.get("HostName") or ips[0],
                    ips,
                    bool(node.get("Online")),
                )
            )
        with self.lock:
            self.known = known
        return nodes

    def refresh(self):
        status = read_status()
        nodes = self.update_nodes(status)

        def inspect(node):
            key, name, ips, online = node
            queues = None
            ip = ips[0]
            if online:
                for ip in ips:
                    queues = peer_catalog(ip, self.port)
                    if queues is not None:
                        break
            if queues is None and key not in self.peers:
                return key, None
            old = self.peers.get(key, {})
            return key, {
                "host": name,
                "url": "http://" + hostport(ip, self.port),
                "online": queues is not None,
                "printers": old.get("printers", []) if queues is None else queues,
            }

        with ThreadPoolExecutor(max_workers=12) as pool:
            found = {k: v for k, v in pool.map(inspect, nodes) if v}
        with self.lock:
            self.peers = found
            self.error = ""

    def run(self, stop):
        while not stop.is_set():
            try:
                self.refresh()
            except (
                PrintError,
                OSError,
                ValueError,
                KeyError,
                subprocess.TimeoutExpired,
            ) as exc:
                logging.warning("Tailscale: %s", exc)
                with self.lock:
                    self.known = {}
                    self.error = "Нет связи с Tailscale. Проверьте подключение агента."
                    self.peers = {
                        k: {**p, "online": False} for k, p in self.peers.items()
                    }
            stop.wait(20)
