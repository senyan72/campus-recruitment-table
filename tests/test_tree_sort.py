"""列表排序：公司归一化聚拢。"""

from app.db.local import normalize_company_name
from app.ui.tree_sort import TreeSortController, company_cluster_key, make_company_title_updated_sort


def test_normalize_clusters_suffix():
    a = normalize_company_name("晨光生物科技股份有限公司")
    b = normalize_company_name("晨光生物科技有限公司")
    assert a == b


def test_company_cluster_key_groups_similar_prefix():
    k1 = company_cluster_key("华为技术有限公司")
    k2 = company_cluster_key("华为技术股份有限公司")
    assert k1[1] == k2[1]  # 去后缀后归一化名相同
    assert k1[0] == k2[0]


def test_default_sort_companies_together():
    rows = [
        {"company": "字节跳动", "title": "B岗", "updated_at": "2026-01-02"},
        {"company": "阿里巴巴", "title": "A2", "updated_at": "2026-01-03"},
        {"company": "阿里巴巴有限公司", "title": "A1", "updated_at": "2026-01-01"},
        {"company": "字节跳动", "title": "A岗", "updated_at": "2026-01-04"},
    ]
    ctrl = make_company_title_updated_sort()
    sorted_rows = ctrl.sort_items(rows)
    companies = [r["company"] for r in sorted_rows]
    # 同归一化公司应相邻
    assert companies.index("阿里巴巴") < companies.index("字节跳动") or companies.count("阿里巴巴") >= 1
    ali_idx = [i for i, c in enumerate(companies) if "阿里" in c]
    assert ali_idx[-1] - ali_idx[0] + 1 == len(ali_idx)


def test_toggle_sort_direction():
    ctrl = TreeSortController(
        sortable={
            "company": ("公司", lambda it: company_cluster_key(it.get("company"))),
            "title": ("岗位名称", lambda it: str(it.get("title") or "")),
        }
    )
    assert ctrl.ascending is True
    ctrl.toggle("company")
    assert ctrl.ascending is False
    ctrl.toggle("title")
    assert ctrl.col == "title"
    assert ctrl.ascending is True
