"""Seed an Atlas (or local replica set) database for frame-sync.

    python -m backends.mongo.seed              # create collections, indexes, sample data
    python -m backends.mongo.seed --add family 2026-10-03_1234.jpg   # simulate a phone upload
    python -m backends.mongo.seed --bump frame-kitchen                # force a frame to resync

Schema validation is on in "warn" mode: it documents the shape without
making the hobby project brittle. Flip to "error" once stable.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import CollectionInvalid

OWNER = os.environ.get("OWNER_ID", "andrew")

MEDIA_SCHEMA = {
    "bsonType": "object",
    "required": ["owner_id", "sha256", "filename", "mime", "storage_key", "uploaded_at"],
    "properties": {
        "owner_id": {"bsonType": "string"},
        "sha256": {"bsonType": "string", "pattern": "^[a-f0-9]{64}$"},
        "filename": {"bsonType": "string"},
        "mime": {"enum": ["image/jpeg", "image/png", "image/webp", "video/mp4"]},
        "storage_key": {"bsonType": "string"},
        "variants": {"bsonType": "object"},
        "bytes": {"bsonType": ["int", "long"]},
        "width": {"bsonType": "int"},
        "height": {"bsonType": "int"},
        "tags": {"bsonType": "array", "items": {"bsonType": "string"}},
    },
}

PLAYLIST_SCHEMA = {
    "bsonType": "object",
    "required": ["owner_id", "slug", "name", "items", "updated_at"],
    "properties": {
        "slug": {"bsonType": "string", "pattern": "^[a-z0-9-]{1,40}$"},
        "items": {
            "bsonType": "array",
            "items": {
                "bsonType": "object",
                "required": ["media_id", "added_at", "order"],
                "properties": {
                    "media_id": {"bsonType": "objectId"},
                    "added_at": {"bsonType": "date"},
                    "order": {"bsonType": "int"},
                },
            },
        },
    },
}

DEVICE_SCHEMA = {
    "bsonType": "object",
    "required": ["owner_id", "token_hash", "playlist_ids", "manifest_version"],
    "properties": {
        "_id": {"bsonType": "string"},
        "token_hash": {"bsonType": "string"},
        "playlist_ids": {"bsonType": "array", "items": {"bsonType": "objectId"}},
        "active_playlist": {"bsonType": ["string", "null"]},
        "manifest_version": {"bsonType": ["int", "long"]},
        "status": {"bsonType": "object"},
    },
}


def ensure_collection(db, name: str, schema: dict) -> None:
    try:
        db.create_collection(name, validator={"$jsonSchema": schema}, validationAction="warn")
        print(f"created {name}")
    except CollectionInvalid:
        db.command("collMod", name, validator={"$jsonSchema": schema}, validationAction="warn")
        print(f"updated validator on {name}")


def ensure_indexes(db) -> None:
    db.media.create_index([("sha256", ASCENDING)], unique=True, name="uniq_sha")
    db.media.create_index([("owner_id", ASCENDING), ("taken_at", DESCENDING)], name="owner_recent")
    db.media.create_index([("owner_id", ASCENDING), ("tags", ASCENDING)], name="owner_tags")
    db.playlists.create_index([("owner_id", ASCENDING), ("slug", ASCENDING)], unique=True, name="uniq_slug")
    db.devices.create_index([("owner_id", ASCENDING)], name="owner")
    db.devices.create_index([("playlist_ids", ASCENDING)], name="by_playlist")  # for the fan-out bump
    print("indexes ok")


def fake_media(filename: str, playlist_hint: str) -> dict:
    sha = hashlib.sha256(filename.encode()).hexdigest()
    return {
        "owner_id": OWNER,
        "sha256": sha,
        "filename": filename,
        "mime": "image/jpeg",
        "storage_key": f"originals/{OWNER}/{sha}.jpg",
        "variants": {"1920": f"derived/{OWNER}/{sha}_1920.jpg", "1280": f"derived/{OWNER}/{sha}_1280.jpg"},
        "bytes": 1_500_000,
        "width": 4032,
        "height": 3024,
        "taken_at": datetime.now(timezone.utc),
        "uploaded_at": datetime.now(timezone.utc),
        "tags": [playlist_hint],
    }


def sample_data(db) -> None:
    if db.playlists.count_documents({"owner_id": OWNER}):
        print("sample data already present")
        return
    now = datetime.now(timezone.utc)
    fam = db.playlists.insert_one(
        {"owner_id": OWNER, "slug": "family", "name": "Family", "items": [], "updated_at": now}
    ).inserted_id
    art = db.playlists.insert_one(
        {"owner_id": OWNER, "slug": "ai-art", "name": "AI Art", "items": [], "updated_at": now}
    ).inserted_id

    for name, dev_pls, active in [
        ("frame-kitchen", [fam], "family"),
        ("frame-office", [fam, art], "ai-art"),
    ]:
        token = f"dev-token-{name}"
        db.devices.insert_one(
            {
                "_id": name,
                "owner_id": OWNER,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "playlist_ids": dev_pls,
                "active_playlist": active,
                "manifest_version": 1,
                "status": {},
                "width": 1920,
                "created_at": now,
            }
        )
        print(f"device {name}  DEVICE_TOKEN={token}")

    for i in range(3):
        add_to_playlist(db, "family", f"2026-10-03_family_{i}.jpg")
    for i in range(2):
        add_to_playlist(db, "ai-art", f"2026-10-03_comfy_{i}.jpg")


def add_to_playlist(db, slug: str, filename: str) -> None:
    """What the upload endpoint does: insert media, push to playlist, bump frames.

    Transaction so a frame never observes a manifest_version bump before the
    playlist item exists. Needs a replica set (Atlas qualifies; local mongod
    needs --replSet).
    """
    media = fake_media(filename, slug)
    with db.client.start_session() as s:
        with s.start_transaction():
            res = db.media.update_one(
                {"sha256": media["sha256"]}, {"$setOnInsert": media}, upsert=True, session=s
            )
            mid = res.upserted_id or db.media.find_one({"sha256": media["sha256"]}, {"_id": 1}, session=s)["_id"]
            pl = db.playlists.find_one_and_update(
                {"owner_id": OWNER, "slug": slug, "items.media_id": {"$ne": mid}},
                {
                    "$push": {"items": {"media_id": mid, "added_at": datetime.now(timezone.utc), "order": int(datetime.now().timestamp())}},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
                projection={"_id": 1},
                session=s,
            )
            if pl is None:
                print(f"{filename}: already in {slug} or playlist missing")
                return
            r = db.devices.update_many({"playlist_ids": pl["_id"]}, {"$inc": {"manifest_version": 1}}, session=s)
    print(f"+ {filename} -> {slug}; bumped {r.modified_count} device(s)")


def bump(db, device_id: str) -> None:
    r = db.devices.update_one({"_id": device_id}, {"$inc": {"manifest_version": 1}})
    print("bumped" if r.modified_count else "no such device")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", nargs=2, metavar=("SLUG", "FILENAME"))
    ap.add_argument("--bump", metavar="DEVICE_ID")
    args = ap.parse_args()

    uri = os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("set MONGO_URI (e.g. mongodb+srv://user:pass@cluster.mongodb.net/)")
    db = MongoClient(uri)[os.environ.get("MONGO_DB", "framesync")]

    if args.add:
        add_to_playlist(db, *args.add)
    elif args.bump:
        bump(db, args.bump)
    else:
        for name, schema in [("media", MEDIA_SCHEMA), ("playlists", PLAYLIST_SCHEMA), ("devices", DEVICE_SCHEMA)]:
            ensure_collection(db, name, schema)
        ensure_indexes(db)
        sample_data(db)


if __name__ == "__main__":
    main()
