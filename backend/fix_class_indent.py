"""Legacy one-off diagnostic for an old database.py indentation bug.

This script used to rewrite database.py in place. That is too risky for a
maintenance helper, especially because the old patch body contained stale
delete logic. Keep it read-only so importing or running it cannot change code.
"""
from pathlib import Path


DB_FILE = Path(__file__).resolve().with_name("database.py")
ORPHAN_MARKER = "Global instance used throughout the project\n\n    def get_library_movies"


def check_class_indent() -> int:
    content = DB_FILE.read_text(encoding="utf-8")
    if ORPHAN_MARKER in content:
        print("检测到历史缩进问题：get_library_movies 仍在 Database 类外。")
        print("此脚本已改为只读诊断，不再自动重写源码。请在 database.py 中手工修复后再验证。")
        return 1

    print("状态正常，未发现历史缩进问题。")
    print("当前文件末尾：")
    print(content[-200:])
    return 0


if __name__ == "__main__":
    raise SystemExit(check_class_indent())
