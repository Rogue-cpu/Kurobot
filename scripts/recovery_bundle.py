"""Operator CLI: create, verify, and restore Kuro without exposing credentials."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recovery_backup import create_bundle, verify_bundle, restore_bundle, resume_restored,inspect_data


def main():
    parser = argparse.ArgumentParser(description="Kuro portable recovery bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--output", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("bundle")
    restore = sub.add_parser("restore")
    restore.add_argument("bundle")
    restore.add_argument("--destination", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--data-dir", required=True)
    resume.add_argument("--acknowledge-pending-actions", action="store_true", required=True)
    inspect=sub.add_parser("inspect")
    inspect.add_argument("--data-dir",required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = create_bundle(args.data_dir, args.output)
    elif args.command == "verify":
        result = verify_bundle(args.bundle)
    elif args.command == "restore":
        result = restore_bundle(args.bundle, args.destination)
    elif args.command == "inspect":
        print(json.dumps(inspect_data(args.data_dir),indent=2))
        return
    else:
        resume_restored(args.data_dir)
        print("Restore pause cleared. Start exactly one bot after retiring the old process.")
        return
    print(json.dumps({"created_utc": result["created_utc"], "saved_files": len(result["files"]), "missing_attachments": len(result["missing_attachments"]), "paused": args.command == "restore"}))


if __name__ == "__main__":
    main()
