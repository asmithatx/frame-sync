"""The per-device manifest as a single aggregation pipeline.

Kept separate so you can paste it into Compass / mongosh and run
`.explain("executionStats")` on it — which is exactly what a TSE does.
"""
from __future__ import annotations


def manifest_pipeline(device_id: str) -> list[dict]:
    return [
        {"$match": {"_id": device_id}},
        # devices.playlist_ids -> playlists docs
        {
            "$lookup": {
                "from": "playlists",
                "localField": "playlist_ids",
                "foreignField": "_id",
                "as": "pls",
            }
        },
        {"$unwind": "$pls"},
        {"$unwind": "$pls.items"},
        # each item -> media doc (only the fields the frame needs)
        {
            "$lookup": {
                "from": "media",
                "let": {"mid": "$pls.items.media_id"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$mid"]}}},
                    {"$project": {"filename": 1, "sha256": 1, "bytes": 1, "variants": 1, "storage_key": 1}},
                ],
                "as": "m",
            }
        },
        {"$unwind": "$m"},
        {"$sort": {"pls.slug": 1, "pls.items.order": 1}},
        {
            "$group": {
                "_id": "$_id",
                "manifest_version": {"$first": "$manifest_version"},
                "active_playlist": {"$first": "$active_playlist"},
                "width": {"$first": "$width"},
                "items": {
                    "$push": {
                        "media_id": {"$toString": "$m._id"},
                        "playlist": "$pls.slug",
                        "filename": "$m.filename",
                        "sha256": "$m.sha256",
                        "bytes": "$m.bytes",
                        "variants": "$m.variants",
                        "storage_key": "$m.storage_key",
                    }
                },
            }
        },
    ]


def empty_manifest_pipeline(device_id: str) -> list[dict]:
    """A device with no playlists still needs its version/active fields."""
    return [
        {"$match": {"_id": device_id}},
        {"$project": {"manifest_version": 1, "active_playlist": 1, "width": 1}},
    ]
