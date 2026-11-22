import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from pypdf import PdfReader, PdfWriter

from tailprint.backends import cups_queues, windows_queues
from tailprint.documents import prepare, selected_pages, settings, sides, validate_file
from tailprint.network import Network
from tailprint.store import Problem, Store


class LayoutTest(unittest.TestCase):
    def test_booklet(self):
        self.assertEqual(
            list(sides(list(range(1, 9)), 2, True)), [[8, 1], [2, 7], [6, 3], [4, 5]]
        )
        self.assertEqual(
            list(sides([1, 2, 3, 4, 5], 2, True)),
            [[None, 1], [2, None], [None, 3], [4, 5]],
        )
        self.assertEqual(settings({"booklet": True})["duplex"], "short")

    def test_ranges(self):
        self.assertEqual(selected_pages("1-3,5", 7), [0, 1, 2, 4])
        for spec in ["4-2", "1-8"]:
            with self.assertRaises(ValueError):
                selected_pages(spec, 5)
        with self.assertRaises(ValueError):
            settings({"nup": True})

    def test_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name, fmt in [("a.png", "PNG"), ("a.jpeg", "JPEG")]:
                image = Image.new("RGB", (160, 120), "red")
                source = folder / name
                image.save(source, fmt)
                validate_file(source.suffix, source.read_bytes())
                prepare(source, settings({}), folder / "out.pdf")
                self.assertEqual(len(PdfReader(folder / "out.pdf").pages), 1)
            source = folder / "source.svg"
            source.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 3 2"><rect width="3" height="2" fill="red"/></svg>'
            )
            prepare(source, settings({"booklet": True}), folder / "out.pdf")
            out = PdfReader(folder / "out.pdf")
            self.assertEqual(len(out.pages), 2)
            self.assertGreater(
                out.pages[0].mediabox.width, out.pages[0].mediabox.height
            )

    def test_multipage(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            writer = PdfWriter()
            for _ in range(9):
                writer.add_blank_page(width=612, height=792)
            writer.write(folder / "in.pdf")
            prepare(
                folder / "in.pdf",
                settings({"nup": 4, "pages": "1-4,7", "scale": "shrink"}),
                folder / "out.pdf",
            )
            self.assertEqual(len(PdfReader(folder / "out.pdf").pages), 2)


class QueueTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = Store(self.dir)
        self.store.publish("Laser", True)
        self.args = ["alice", str(uuid4()), "Laser", 2, "doc.pdf", b"pdf", settings({})]

    def test_duplicate_and_restart(self):
        self.store.enqueue(*self.args)
        threads = [
            threading.Thread(target=self.store.enqueue, args=self.args)
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.history("alice")), 1)
        self.assertEqual(self.store.history("bob"), [])
        restarted = Store(self.dir)
        self.assertEqual(restarted.take()["state"], "queued")
        self.assertEqual(Store(self.dir).history("alice")[0]["state"], "uncertain")
        self.assertIsNone(self.store.take())

    def test_changed_options_and_unpublish(self):
        self.store.enqueue(*self.args)
        self.args[-1] = settings({"booklet": True})
        with self.assertRaises(Problem):
            self.store.enqueue(*self.args)
        self.store.publish("Laser", False)
        self.assertIsNone(self.store.take())
        self.assertEqual(self.store.history("alice")[0]["state"], "failed")


class NetworkTest(unittest.TestCase):
    def test_discover_remove_offline(self):
        peer = {
            "ID": "p",
            "HostName": "office",
            "Online": True,
            "TailscaleIPs": ["100.101.1.2"],
        }
        status = {
            "Self": {"HostName": "home", "TailscaleIPs": ["100.101.1.1"]},
            "Peer": {"p": peer},
        }
        net = Network(status, 8787)
        with (
            patch("tailprint.network.read_status", return_value=status),
            patch("tailprint.network.peer_catalog", return_value=["Laser"]),
        ):
            net.refresh()
            self.assertEqual(net.peers["p"]["printers"], ["Laser"])
            peer["Online"] = False
            net.refresh()
            self.assertFalse(net.peers["p"]["online"])
            status["Peer"].clear()
            net.refresh()
            self.assertEqual(net.peers, {})

    def test_local_discovery(self):
        self.assertEqual(
            cups_queues("device for laser: usb://vendor/laser")[0]["name"], "laser"
        )
        self.assertEqual(
            windows_queues(
                '[{"Name":"Laser А4","PortName":"USB001"},{"Name":"PDF","PortName":"PORTPROMPT:"}]'
            ),
            [{"name": "Laser А4", "kind": "Прямое подключение"}],
        )
