import sqlite3
import os
import shutil
import time
import subprocess
import tempfile
from pathlib import Path

ALLOW_DESTRUCTIVE_MAINTENANCE = "BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE"
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"


def unsafe_path_output_enabled() -> bool:
    return os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def display_path(path: Path) -> str:
    path = Path(path)
    if unsafe_path_output_enabled():
        return str(path)
    return f"<path:{path.name}>"


def get_db_path() -> Path:
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH")).expanduser().resolve()
    data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return (data_dir / "movies.db").expanduser().resolve()


def full_integrity_check_enabled() -> bool:
    return os.getenv("BUS_TERMINAL_FULL_INTEGRITY_CHECK", "").lower() in {"1", "true", "yes", "on"}


def check_integrity(path: Path | None = None):
    db_path = path or get_db_path()
    pragma = "integrity_check" if full_integrity_check_enabled() else "quick_check"
    print(f"正在检查数据库完整性: {display_path(db_path)} ({pragma})...")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA {pragma}")
        result = cursor.fetchone()[0]
        conn.close()
        print(f"完整性检查结果: {result}")
        return result == "ok"
    except Exception as e:
        print(f"打开数据库失败: {e}")
        return False

def attempt_repair(path: Path | None = None):
    db_path = path or get_db_path()
    if os.getenv(ALLOW_DESTRUCTIVE_MAINTENANCE, "").lower() not in {"1", "true", "yes", "on"}:
        print(
            "Database repair skipped. "
            f"Set {ALLOW_DESTRUCTIVE_MAINTENANCE}=true to rewrite the database file."
        )
        return False

    print("尝试修复数据库...")
    backup_path = db_path.with_name(f"{db_path.name}.bak.{int(time.time())}")
    shutil.copy2(db_path, backup_path)
    print(f"已创建备份: {display_path(backup_path)}")
    
    try:
        with tempfile.TemporaryDirectory(prefix="busterminal_repair_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            sql_file = tmp_path / "repair.sql"
            new_db = tmp_path / "movies_new.db"

            with sql_file.open("w", encoding="utf-8") as output:
                subprocess.run(["sqlite3", str(db_path), ".dump"], stdout=output, check=True)

            with sql_file.open("r", encoding="utf-8") as input_file:
                subprocess.run(["sqlite3", str(new_db)], stdin=input_file, check=True)

            if check_integrity_custom(new_db):
                shutil.move(str(new_db), db_path)
                print("修复成功！")
                return True

            print("修复后的数据库仍有问题。")
            return False
    except Exception as e:
        print(f"修复过程出错: {e}")
        return False

def check_integrity_custom(path):
    conn = None
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA quick_check")
        result = cursor.fetchone()[0]
        return result == "ok"
    except Exception:
        return False
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    if not check_integrity():
        # attempt_repair()
        print("数据库已损坏！建议删除 movies.db 并重启应用（数据会自动重新抓取）。")
        # 直接由用户决定是否删除。但在这种场景下，删除是最快的方案。
    else:
        print("数据库状态良好。")
