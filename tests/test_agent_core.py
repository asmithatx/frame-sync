"""End-to-end test of the agent core with a fake backend and a local HTTP server.

No cloud, no Mongo: proves the diff/download/delete/version logic.

    python -m tests.test_agent_core
"""
from __future__ import annotations

import hashlib
import http.server
import tempfile
import threading
from pathlib import Path

from agent.backend import DeviceStatus, Manifest, MediaItem
from agent.dynaframe import DynaframeTarget
from agent.main import Agent
from agent.state import LocalState


class FakeBackend:
    def __init__(self, base_url: str):
        self.base, self.version, self.items = base_url, 1, {}
        self.statuses: list[DeviceStatus] = []

    def _item(self, pl: str, name: str, body: bytes) -> MediaItem:
        return MediaItem(f"id-{name}", pl, name, f"{self.base}/{name}", hashlib.sha256(body).hexdigest(), len(body))

    def register(self) -> None: ...
    def get_manifest(self) -> Manifest:
        return Manifest("test-frame", self.version, tuple(self.items.values()), "family")
    def subscribe(self, on_change) -> None: ...
    def report_status(self, s: DeviceStatus) -> None:
        self.statuses.append(s)


def main() -> None:
    srv_dir = Path(tempfile.mkdtemp(prefix="srv-"))
    media_dir = Path(tempfile.mkdtemp(prefix="frames-"))
    files = {"a.jpg": b"AAAA" * 100, "b.jpg": b"BBBB" * 100, "c.jpg": b"CCCC" * 100}
    for n, b in files.items():
        (srv_dir / n).write_bytes(b)

    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(srv_dir), **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    be = FakeBackend(base)
    be.items = {"a": be._item("family", "a.jpg", files["a.jpg"]), "b": be._item("family", "b.jpg", files["b.jpg"])}
    agent = Agent(be, LocalState(media_dir / ".state.sqlite"), DynaframeTarget(media_dir), "test-frame")

    # 1. initial sync downloads both into family/
    assert agent.sync() is True
    assert sorted(p.name for p in (media_dir / "family").iterdir()) == ["a.jpg", "b.jpg"]
    assert agent.state.manifest_version == 1

    # 2. same version -> no-op
    assert agent.sync() is False

    # 3. remove b, add c in a new playlist, move a to 'art'
    be.version = 2
    be.items = {"a": be._item("art", "a.jpg", files["a.jpg"]), "c": be._item("art", "c.jpg", files["c.jpg"])}
    assert agent.sync() is True
    assert not (media_dir / "family").exists(), "empty playlist folder should be removed"
    assert sorted(p.name for p in (media_dir / "art").iterdir()) == ["a.jpg", "c.jpg"]
    assert agent.state.manifest_version == 2

    # 4. sha mismatch -> error recorded, version NOT bumped, retry possible
    be.version = 3
    bad = MediaItem("id-bad", "art", "bad.jpg", f"{base}/a.jpg", "0" * 64, 1)
    be.items["bad"] = bad
    agent.sync()
    assert agent.state.manifest_version == 2
    assert "sha256 mismatch" in (agent.status().last_error or "")
    assert not (media_dir / "art" / "bad.jpg").exists()
    assert not list((media_dir / "art").glob("*.part")), "no partial files left behind"

    # 5. fix it -> recovers
    (srv_dir / "bad.jpg").write_bytes(files["a.jpg"])
    be.items["bad"] = be._item("art", "bad.jpg", files["a.jpg"])
    assert agent.sync() is True
    assert agent.state.manifest_version == 3
    assert agent.status().last_error is None

    n, b = agent.state.totals()
    assert n == 3 and b == 1200, (n, b)
    httpd.shutdown()
    print("agent core: all checks passed")


if __name__ == "__main__":
    main()
