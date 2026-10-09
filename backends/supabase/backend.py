"""SupabaseBackend — the frame is an auth user; RLS does the authorization.

What the Pi holds: project URL, the *public* anon key, and the frame's own
email/password. Nothing else. Every read goes through RLS, the manifest is one
RPC, downloads are short-lived signed URLs, and the push channel is a private
Realtime broadcast topic the DB trigger writes to.

Contrast with the Mongo track: there the Pi has a database user and we
enforce the token check in Python. Here the database enforces it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable

from supabase import Client, create_client

from agent.backend import DeviceStatus, Manifest, MediaItem

log = logging.getLogger("frame-sync.supabase")

SIGNED_URL_TTL = 3600


class SupabaseBackend:
    def __init__(self, url: str, anon_key: str, email: str, password: str):
        self.url, self.anon_key = url.rstrip("/"), anon_key
        self.email, self.password = email, password
        self.sb: Client = create_client(url, anon_key)
        self.device_id: str | None = None
        self._token_exp = 0.0

    # --- auth ---------------------------------------------------------------
    def _ensure_session(self) -> str:
        """Sign in (or refresh) and return the current access token."""
        now = time.time()
        if self.device_id and now < self._token_exp - 120:
            return self.sb.auth.get_session().access_token  # type: ignore[union-attr]
        if self.device_id:
            try:
                res = self.sb.auth.refresh_session()
            except Exception as e:  # noqa: BLE001 - refresh token revoked/expired → full sign-in
                log.warning("refresh failed (%s); signing in again", e)
                res = self.sb.auth.sign_in_with_password({"email": self.email, "password": self.password})
        else:
            res = self.sb.auth.sign_in_with_password({"email": self.email, "password": self.password})
        self.device_id = res.user.id
        self._token_exp = float(res.session.expires_at or now + 3000)
        return res.session.access_token

    # --- SyncBackend --------------------------------------------------------
    def register(self) -> None:
        """Nothing to create: the device row is made by the owner (seed / phone UI).
        Signing in proves we are a known frame; RLS proves we're assigned to it."""
        self._ensure_session()
        row = self.sb.table("devices").select("id,name").eq("id", self.device_id).single().execute()
        log.info("signed in as frame %s (%s)", row.data["name"], self.device_id)

    def get_manifest(self) -> Manifest:
        self._ensure_session()
        d = self.sb.rpc("get_manifest").execute().data
        if not d:
            raise RuntimeError("get_manifest returned nothing — is this user in public.devices?")
        items = []
        # Signed URLs in one round-trip rather than one per file.
        paths = [it["storage_path"] for it in d["items"]]
        signed = {}
        if paths:
            for r in self.sb.storage.from_("media").create_signed_urls(paths, SIGNED_URL_TTL):
                if r.get("signedURL") or r.get("signedUrl"):
                    signed[r["path"]] = r.get("signedURL") or r.get("signedUrl")
                else:
                    log.warning("no signed url for %s: %s", r.get("path"), r.get("error"))
        for it in d["items"]:
            url = signed.get(it["storage_path"])
            if not url:
                continue  # RLS denied it or object missing; skip rather than fail the whole sync
            items.append(
                MediaItem(
                    media_id=it["media_id"],
                    playlist=it["playlist"],
                    filename=it["filename"],
                    url=url,
                    sha256=it.get("sha256"),
                    size_bytes=it.get("bytes"),
                )
            )
        return Manifest(d["device_id"], int(d["manifest_version"]), tuple(items), d.get("active_playlist"))

    def subscribe(self, on_change: Callable[[], None]) -> None:
        asyncio.run(self._subscribe(on_change))

    async def _subscribe(self, on_change: Callable[[], None]) -> None:
        """Private broadcast channel 'device:<uuid>'. The DB trigger realtime.send()s
        {manifest_version} on every bump; RLS on realtime.messages decides who may listen."""
        from realtime import AsyncRealtimeClient  # realtime-py >= 2.x

        backoff = 1.0
        while True:
            token = self._ensure_session()
            ws_url = self.url.replace("https://", "wss://").replace("http://", "ws://") + "/realtime/v1"
            client = AsyncRealtimeClient(ws_url, self.anon_key, auto_reconnect=True)
            try:
                await client.connect()
                await client.set_auth(token)  # private channels are authorized with the user JWT
                topic = f"device:{self.device_id}"
                ch = client.channel(topic, {"config": {"private": True, "broadcast": {"self": False}}})

                def _on_manifest(payload, _ref=None):
                    v = (payload or {}).get("payload", payload or {}).get("manifest_version")
                    log.info("broadcast: manifest v%s", v)
                    on_change()

                ch.on_broadcast("manifest", _on_manifest)
                await ch.subscribe()
                log.info("listening on %s", topic)
                backoff = 1.0

                async def _refresh_jwt() -> None:
                    # Realtime closes the socket when the JWT expires (default 1h); re-auth early.
                    while True:
                        await asyncio.sleep(1500)
                        await client.set_auth(self._ensure_session())

                listener = asyncio.create_task(client.listen())
                refresher = asyncio.create_task(_refresh_jwt())
                done, pending = await asyncio.wait({listener, refresher}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.result()  # re-raise whatever ended it
            except Exception as e:  # noqa: BLE001
                log.warning("realtime disconnected: %s", e)
            finally:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
            # We may have missed a bump while down; the agent's poll loop covers that,
            # but a cheap explicit resync on reconnect is the right call.
            on_change()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def report_status(self, status: DeviceStatus) -> None:
        try:
            self._ensure_session()
            self.sb.rpc(
                "heartbeat",
                {
                    "p_status": {
                        "manifest_version": status.manifest_version,
                        "files": status.files_on_disk,
                        "bytes": status.bytes_on_disk,
                        "last_error": status.last_error,
                    }
                },
            ).execute()
        except Exception as e:  # noqa: BLE001
            log.debug("heartbeat failed: %s", e)
