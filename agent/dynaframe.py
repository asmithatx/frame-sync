"""Dynaframe integration: write files where Dynaframe looks, then poke it.

Dynaframe treats folders as playlists, so each playlist becomes a subfolder of
MEDIA_ROOT. Downloads go to a temp name and are atomically renamed so Dynaframe
never picks up a half-written JPEG.

The "poke" is version-dependent. Open-source Dynaframe 2.x/3 accepts simple
HTTP GETs against its web UI; the commercial Geek Toolkit build may differ.
RELOAD_URL_TEMPLATE is therefore config, not code: set it once you've
reverse-engineered the right URL from the web UI (see wiki). If unset, the
agent just writes files and lets Dynaframe's folder scan pick them up.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import urllib.request
from pathlib import Path

log = logging.getLogger("frame-sync.dynaframe")


class DynaframeTarget:
    def __init__(self, media_root: Path, reload_url_template: str | None = None, timeout: float = 5.0):
        self.media_root = media_root
        self.reload_url_template = reload_url_template
        self.timeout = timeout
        media_root.mkdir(parents=True, exist_ok=True)

    # --- paths ------------------------------------------------------------
    def path_for(self, playlist: str, filename: str) -> Path:
        safe_pl = playlist.replace("/", "_").replace("..", "_")
        safe_fn = os.path.basename(filename)
        return self.media_root / safe_pl / safe_fn

    # --- download ---------------------------------------------------------
    def download(self, url: str, dest: Path, expected_sha256: str | None = None) -> tuple[str, int]:
        """Stream url -> dest atomically. Returns (sha256, size)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        h = hashlib.sha256()
        size = 0
        req = urllib.request.Request(url, headers={"User-Agent": "frame-sync-agent/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
        digest = h.hexdigest()
        if expected_sha256 and digest != expected_sha256:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {dest.name}: got {digest[:12]}, want {expected_sha256[:12]}")
        os.replace(tmp, dest)
        return digest, size

    def remove(self, playlist: str, filename: str) -> None:
        p = self.path_for(playlist, filename)
        p.unlink(missing_ok=True)
        # drop empty playlist folders so Dynaframe's playlist list stays clean
        try:
            p.parent.rmdir()
        except OSError:
            pass

    def wipe_unknown(self, keep: set[Path]) -> None:
        """Delete anything under media_root we don't know about (stale from a prior backend)."""
        for p in self.media_root.rglob("*"):
            if p.is_file() and p not in keep and not p.name.endswith(".part"):
                log.info("removing unknown file %s", p)
                p.unlink()

    # --- notify -----------------------------------------------------------
    def reload(self, active_playlist: str | None) -> None:
        if not self.reload_url_template:
            return
        url = self.reload_url_template.format(playlist=active_playlist or "")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                log.info("dynaframe reload -> %s", r.status)
        except Exception as e:  # noqa: BLE001 - best effort
            log.warning("dynaframe reload failed: %s", e)
