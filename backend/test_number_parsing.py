# -*- coding: utf-8 -*-
# number_parsing.py 的移植验证测试: 覆盖番号识别、文件相位标记与比对函数.
import pytest

from number_parsing import (
    ContentType,
    Mosaic,
    detect_cd,
    extract_number,
    fold_number,
    is_same_name,
    is_same_number,
    leading_token,
    normalize_name,
    parse_file_info,
    parse_western_number,
    split_number,
)


@pytest.mark.parametrize(
    ("text", "number", "content_type"),
    [
        # 有码番号
        ("ABC-123", "ABC-123", ContentType.CENSORED),
        ("[ABC-123] 某标题", "ABC-123", ContentType.CENSORED),
        # FC2 (PPV 写法归一)
        ("FC2-PPV-1234567", "FC2-1234567", ContentType.FC2),
        ("FC2PPV-1234567", "FC2-1234567", ContentType.FC2),
        # 欧美日期号
        ("Vixen.23.05.18", "Vixen.23.05.18", ContentType.WESTERN),
        ("Blacked.2023.05.18", "Blacked.23.05.18", ContentType.WESTERN),
        # 国产 MD 系: -1 是番号一部分, 不能被当盘符剥掉
        ("MD0165-1", "MD0165-1", ContentType.CHINESE),
        # 无码
        ("HEYZO-1234", "HEYZO-1234", ContentType.UNCENSORED),
        ("010115-001", "010115-001", ContentType.UNCENSORED),
        # 素人
        ("300MIUM-123", "300MIUM-123", ContentType.AMATEUR),
    ],
)
def test_extract_number(text, number, content_type):
    info = parse_file_info(text=text)
    assert info.number == number
    assert info.content_type == content_type


def test_text_mode_miss_returns_none():
    # 文本模式未命中必须返回 None, 不得把原文当番号
    assert extract_number("这是一段普通的中文文本") is None
    assert extract_number("hello world plain words") is None
    assert parse_file_info(text="这是一段普通的中文文本").number is None


def test_prefix():
    assert parse_file_info(text="ABC-123").prefix == "ABC"
    assert parse_file_info(text="FC2-PPV-1234567").prefix == "FC2"
    assert split_number("ABC-123") == ("ABC", "123")


@pytest.mark.parametrize(
    ("filename", "cd"),
    [
        ("ABC-123-CD1", 1),
        ("ABC-123-cd2", 2),
        ("ABC-123-PART2", 2),
        ("ABC-123-A", 1),
        ("ABC-123-B", 2),
        ("ABC-123", None),
    ],
)
def test_detect_cd(filename, cd):
    assert detect_cd(filename) == cd


def test_md0165_tail_is_number_not_cd_strip():
    # MD0165-1: 番号保留 -1; 同时它也被正确识别为分集 1
    info = parse_file_info(text="MD0165-1")
    assert info.number == "MD0165-1"
    assert info.cd == 1


@pytest.mark.parametrize(
    ("filename", "has_subtitle"),
    [
        ("ABC-123-C", True),
        ("ABC-123-UC", True),
        ("ABC-123-中文字幕", True),
        ("ABC-123", False),
        ("ABC-123-CD1", False),  # -CD1 是分集, 不是字幕
    ],
)
def test_detect_subtitle(filename, has_subtitle):
    assert parse_file_info(text=filename).has_subtitle is has_subtitle


def test_uc_is_cracked_mosaic():
    assert parse_file_info(text="ABC-123-UC").mosaic == Mosaic.CRACKED
    assert parse_file_info(text="ABC-123-C").mosaic == Mosaic.CENSORED  # 有码类型默认填充
    assert parse_file_info(text="HEYZO-1234").mosaic == Mosaic.UNCENSORED  # 无码类型默认填充


@pytest.mark.parametrize(
    ("filename", "definition"),
    [
        ("ABC-123-4K", "4K"),
        ("ABC-123-2160p", "4K"),
        ("ABC-123-1080p", "1080p"),
        ("ABC-123-1080p60", "1080p"),
        ("ABC-123-720P", "720p"),
        ("ABC-123", None),
    ],
)
def test_detect_definition(filename, definition):
    info = parse_file_info(text=filename)
    assert info.definition == definition
    # 分辨率标记不得污染番号
    assert info.number == "ABC-123"


def test_parent_dir_fallback():
    # 文件名无番号时回退父目录
    info = parse_file_info("downloads/ABC-123/xxxx.mkv")
    assert info.number == "ABC-123"
    assert info.content_type == ContentType.CENSORED


def test_parent_dir_cd():
    info = parse_file_info("downloads/ABC-123/CD2/xxxx.mkv")
    assert info.number == "ABC-123"
    assert info.cd == 2


def test_mosaic_from_dir_token():
    info = parse_file_info("无码/HEYZO-1234/video.mkv")
    assert info.number == "HEYZO-1234"
    assert info.mosaic == Mosaic.UNCENSORED


def test_escape_strings():
    info = parse_file_info(text="ABC-123@ Sponsor", escape_strings=["@ Sponsor"])
    assert info.number == "ABC-123"


# ---------------------------------------------------------------------------
# 番号/名字比对函数 (来自 amane crawlers/parsing.py)
# ---------------------------------------------------------------------------


def test_fold_number():
    assert fold_number("ABC-123") == fold_number("abc 123")
    assert fold_number("010115_001") != fold_number("010115-001")  # _ 与 - 不等价


def test_is_same_number():
    assert is_same_number("ABC-123", "abc123")
    assert not is_same_number("010115_001", "010115-001")
    assert not is_same_number("", "ABC-123")
    # 欧美日期号: 2 位与 4 位年份写法同指一部
    assert is_same_number("Vixen.23.05.18", "vixen.2023.05.18")


def test_parse_western_number():
    result = parse_western_number("Vixen.23.05.18")
    assert result is not None
    assert result.studio == "vixen"
    assert result.date == "2023-05-18"
    assert result.alternate == "Vixen.2023.05.18"
    # 非法日期与普通番号返回 None
    assert parse_western_number("Vixen.23.13.40") is None
    assert parse_western_number("ABC-123") is None


def test_name_helpers():
    assert normalize_name(" ＡＢＣ ") == "ABC"  # NFKC 全角折半角 + 去首尾空白
    assert is_same_name("张三 ", "张三")
    assert not is_same_name("", "张三")
    assert leading_token("ABC-123 某标题") == "ABC-123"
    assert leading_token("") == ""
