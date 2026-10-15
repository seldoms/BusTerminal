"""库内敏感配置的加密与「明文审计」（KI-044 / KI-048）。

回归点：`amane_token` 曾经不在 `SENSITIVE_CONFIG_KEYS` 里 —— 即使配了
`BUS_TERMINAL_CONFIG_SECRET`，它也照样明文入库。这里把"清单里必须有它"
以及"审计能如实点名明文键"一起锁死。
"""

import config
import database as database_module


def _fresh_db(tmp_path, monkeypatch):
    """拿一个只属于本用例的 sqlite 库，避免动到会话级 `database.db`。"""
    db_path = tmp_path / "config_audit.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return database_module.Database()


def _raw_value(db, key):
    """直接读 config 表里那一列，绕过解密，用来断言"落库到底是不是密文"。"""
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        db._release_connection(conn)


def test_amane_token_is_treated_as_sensitive():
    assert "amane_token" in database_module.SENSITIVE_CONFIG_KEYS


def test_amane_token_is_encrypted_at_rest_when_secret_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv(database_module.CONFIG_SECRET_ENV, "unit-test-config-secret")
    db = _fresh_db(tmp_path, monkeypatch)

    db.set_config("amane_token", "amane-plaintext-token")

    assert database_module._is_encrypted_config_value(_raw_value(db, "amane_token"))
    # 加密不能影响读出来的值
    assert db.get_config("amane_token") == "amane-plaintext-token"


def test_audit_names_keys_left_in_plaintext_without_secret(tmp_path, monkeypatch):
    monkeypatch.delenv(database_module.CONFIG_SECRET_ENV, raising=False)
    db = _fresh_db(tmp_path, monkeypatch)

    db.set_config("amane_token", "amane-plaintext-token")
    db.set_config("qb_password", "qb-plaintext-pass")
    db.set_config("jellyfin_host", "http://jellyfin.local:8096")  # 非敏感键，不该被点名

    assert db.plaintext_sensitive_config_keys() == ["amane_token", "qb_password"]


def test_audit_reports_legacy_plaintext_even_after_secret_is_added(tmp_path, monkeypatch):
    """密钥是后补的场景：旧值不会自动加密，必须仍然被点名。"""
    monkeypatch.delenv(database_module.CONFIG_SECRET_ENV, raising=False)
    db = _fresh_db(tmp_path, monkeypatch)
    db.set_config("qb_password", "legacy-qb-pass")

    monkeypatch.setenv(database_module.CONFIG_SECRET_ENV, "unit-test-config-secret")
    assert database_module.config_encryption_enabled() is True
    db.set_config("jellyfin_api_key", "fresh-key")

    # 新写的已加密、旧值仍是明文
    assert database_module._is_encrypted_config_value(_raw_value(db, "jellyfin_api_key"))
    assert db.plaintext_sensitive_config_keys() == ["qb_password"]
