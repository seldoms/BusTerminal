import sys
import os
import getpass

RESET_PASSWORD_CONFIRM_ENV = "BUS_TERMINAL_CONFIRM_RESET_PASSWORD"
RESET_PASSWORD_CONFIRM_PHRASE = "RESET_USER_PASSWORD"


def reset_password(username, new_password):
    """重置指定用户的密码。"""
    if os.getenv(RESET_PASSWORD_CONFIRM_ENV) != RESET_PASSWORD_CONFIRM_PHRASE:
        print(
            "Password reset skipped. "
            f"Set {RESET_PASSWORD_CONFIRM_ENV}={RESET_PASSWORD_CONFIRM_PHRASE} to update the database."
        )
        return False

    print(f"正在重置用户 '{username}' 的密码...")
    
    try:
        from database import db
        from security import hash_password

        if not db.update_user_password(username, hash_password(new_password)):
            print(f"错误: 用户 '{username}' 不存在！")
            return False

        print(f"成功: 用户 '{username}' 的密码已重置。")
        return True
    except Exception as e:
        print(f"重置失败: {type(e).__name__}")
        return False


def read_new_password():
    env_password = os.getenv("BUS_TERMINAL_RESET_PASSWORD")
    if env_password:
        return env_password

    first = getpass.getpass("请输入新密码: ")
    second = getpass.getpass("请再次输入新密码: ")
    if first != second:
        print("错误: 两次输入的密码不一致。")
        return ""
    return first


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python reset_password.py <username>")
        print("Set BUS_TERMINAL_RESET_PASSWORD for non-interactive use.")
        print(f"Set {RESET_PASSWORD_CONFIRM_ENV}={RESET_PASSWORD_CONFIRM_PHRASE} to confirm the database update.")
        sys.exit(1)
        
    username = sys.argv[1]
    new_password = read_new_password()
    if not new_password:
        sys.exit(1)
    
    raise SystemExit(0 if reset_password(username, new_password) else 1)
