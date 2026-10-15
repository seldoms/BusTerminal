"""Disabled legacy SQL patcher.

This script used to rewrite database.py in place. Source edits must go through
normal review and apply_patch changes so the diff is explicit and testable.
"""


def main() -> int:
    print("patch_query.py is disabled. Review database.py and apply explicit patches instead.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
