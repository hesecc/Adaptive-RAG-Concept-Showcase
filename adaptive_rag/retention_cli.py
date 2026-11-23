"""Offline maintenance and cooperating foreground writer entry points (Linux)."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys


@contextmanager
def directory_lock(directory, *, writer):
    # The directory inode, unlike a lock file, cannot be unlinked/replaced on the mount.
    import fcntl

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        flags = fcntl.LOCK_SH if writer else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        yield fd
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("writer").add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("purge").add_argument("--exclusive-deployment", required=True,
                                               action="store_true", help="see deploy/retention/README.md")
    args = parser.parse_args(argv)
    root = Path(os.environ.get("RAG_DATA_DIR", ""))
    if not root.is_absolute() or root != root.resolve() or not root.is_dir():
        parser.error("RAG_DATA_DIR must name an existing absolute, non-linked data directory")
    try:
        if args.action == "writer":
            with directory_lock(root, writer=True) as fd:
                if not args.command:
                    parser.error("writer requires a foreground command")
                os.set_inheritable(fd, True)
                os.execvp(args.command[0], args.command)
        from .retention import run_offline
        print(json.dumps(run_offline(root, os.environ), sort_keys=True))
    except BlockingIOError:
        print("retention: writer or maintenance lock busy", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never echo SQL parameters, DSNs or content.
        print(f"retention: failed ({type(exc).__name__}); see maintenance runbook", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
