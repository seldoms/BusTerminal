"""Disabled legacy source patcher.

This script used to rewrite main.py to add a global statement. That kind of
one-off patcher is unsafe to keep active in a maintained repository.
"""


def main() -> int:
    print("fix_global_ref.py is disabled. Inspect main.py and apply reviewed edits instead.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
