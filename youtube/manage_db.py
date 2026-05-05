#!/usr/bin/env python3
import sys
import argparse
from app.config import get_settings
from app.database import Database

def main():
    parser = argparse.ArgumentParser(description="YouTube Downloader Database Management CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List command
    list_parser = subparsers.add_parser("list", help="List requests")
    list_parser.add_argument("--status", help="Filter by status (pending, completed, failed, processing)")
    list_parser.add_argument("--limit", type=int, default=20, help="Limit number of items (default 20)")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add a new request")
    add_parser.add_argument("--type", choices=["track", "album"], required=True)
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--artist")
    add_parser.add_argument("--album")
    add_parser.add_argument("--year", type=int)
    add_parser.add_argument("--force", action="store_true", help="Force YouTube download")

    # Delete command
    del_parser = subparsers.add_parser("delete", help="Delete a request")
    del_parser.add_argument("id", type=int, help="ID of the request to delete")

    # Reset command
    subparsers.add_parser("reset", help="Reset all failed requests to pending")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    settings = get_settings()
    
    with Database(settings) as db:

        if args.command == "list":
            items = db.list_requests(status=args.status, limit=args.limit)
            if not items:
                print("No requests found.")
            else:
                print(f"{'ID':<5} {'Type':<8} {'Status':<12} {'Title':<30} {'Artist':<20}")
                print("-" * 75)
                for it in items:
                    print(f"{it['id']:<5} {it['type']:<8} {it['status']:<12} {it['title'][:30]:<30} {(it['artist'] or '' )[:20]:<20}")

        elif args.command == "add":
            import uuid
            key = f"cli|{uuid.uuid4()}"
            db.upsert_request(
                key=key,
                type=args.type,
                title=args.title,
                artist=args.artist,
                album=args.album,
                year=args.year,
                force_youtube=args.force
            )
            print(f"Successfully added {args.type}: {args.title}")

        elif args.command == "delete":
            if db.delete_request(args.id):
                print(f"Deleted request {args.id}")
            else:
                print(f"Failed to delete request {args.id}")

        elif args.command == "reset":
            if db.reset_failed_requests():
                print("All failed requests have been reset to pending.")
            else:
                print("Failed to reset requests.")

if __name__ == "__main__":
    main()
