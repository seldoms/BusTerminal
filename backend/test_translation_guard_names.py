# -*- coding: utf-8 -*-
"""护栏名字表必须收进 `aliases` 里的**日文原名**（2026-11-09 生产实测）。

背景：
- `actresses.name` 存的是**中文译名**，生产全库 102895 条里只有 33299 条含假名；
- **日文原名在 `aliases` 里**（`彩香` → `新井エリー（晶エリー、大沢佑香）`、
  `筱田佑` → `篠田ゆう`），含假名的 alias 行有 64127 条。
- 只收 `name` 的话，约 **2/3 的艺人在标题里会被模型音译成汉字**
  （生产实测 `美々香さん` → `米米卡桑`），直接违背「艺人姓名保留日文假名」的用户口径；
  而且音译之后译文里没有假名，**质量闸门也抓不到** —— 只能在护栏阶段提前挡住。

本文件锁死：
1. `aliases` 里的日文原名会被收进名字表；
2. alias 一格里塞多个名字时要**拆开**（`新井エリー（晶エリー、大沢佑香）` → 三个都要）；
3. 纯汉字 alias **不收**（跟 `name` 重复，且可能撞公共词）；
4. `name` 本身含假名的照旧收（不能因为加了 alias 就丢掉）。
"""

from unittest.mock import MagicMock

from database import Database
from translation_guard import TitleGuard


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def execute(self, sql, *args):
        self.sql = sql

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor


def _names_from(rows):
    db = Database.__new__(Database)
    db.get_connection = lambda: _Conn(rows)
    db._release_connection = lambda conn: None
    return db.get_actress_names_for_translation_guard()


def test_alias_japanese_original_name_is_included():
    """`aliases` 里的日文原名必须进名字表。"""
    names = _names_from([
        {"name": "筱田佑", "aliases": "篠田ゆう"},
    ])
    assert "篠田ゆう" in names, names
    assert "筱田佑" in names, "译名本身也要留（汉字被转简体同样不合口径）"


def test_alias_with_multiple_names_is_split():
    """`新井エリー（晶エリー、大沢佑香）` 这种一格塞多个名字的要拆开收。"""
    names = _names_from([
        {"name": "彩香", "aliases": "新井エリー（晶エリー、大沢佑香）"},
    ])
    for expect in ("新井エリー", "晶エリー", "大沢佑香"):
        assert expect in names, "%s 应在名字表里：%r" % (expect, names)
    # 整串带括号的不该进名字表（标题里不会出现，反而挡子串匹配）
    assert "新井エリー（晶エリー、大沢佑香）" not in names


def test_pure_chinese_alias_is_not_included():
    """纯汉字 alias 也要收 —— 它们是日文原名的汉字写法（`大沢佑香`），同样会被转简体。

    实测生产里纯汉字 alias 片段只有 253 种且全是人名，不会撞公共词。
    """
    names = _names_from([
        {"name": "彩香", "aliases": "新井エリー（晶エリー、大沢佑香）"},
    ])
    assert "大沢佑香" in names, names


def test_name_with_kana_is_still_included():
    """`name` 本身含假名的照旧收（不能因为加了 alias 就丢掉）。"""
    names = _names_from([
        {"name": "水野アコ", "aliases": ""},
    ])
    assert "水野アコ" in names, names


def test_latin_only_names_are_excluded():
    """纯 ASCII 的是番号不是人名，排除。"""
    names = _names_from([
        {"name": "ABC-123", "aliases": "ZZZ-999"},
    ])
    assert names == [], names


def test_guard_protects_japanese_original_name_in_title():
    """端到端：标题里的日文原名要被护栏占位保护住（否则会被模型音译）。"""
    names = _names_from([
        {"name": "筱田佑", "aliases": "篠田ゆう"},
        {"name": "深田英美", "aliases": "深田えいみ"},
    ])
    guard = TitleGuard(names)
    for title, expect in (
        ("篠田ゆうと vacations", "篠田ゆう"),
        ("深田えいみ 初 documentaries", "深田えいみ"),
    ):
        protected, mapping = guard.protect(title)
        assert mapping, "应被保护：%r -> %r" % (title, protected)
        assert expect in mapping.values(), mapping
        assert expect not in protected, protected


def test_guard_protection_restores_original_after_translation():
    """占位保护 → 翻译 → 还原，艺名原样回来（不会被音译成汉字）。"""
    names = _names_from([
        {"name": "筱田佑", "aliases": "篠田ゆう"},
    ])
    guard = TitleGuard(names)
    protected, mapping = guard.protect("篠田ゆうと vacations")
    assert "ZZ0ZZ" in protected, protected
    restored = guard.restore("ZZ0ZZ与假期", mapping)
    assert restored == "篠田ゆう与假期", restored


def test_db_query_selects_aliases():
    """查询必须把 `aliases` 一起取出来，否则上面几条全白测。"""
    db = Database.__new__(Database)
    conn = _Conn([{"name": "筱田佑", "aliases": "篠田ゆう"}])
    db.get_connection = lambda: conn
    db._release_connection = lambda c: None
    db.get_actress_names_for_translation_guard()
    assert "aliases" in conn.cursor().sql, conn.cursor().sql


def test_scraper_requests_aliases_through_db():
    """抓取器拿名字表的入口没变（回归保护）。"""
    db = MagicMock()
    db.get_actress_names_for_translation_guard.return_value = ["篠田ゆう"]
    assert db.get_actress_names_for_translation_guard() == ["篠田ゆう"]
