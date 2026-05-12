from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import ReplaceOne

from config import settings
from database.db import db


MIGRATION_COLLECTIONS = (
    "users",
    "admins",
    "broadcasts",
    "channels",
    "ads",
    "support_tickets",
    "alerts",
)


def _backup_root() -> Path:
    path = Path(settings.backup_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


async def export_database(target_dir: str | Path | None = None) -> Path:
    assert db.database is not None
    root = Path(target_dir) if target_dir else _backup_root()
    root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_dir = root / f"{settings.mongo_db}_{stamp}"
    export_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "database": settings.mongo_db,
        "created_at": datetime.now(timezone.utc),
        "collections": list(MIGRATION_COLLECTIONS),
        "format": "mongo-json-util-gzip-v1",
    }
    for collection_name in MIGRATION_COLLECTIONS:
        cursor = db.database[collection_name].find({})
        documents = await cursor.to_list(length=None)
        with gzip.open(export_dir / f"{collection_name}.json.gz", "wt", encoding="utf-8") as file:
            file.write(json_util.dumps(documents, ensure_ascii=False, indent=2))

    with (export_dir / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump(json.loads(json_util.dumps(manifest)), file, ensure_ascii=False, indent=2)

    return export_dir


async def import_database(source_dir: str | Path, replace_existing: bool = False) -> list[str]:
    assert db.database is not None
    source = Path(source_dir)
    imported: list[str] = []

    for collection_name in MIGRATION_COLLECTIONS:
        path = source / f"{collection_name}.json.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as file:
            documents = json_util.loads(file.read())
        if not documents:
            imported.append(collection_name)
            continue

        collection = db.database[collection_name]
        if replace_existing:
            await collection.delete_many({})
            await collection.insert_many(documents)
        else:
            operations = [ReplaceOne({"_id": item["_id"]}, item, upsert=True) for item in documents]
            await collection.bulk_write(operations, ordered=False)
        imported.append(collection_name)

    await db.ensure_indexes()
    return imported
