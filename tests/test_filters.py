from app.collector.filters import (
    clean_company_nature,
    extract_job_tags,
    is_aggregator_text,
    map_recruit_bucket,
    should_skip_sheet,
)


def test_skip_sheets():
    assert should_skip_sheet("表格说明")
    assert should_skip_sheet("内推码专栏")
    assert should_skip_sheet("【offer】求职进度管理表模板")
    assert not should_skip_sheet("春招汇总表")
    assert not should_skip_sheet("互联网+IT软件专栏")


def test_clean_company_nature():
    assert clean_company_nature("私企") == "私企"
    assert clean_company_nature("事业单位") == "事业单位"
    assert clean_company_nature("某某公司2025届校招正式启动") is None
    assert clean_company_nature("民营企业") == "民企"


def test_map_recruit_bucket():
    assert map_recruit_bucket("暑假实习") == "日常实习"
    assert map_recruit_bucket("2027届毕业实习") == "应届生实习"
    assert map_recruit_bucket("2025届校招/春招") == "校招"
    assert map_recruit_bucket(None, "秋招提前批启动") == "校招"
    assert map_recruit_bucket("日常实习") == "日常实习"


def test_aggregator_and_tags():
    assert is_aggregator_text("今日整理了 30 家校招")
    assert is_aggregator_text("海投助手每日岗")
    assert not is_aggregator_text("字节跳动校园招聘公告")
    assert "算法" in extract_job_tags("机器学习算法工程师")
    assert "前端" in extract_job_tags("Web前端开发实习生")
