# -*- coding: utf-8 -*-
"""W2 接线验证: jellyfin_service / scrape_monitor / main 的番号提取全部走 number_parsing.

直接调未绑定方法 (这些方法只读参数, 不访问 self), 避免构造服务实例带来的外部依赖.
"""
import pytest

from jellyfin_service import JellyfinService
from scrape_monitor import JellyfinCleanupTask, movie_label_for_log

# (输入, 期望番号); None 表示不得返回番号 (不允许把标题原文冒充番号)
CASES = [
    ("ABC-123", "ABC-123"),
    ("abc-123", "ABC-123"),
    ("ABP-999 某标题", "ABP-999"),
    ("SSIS-001-C", "SSIS-001"),
    # 旧正则会把 PPV-1234567 / FC2PPV-1234567 当成番号
    ("FC2-PPV-1234567", "FC2-1234567"),
    ("FC2PPV-1234567", "FC2-1234567"),
    ("HEYZO-1234", "HEYZO-1234"),
    ("300MIUM-123", "300MIUM-123"),
    ("010115-001", "010115-001"),
    # 欧美日期号与国产 MD 系: 旧正则前者认不出, 后者会把 -1 当盘符剥掉
    ("Vixen.23.05.18", "Vixen.23.05.18"),
    ("MD0165-1", "MD0165-1"),
    ("MKY-AB-123", "MKY-AB-123"),
    ("[字幕组] ABC-123 1080p", "ABC-123"),
    ("/hp/ABC-123/ABC-123.mp4", "ABC-123"),
    ("ABC123", "ABC-123"),
    ("", None),
    ("某标题", None),
    ("我的女友 2023", None),
    ("1080p 高清 电影", None),
]


@pytest.mark.parametrize(("name", "expected"), CASES)
def test_jellyfin_service_extract(name, expected):
    assert JellyfinService.extract_movie_id_from_name(None, name) == expected


@pytest.mark.parametrize(("name", "expected"), CASES)
def test_cleanup_task_extract(name, expected):
    # 清理候选落库用空串表示未识别, 与 None 同义
    assert JellyfinCleanupTask._extract_movie_id(None, name) == (expected or "")


def test_movie_label_for_log_uses_number_only():
    assert movie_label_for_log("ABP-999 某标题") == "ABP-999"
    # 认不出番号时不回退原文, 落到脱敏摘要
    assert movie_label_for_log("某标题").startswith("<redacted ")
