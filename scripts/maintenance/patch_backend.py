"""Disabled legacy patch script.

This file used to rewrite database.py/main.py and inject stale Jellyfin delete
logic. Keeping an auto-patcher in the repository is dangerous: importing or
running it can silently overwrite newer, safer implementations.
"""


def main() -> int:
    print("patch_backend.py is disabled. Use normal code review and apply_patch edits instead.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
