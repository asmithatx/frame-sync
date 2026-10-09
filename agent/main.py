"""frame-sync agent entry point.

    python -m agent.main            # uses env vars / .env

Loop:
  register -> full sync -> subscribe(on_change -> sync) ; periodic status + fallback poll
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .backend import DeviceStatus, Manifest, SyncBackend
from .dynaframe import DynaframeTarget
from .state import LocalState

log = logging.getLogger("frame-sync")


def load_env() -> None:
    """Tiny .env loader so the Pi doesn't need python-dotenv."""
    p = Path(os.environ.get("FRAME_SYNC_ENV", ".env"))
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def build_backend(device_id: str) -> SyncBackend:
    kind = os.environ.get("BACKEND", "mongo").lower()
    if kind == "mongo":
        from backends.mongo.backend import MongoBackend
        return MongoBackend(
            uri=os.environ["MONGO_URI"],
            db_name=os.environ.get("MONGO_DB", "framesync"),
            device_id=device_id,
            device_token=os.environ["DEVICE_TOKEN"],
            resume_token_path=Path(os.environ.get("STATE_DIR", "state")) / "mongo.resume",
        )
    if kind == "supabase":
        from backends.supabase.backend import SupabaseBackend
        return SupabaseBackend(
            url=os.environ["SUPABASE_URL"],
            anon_key=os.environ["SUPABASE_ANON_KEY"],
            email=os.environ["DEVICE_EMAIL"],
            password=os.environ["DEVICE_PASSWORD"],
        )
    if kind == "immich":
        raise NotImplementedError("Track 3 — see backends/immich/")
    raise SystemExit(f"unknown BACKEND={kind}")


class Agent:
    def __init__(self, backend: SyncBackend, state: LocalState, target: DynaframeTarget, device_id: str):
        self.backend, self.state, self.target, self.device_id = backend, state, target, device_id
        self._lock = threading.Lock()
        self._last_error: str | None = None

    # --- the core diff -----------------------------------------------------
    def sync(self, manifest: Manifest | None = None) -> bool:
        """Reconcile disk with manifest. Returns True if anything changed."""
        with self._lock:
            try:
                m = manifest or self.backend.get_manifest()
            except Exception as e:  # noqa: BLE001
                self._last_error = f"get_manifest: {e}"
                log.error(self._last_error)
                return False

            if m.version == self.state.manifest_version and manifest is None:
                log.debug("manifest v%s unchanged", m.version)
                return False

            known = self.state.known()
            wanted = {it.media_id: it for it in m.items}
            changed = False

            # deletions first — frees disk before downloads land
            for media_id, (pl, fn, _) in list(known.items()):
                if media_id not in wanted:
                    log.info("- %s/%s", pl, fn)
                    self.target.remove(pl, fn)
                    self.state.forget(media_id)
                    changed = True

            # additions / moves / content changes
            for media_id, it in wanted.items():
                prev = known.get(media_id)
                dest = self.target.path_for(it.playlist, it.filename)
                if prev and prev[0] == it.playlist and prev[1] == it.filename and dest.exists():
                    if it.sha256 is None or prev[2] == it.sha256:
                        continue  # already have it
                if prev:
                    self.target.remove(prev[0], prev[1])
                try:
                    log.info("+ %s/%s", it.playlist, it.filename)
                    digest, size = self.target.download(it.url, dest, it.sha256)
                    self.state.record(media_id, it.playlist, it.filename, digest, size)
                    changed = True
                except Exception as e:  # noqa: BLE001
                    self._last_error = f"download {it.filename}: {e}"
                    log.error(self._last_error)
                    # leave version unbumped so we retry next pass
                    return changed

            self.state.manifest_version = m.version
            self._last_error = None
            if changed:
                self.target.reload(m.active_playlist)
            log.info("synced to manifest v%s (%d items)", m.version, len(m.items))
            return changed

    def status(self) -> DeviceStatus:
        n, b = self.state.totals()
        return DeviceStatus(self.device_id, self.state.manifest_version, n, b, self._last_error)

    # --- run loop ----------------------------------------------------------
    def run(self, poll_seconds: int = 300, heartbeat_seconds: int = 60) -> None:
        self.backend.register()
        self.sync()
        stop = threading.Event()

        def heartbeat() -> None:
            last_poll = time.monotonic()
            while not stop.is_set():
                try:
                    self.backend.report_status(self.status())
                except Exception as e:  # noqa: BLE001
                    log.warning("report_status: %s", e)
                if time.monotonic() - last_poll > poll_seconds:
                    last_poll = time.monotonic()
                    self.sync()  # belt-and-braces if the push channel is silent
                stop.wait(heartbeat_seconds)

        threading.Thread(target=heartbeat, daemon=True, name="heartbeat").start()

        def on_signal(*_: object) -> None:
            log.info("shutting down")
            stop.set()
            sys.exit(0)

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)

        # subscribe() blocks; backend handles reconnects
        self.backend.subscribe(on_change=lambda: self.sync())


def main() -> None:
    load_env()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    device_id = os.environ.get("DEVICE_ID") or os.uname().nodename
    state_dir = Path(os.environ.get("STATE_DIR", "state"))
    target = DynaframeTarget(
        media_root=Path(os.environ.get("MEDIA_ROOT", str(Path.home() / "Pictures" / "frames"))),
        reload_url_template=os.environ.get("DYNAFRAME_RELOAD_URL") or None,
    )
    agent = Agent(build_backend(device_id), LocalState(state_dir / "agent.sqlite"), target, device_id)
    agent.run(
        poll_seconds=int(os.environ.get("POLL_SECONDS", "300")),
        heartbeat_seconds=int(os.environ.get("HEARTBEAT_SECONDS", "60")),
    )


if __name__ == "__main__":
    main()
