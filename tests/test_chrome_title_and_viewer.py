"""站点 chrome 标题拒绝、Apple 式多岗枚举、Viewer 筛选 placeholder。"""

from pathlib import Path

from app.collector.adapters.generic import parse_generic
from app.collector.adapters.router import parse_html
from app.collector.portal_nav import enumerate_detail_link_jobs

FIXTURES = Path(__file__).parent / "fixtures"


def test_apple_search_enumerates_individual_jobs():
    html = (FIXTURES / "apple_search_list.html").read_text(encoding="utf-8")
    posts = enumerate_detail_link_jobs("https://jobs.apple.com/zh-cn/search", html)
    titles = [p.title for p in posts]
    assert "CN-Store Leader" in titles
    assert "CN-Business Expert" in titles
    assert "CN-Specialist" in titles
    assert all("Search Jobs" not in (t or "") for t in titles)
    assert all("招贤纳才" not in (t or "") for t in titles)
    assert len(posts) == 3
    assert all("/details/" in (p.apply_url or "") for p in posts)


def test_apple_detail_strips_chrome_title():
    html = (FIXTURES / "apple_job_detail.html").read_text(encoding="utf-8")
    result = parse_generic("https://jobs.apple.com/zh-cn/details/114438029/cn-store-leader", html)
    assert result.title
    assert result.title.startswith("CN-Store Leader")
    assert "招贤纳才" not in result.title
    assert "Search Jobs" not in result.title
    # 导航词不得当 base 地
    assert result.work_location != "团队"
    assert result.work_location and (
        "上海" in result.work_location or "中国" in result.work_location
    )


def test_parse_html_search_page_not_single_chrome_row():
    html = (FIXTURES / "apple_search_list.html").read_text(encoding="utf-8")
    result = parse_html("https://jobs.apple.com/zh-cn/search?location=china-CHNC", html)
    # 整页解析不得把 Search Jobs chrome 当作岗位名称
    assert not result.title or "Search Jobs" not in result.title
    assert not result.title or "招贤纳才" not in result.title


def test_viewer_my_pick_controls():
    src = Path(__file__).resolve().parents[1] / "app" / "ui" / "viewer.py"
    text = src.read_text(encoding="utf-8")
    assert 'text="加入个人校招投递"' in text
    assert 'text="移出个人校招投递"' in text
    assert "MY_PICK_CAMPUS" in text
    assert "my_pick_only" in text

    src = Path(__file__).resolve().parents[1] / "app" / "ui" / "viewer.py"
    text = src.read_text(encoding="utf-8")
    expected = (
        "关键词（公司/岗位）",
        "企业性质",
        "行业",
        "base地",
    )
    for ph in expected:
        assert f'placeholder_text="{ph}"' in text, ph
    assert 'text="筛选条件"' in text
    for label in ("关键词", "企业性质", "行业", "base 地"):
        assert f'add_filter_field("{label}")' in text, label
    assert 'text="清空"' in text
    assert 'text="打开原文"' not in text
    assert 'placeholder_text="届别"' not in text
    assert '"应届生实习"' in text
    assert 'justify="center"' in text
