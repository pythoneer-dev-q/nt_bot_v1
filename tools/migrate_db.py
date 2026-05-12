from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.db import db
from database.migration import export_database, import_database


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Export or import Mongo data for DC migration.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export configured Mongo database.")
    export_parser.add_argument("--out", default=None, help="Target directory. Defaults to BACKUP_DIR.")

    import_parser = subparsers.add_parser("import", help="Import a previous export directory.")
    import_parser.add_argument("path", help="Export directory containing *.json.gz files.")
    import_parser.add_argument("--replace", action="store_true", help="Delete collection data before importing.")

    args = parser.parse_args()

    await db.connect()
    try:
        if args.command == "export":
            path = await export_database(args.out)
            print(path)
        elif args.command == "import":
            collections = await import_database(args.path, replace_existing=args.replace)
            print("Imported collections:", ", ".join(collections))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
