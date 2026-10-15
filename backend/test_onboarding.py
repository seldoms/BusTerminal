"""首次部署引导（onboarding）的行为约束测试。

重点保护两件容易悄悄坏掉的事：
1. 屏蔽词口径 —— 填 `vr` 必须真的挡住 VR/ＶＲ/VR-，且**不能**误伤正常片子。
2. 敏感项空值 —— 前端拿不到密钥明文，空提交不能把已有密钥擦掉。
"""
import pytest

from onboarding_service import (
    BLOCK_KEYWORDS_KEY,
    MAX_BLOCK_KEYWORDS,
    OnboardingService,
    _database_url_identity,
    _mask_database_url,
    cache_ttl_hours_to_seconds,
    match_block_keyword,
    normalize_block_keyword,
    parse_block_keywords,
)


class FakeDB:
    """只存内存的最小 db 替身，只实现引导服务用到的方法。"""

    def __init__(self, seed=None):
        self.store = dict(seed or {})

    def get_config(self, key):
        return self.store.get(key)

    # 引导页要回填 40+ 个配置键，真实实现走全表批量读（逐键读在 sqlite 下要 5秒）。
    # 替身必须跟上真实接口，否则测试会因桩缺方法而走到慢路径得出错误结论。
    def get_all_config(self):
        return dict(self.store)

    def set_config(self, key, value):
        self.store[key] = value

    def count_table_rows(self, table):
        return {"movies": 12, "actresses": 3, "translations": 1, "local_files": 0}.get(table, 0)

    def get_connection(self):
        raise AssertionError("引导逻辑不应直接开连接")


def _service(seed=None):
    db = FakeDB(seed)
    svc = OnboardingService(
        db=db,
        config=type("Cfg", (), {"DATABASE_URL": "sqlite:///data/movies.db", "MOVIE_DETAIL_CACHE_TTL_SECONDS": 21600})(),
        logger=type("Log", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None})(),
        is_valid_http_url=lambda value, **kw: str(value).startswith("http"),
        normalize_qb_root_path=lambda value: str(value) if str(value).startswith("/") else None,
        probe_qb=lambda: {"ok": True},
        probe_jellyfin=lambda: {"ok": True},
        probe_amane=lambda: {"ok": True},
        probe_translation=lambda payload: {"ok": True},
    )
    return svc


# ---------------------------------------------------------------- 归一化口径


@pytest.mark.parametrize("raw", ["VR", "vr", "ＶＲ", "ｖｒ", "VR-", "  V.R  ", "VR_", "Vr"])
def test_normalize_collapses_vr_variants(raw):
    assert normalize_block_keyword(raw) == "vr"


def test_normalize_keeps_distinct_words_distinct():
    # 不能把所有东西都归一成同一个词，否则 A/B 两个词会互相误伤
    assert normalize_block_keyword("4K") != normalize_block_keyword("VR")
    assert normalize_block_keyword("VRP") != normalize_block_keyword("VR")


def test_normalize_returns_empty_for_punctuation_only():
    assert normalize_block_keyword("---") == ""
    assert normalize_block_keyword("   ") == ""
    assert normalize_block_keyword(None) == ""


def test_parse_splits_dedupes_and_keeps_original_order():
    assert parse_block_keywords("VR\nvr, VR\n4k,,,;") == ["vr", "4k"]


def test_parse_accepts_list_form():
    assert parse_block_keywords(["VR", "4k"]) == ["vr", "4k"]


def test_parse_caps_keyword_count():
    many = "\n".join(f"kw{i}" for i in range(MAX_BLOCK_KEYWORDS + 50))
    assert len(parse_block_keywords(many)) == MAX_BLOCK_KEYWORDS


# ---------------------------------------------------------------- 匹配


def test_match_hits_normalized_variants():
    kw = parse_block_keywords("VR")
    for text in ("HD VR", "ＶＲ fisheye", "V-R-Test", "vr porn"):
        assert match_block_keyword([text], kw) == "vr", text


def test_match_does_not_hit_unrelated_titles():
    kw = parse_block_keywords("VR")
    assert match_block_keyword(["Tokyo Swimmer 1080p", "SSIS-123"], kw) is None


def test_match_returns_none_without_keywords():
    assert match_block_keyword(["VR anything"], []) is None


# ---------------------------------------------------------------- 下载闸门


def test_gate_blocks_configured_keyword():
    svc = _service({BLOCK_KEYWORDS_KEY: "vr"})
    result = svc.is_blocked("ABC-123", "HD VR Movie", None)
    assert result["blocked"] is True
    assert result["keyword"] == "vr"


def test_gate_allows_when_no_keywords_configured():
    # 默认部署不该有任何行为变化
    svc = _service()
    assert svc.is_blocked("HD VR Movie", None)["blocked"] is False


def test_gate_matches_on_movie_id_too():
    # 番号里带 VR 的片子同样要挡住（BPDVR0005 这种）
    svc = _service({BLOCK_KEYWORDS_KEY: "vr"})
    assert svc.is_blocked("BPDVR0005", "クイーンズブレイド", None)["blocked"] is True


# ---------------------------------------------------------------- 列表过滤


def test_filter_removes_blocked_and_keeps_rest():
    svc = _service({BLOCK_KEYWORDS_KEY: "vr"})
    kept, blocked = svc.filter_blocked_movies([
        {"id": "A-1", "title": "HD VR Movie"},
        {"id": "B-2", "title": "Tokyo Swimmer"},
        {"id": "C-3", "title": "VRP something"},
    ])
    assert [m["id"] for m in kept] == ["B-2"]
    assert blocked == 2


def test_filter_matches_actress_names():
    svc = _service({BLOCK_KEYWORDS_KEY: "vr"})
    kept, blocked = svc.filter_blocked_movies([
        {"id": "D-4", "title": "Normal title", "actresses": [{"name": "VR Tanaka"}]},
    ])
    assert kept == []
    assert blocked == 1


def test_filter_is_noop_without_keywords():
    svc = _service()
    movies = [{"id": "A-1", "title": "HD VR Movie"}]
    kept, blocked = svc.filter_blocked_movies(movies)
    assert kept == movies
    assert blocked == 0


# ---------------------------------------------------------------- 落库


def test_apply_stores_normalized_keywords():
    svc = _service()
    svc.apply({"block_keywords": "VR\nＶＲ, 4k"}, completed=False)
    assert svc.get_block_keywords() == ["vr", "4k"]


def test_apply_keeps_existing_secret_when_blank():
    # 引导页拿不到密钥明文，空提交绝不能把已有密钥擦掉
    svc = _service({"jellyfin_api_key": "old-secret", "amane_token": "old-token"})
    result = svc.apply({"jellyfin_api_key": "", "amane_token": ""}, completed=False)
    assert result["success"] is True
    assert "jellyfin_api_key" in result["skipped"]
    assert svc.db.get_config("jellyfin_api_key") == "old-secret"
    assert svc.db.get_config("amane_token") == "old-token"


def test_apply_writes_non_empty_secret():
    svc = _service()
    svc.apply({"jellyfin_api_key": "new-secret"}, completed=False)
    assert svc.db.get_config("jellyfin_api_key") == "new-secret"


def test_apply_marks_completion():
    svc = _service()
    assert svc.is_completed() is False
    svc.apply({"block_keywords": "vr"}, completed=True)
    assert svc.is_completed() is True


def test_apply_rejects_keys_outside_whitelist():
    # 防止前端传任意键污染 config 表
    svc = _service()
    result = svc.apply({"auth_secret": "hijack", "some_random_key": "x"}, completed=False)
    assert result["success"] is True
    assert svc.db.get_config("auth_secret") is None
    assert svc.db.get_config("some_random_key") is None


def test_apply_rejects_invalid_payload():
    svc = _service()
    assert svc.apply({"disk_threshold_gb": -5}, completed=False)["success"] is False
    assert svc.apply({"delivery_folder": "relative/path"}, completed=False)["success"] is False
    assert svc.apply({"translation_provider": "nope"}, completed=False)["success"] is False


def test_apply_writes_ttl_seconds_derived_key():
    svc = _service()
    svc.apply({"movie_detail_cache_ttl_hours": 12}, completed=False)
    assert svc.db.get_config("movie_detail_cache_ttl_seconds") == 12 * 3600


def test_reset_only_clears_completion_flag():
    svc = _service({BLOCK_KEYWORDS_KEY: "vr"})
    svc.apply({}, completed=True)
    assert svc.is_completed() is True
    svc.reset()
    assert svc.is_completed() is False
    # 业务配置不该被 reset 动
    assert svc.get_block_keywords() == ["vr"]


# ---------------------------------------------------------------- 设置回填


def test_current_settings_masks_secrets():
    svc = _service({"jellyfin_api_key": "super-secret"})
    payload = svc.current_settings()
    assert payload["jellyfin_api_key"] == ""
    assert payload["jellyfin_api_key_configured"] is True


def test_status_reports_completion_and_keywords():
    svc = _service()
    svc.apply({"block_keywords": "VR"}, completed=True)
    status = svc.status()
    assert status["completed"] is True
    assert status["block_keywords"] == ["vr"]


# ---------------------------------------------------------------- 数据库比对


def test_database_identity_ignores_credentials():
    a = _database_url_identity("postgresql://u:pw@host:5433/db")
    b = _database_url_identity("postgresql://other:pw2@host:5433/db")
    assert a == b


def test_database_identity_distinguishes_hosts():
    a = _database_url_identity("postgresql://u:pw@hostA:5433/db")
    b = _database_url_identity("postgresql://u:pw@hostB:5433/db")
    assert a != b


def test_mask_never_leaks_password():
    masked = _mask_database_url("postgresql://admin:SUPERSECRET@192.168.1.5:5433/db")
    assert "SUPERSECRET" not in masked
    assert "admin" not in masked
    assert "192.168.1.5" in masked


def test_database_check_flags_mismatch():
    svc = _service()
    svc.config.DATABASE_URL = "postgresql://u:pw@hostA:5433/db"
    result = svc.database_check("postgresql://u:pw@hostB:5433/db")
    assert result["matches_expected"] is False
    assert result["switch_requires_restart"] is True
    assert "DATABASE_URL" in result["hint"]


def test_database_check_accepts_match():
    svc = _service()
    svc.config.DATABASE_URL = "postgresql://u:pw@hostA:5433/db"
    result = svc.database_check("postgresql://u:pw@hostA:5433/db")
    assert result["matches_expected"] is True


# ---------------------------------------------------------------- TTL 换算


def test_ttl_hours_conversion_and_fallback():
    assert cache_ttl_hours_to_seconds(6, fallback=1) == 21600
    assert cache_ttl_hours_to_seconds("bad", fallback=99) == 99
    assert cache_ttl_hours_to_seconds(-3, fallback=77) == 77
    assert cache_ttl_hours_to_seconds(None, fallback=42) == 42


# ---------------------------------------------------------------- 探针分派


def test_test_target_rejects_unknown():
    svc = _service()
    assert svc.test_target("nope", {})["ok"] is False


def test_test_target_dispatches():
    svc = _service()
    assert svc.test_target("qb", {})["ok"] is True
    assert svc.test_target("translation", {"provider": "ollama_local"})["ok"] is True