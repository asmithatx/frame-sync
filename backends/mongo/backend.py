"""MongoBackend — Atlas (or any replica set) + S3-compatible object storage.

Push channel: a change stream on the device's own document, filtered
server-side, with the resume token persisted to disk so a reboot or a
dropped connection never misses a manifest bump.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bson import json_util
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

from agent.backend import DeviceStatus, Manifest, MediaItem

from .manifest import empty_manifest_pipeline, manifest_pipeline

log = logging.getLogger("frame-sync.mongo")


# --------------------------------------------------------------------------
# Object storage: Mongo doesn't bundle one, so this is pluggable.
# Dev: STORAGE_BASE_URL=https://pub-xxxx.r2.dev  (public bucket)
# Real: S3_BUCKET + S3_ENDPOINT + keys -> presigned GETs (R2/B2/S3/MinIO)
# --------------------------------------------------------------------------
class Storage:
    def __init__(self) -> None:
        self.base = os.environ.get("STORAGE_BASE_URL", "").rstrip("/")
        self.bucket = os.environ.get("S3_BUCKET")
        self._s3 = None
        if self.bucket:
            import boto3  # only needed when presigning

            self._s3 = boto3.client(
                "s3",
                endpoint_url=os.environ.get("S3_ENDPOINT"),
                aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
                aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
                region_name=os.environ.get("S3_REGION", "auto"),
            )

    def url_for(self, key: str) -> str:
        if self._s3:
            return self._s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
            )
        if self.base:
            return f"{self.base}/{key}"
        raise RuntimeError("set STORAGE_BASE_URL or S3_BUCKET")


class MongoBackend:
    def __init__(self, uri: str, db_name: str, device_id: str, device_token: str, resume_token_path: Path):
        self.client = MongoClient(uri, appname=f"frame-sync/{device_id}", serverSelectionTimeoutMS=10_000)
        self.db = self.client[db_name]
        self.device_id = device_id
        self.token_hash = hashlib.sha256(device_token.encode()).hexdigest()
        self.resume_path = resume_token_path
        self.storage = Storage()

    # --- SyncBackend --------------------------------------------------------
    def register(self) -> None:
        """Idempotent: create the device doc if missing; verify token if present.

        In production the device would hit a FastAPI endpoint with its token
        and never hold Mongo credentials. For the hobby build the Pi has a
        least-privilege Atlas user (read on playlists/media, readWrite on
        devices) and we enforce the token check here.
        """
        doc = self.db.devices.find_one({"_id": self.device_id}, {"token_hash": 1})
        if doc is None:
            self.db.devices.insert_one(
                {
                    "_id": self.device_id,
                    "owner_id": os.environ.get("OWNER_ID", "andrew"),
                    "token_hash": self.token_hash,
                    "playlist_ids": [],
                    "active_playlist": None,
                    "manifest_version": 1,
                    "status": {},
                    "width": int(os.environ.get("FRAME_WIDTH", "1920")),
                    "created_at": datetime.now(timezone.utc),
                }
            )
            log.info("registered new device %s", self.device_id)
        elif doc.get("token_hash") != self.token_hash:
            raise PermissionError(f"device token mismatch for {self.device_id}")

    def get_manifest(self) -> Manifest:
        rows = list(self.db.devices.aggregate(manifest_pipeline(self.device_id)))
        if not rows:  # no playlists assigned (or none with items)
            rows = list(self.db.devices.aggregate(empty_manifest_pipeline(self.device_id)))
            if not rows:
                raise RuntimeError(f"device {self.device_id} not found")
            d = rows[0]
            return Manifest(self.device_id, int(d["manifest_version"]), (), d.get("active_playlist"))

        d = rows[0]
        want_w = str(d.get("width") or 1920)
        items = []
        for it in d["items"]:
            key = (it.get("variants") or {}).get(want_w) or it["storage_key"]
            items.append(
                MediaItem(
                    media_id=it["media_id"],
                    playlist=it["playlist"],
                    filename=it["filename"],
                    url=self.storage.url_for(key),
                    sha256=it.get("sha256") if key == it["storage_key"] else None,  # variant has a different hash
                    size_bytes=it.get("bytes"),
                )
            )
        return Manifest(self.device_id, int(d["manifest_version"]), tuple(items), d.get("active_playlist"))

    def subscribe(self, on_change: Callable[[], None]) -> None:
        """Watch only our own device doc. Reconnect with backoff; resume from token."""
        pipeline = [
            {
                "$match": {
                    "operationType": {"$in": ["update", "replace"]},
                    "documentKey._id": self.device_id,
                    # ignore our own heartbeat writes
                    "updateDescription.updatedFields.manifest_version": {"$exists": True},
                }
            }
        ]
        backoff = 1.0
        while True:
            resume = self._load_resume()
            kwargs = {"resume_after": resume} if resume else {}
            try:
                with self.db.devices.watch(pipeline, **kwargs) as stream:
                    log.info("change stream open (resume=%s)", bool(resume))
                    backoff = 1.0
                    for event in stream:
                        self._save_resume(event["_id"])
                        log.info("manifest bump -> v%s", event["updateDescription"]["updatedFields"].get("manifest_version"))
                        on_change()
            except OperationFailure as e:
                # 286 ChangeStreamHistoryLost: oplog rolled past our token. Drop it and full-sync.
                if e.code == 286:
                    log.warning("resume token too old; discarding and resyncing")
                    self.resume_path.unlink(missing_ok=True)
                    on_change()
                else:
                    log.error("change stream failed: %s", e)
            except PyMongoError as e:
                log.warning("change stream disconnected: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def report_status(self, status: DeviceStatus) -> None:
        try:
            self.db.devices.update_one(
                {"_id": self.device_id},
                {
                    "$set": {
                        "status": {
                            "seen_at": datetime.now(timezone.utc),
                            "manifest_version": status.manifest_version,
                            "files": status.files_on_disk,
                            "bytes": status.bytes_on_disk,
                            "last_error": status.last_error,
                        }
                    }
                },
            )
        except PyMongoError as e:
            log.debug("heartbeat failed: %s", e)

    # --- resume token persistence ----------------------------------------
    def _load_resume(self):
        if self.resume_path.exists():
            try:
                return json_util.loads(self.resume_path.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None

    def _save_resume(self, token) -> None:
        self.resume_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.resume_path.with_suffix(".tmp")
        tmp.write_text(json_util.dumps(token))
        os.replace(tmp, self.resume_path)
