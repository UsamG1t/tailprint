"""Local OS print queues and one-shot PDF submission."""

import json
import os
import shutil
import subprocess
from pathlib import Path


class PrintError(Exception):
    pass


def command(args, timeout=12):
    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise PrintError(f"Не найдена программа {args[0]}") from exc
    if result.returncode:
        raise PrintError(
            (
                result.stderr.strip()
                or result.stdout.strip()
                or f"{args[0]}: код {result.returncode}"
            )[:350]
        )
    return result.stdout.strip()


def cups_queues(text):
    queues = []
    for line in text.splitlines():
        if not line.startswith("device for ") or ": " not in line:
            continue
        name, uri = line[len("device for ") :].split(": ", 1)
        usb = uri.lower().startswith(
            ("usb:", "parallel:", "serial:", "hp:/usb", "cnijusb:", "epsonusb:")
        )
        queues.append(
            {"name": name, "kind": "Прямое подключение" if usb else "Системная очередь"}
        )
    return queues


def windows_queues(text):
    result = []
    for p in json.loads(text):
        port = str(p["PortName"]).upper().rstrip(":")
        if port in {"FILE", "PORTPROMPT", "NUL", "SHRFAX"} or port.startswith(
            "XPSPORT"
        ):
            continue
        direct = port.startswith(("USB", "DOT4", "LPT", "COM"))
        result.append(
            {
                "name": p["Name"],
                "kind": "Прямое подключение" if direct else "Системная очередь",
            }
        )
    return result


class Backend:
    def __init__(self, sumatra=None):
        self.sumatra = sumatra

    def list(self):
        if os.name == "nt":
            script = (
                "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                "$p=@(Get-Printer | Select-Object Name,PortName); ConvertTo-Json -Compress -InputObject $p"
            )
            return windows_queues(
                command(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                    ]
                )
            )
        try:
            return cups_queues(command(["lpstat", "-v"]))
        except PrintError as exc:
            if "No destinations added" in str(exc):
                return []
            raise

    def submit(self, printer, copies, path, opts):
        paper = opts["paper"]
        if os.name != "nt":
            sides = {
                "simplex": "one-sided",
                "long": "two-sided-long-edge",
                "short": "two-sided-short-edge",
            }[opts["duplex"]]
            return command(
                [
                    "lp",
                    "-d",
                    printer,
                    "-n",
                    str(copies),
                    "-o",
                    "media=" + paper,
                    "-o",
                    "sides=" + sides,
                    "-o",
                    "number-up=1",
                    "-o",
                    "print-scaling=none",
                    "-t",
                    "TailPrint",
                    str(path),
                ],
                timeout=90,
            )
        candidates = [self.sumatra, shutil.which("SumatraPDF.exe")]
        for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(var):
                candidates.append(
                    Path(os.environ[var]) / "SumatraPDF" / "SumatraPDF.exe"
                )
        exe = next((str(p) for p in candidates if p and Path(p).is_file()), None)
        if not exe:
            raise PrintError("Установите SumatraPDF или укажите путь через --sumatra.")
        sides = {"simplex": "simplex", "long": "duplexlong", "short": "duplexshort"}[
            opts["duplex"]
        ]
        command(
            [
                exe,
                "-silent",
                "-print-to",
                printer,
                "-print-settings",
                f"{copies}x,{sides},paper={paper},noscale",
                str(path),
            ],
            timeout=120,
        )
        return "Передано через SumatraPDF"


class DemoBackend:
    def list(self):
        return [
            {"name": "Studio Laser", "kind": "Демонстрационный принтер"},
            {"name": "ColorJet A4", "kind": "Демонстрационный принтер"},
        ]

    def submit(self, printer, copies, path, opts):
        return "Демонстрация · физическая печать отключена"
