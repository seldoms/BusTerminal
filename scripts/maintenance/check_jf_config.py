def configured(value):
    return "configured" if str(value or "").strip() else "missing"


def check_jellyfin_config():
    from database import db

    print(f"Host: {configured(db.get_config('jellyfin_host'))}")
    print(f"Key: {configured(db.get_config('jellyfin_api_key'))}")
    print(f"User: {configured(db.get_config('jellyfin_user_id'))}")


if __name__ == "__main__":
    check_jellyfin_config()
