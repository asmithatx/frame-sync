"""SyncBackend: the one interface every cloud backend implements.

The agent never imports pymongo, supabase, or an Immich client directly.
It only talks to this Protocol, so swapping backends is a config change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol


@dataclass(frozen=True)
class MediaItem:
    """One file the frame should have on disk."""
    media_id: str          # backend's stable ID for this asset
    playlist: str          # slug; becomes the subfolder name under media_root
    filename: str          # e.g. "2026-10-03_1234.jpg"
    url: str               # where the agent downloads it from (signed URL ok)
    sha256: str | None     # optional; lets the agent skip re-downloads
    size_bytes: int | None = None


@dataclass(frozen=True)
class Manifest:
    """Everything a single frame should be showing right now."""
    device_id: str
    version: int                       # monotonically increasing; bump = resync
    items: tuple[MediaItem, ...] = field(default_factory=tuple)
    active_playlist: str | None = None # which subfolder Dynaframe should play

    def by_playlist(self) -> dict[str, list[MediaItem]]:
        out: dict[str, list[MediaItem]] = {}
        for it in self.items:
            out.setdefault(it.playlist, []).append(it)
        return out


@dataclass(frozen=True)
class DeviceStatus:
    device_id: str
    manifest_version: int
    files_on_disk: int
    bytes_on_disk: int
    last_error: str | None = None


class SyncBackend(Protocol):
    def register(self) -> None:
        """Ensure this device exists server-side. Idempotent."""

    def get_manifest(self) -> Manifest:
        """Full desired state for this device."""

    def subscribe(self, on_change: Callable[[], None]) -> None:
        """Block, calling on_change() whenever the manifest may have changed.

        Implementations must reconnect on failure and must not miss events
        across reconnects where the backend supports it (Mongo resume tokens,
        Supabase replay via version compare, Immich = poll).
        """

    def report_status(self, status: DeviceStatus) -> None:
        """Best-effort heartbeat. Never raise."""
