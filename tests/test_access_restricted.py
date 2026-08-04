import pytest

from app.collector.adapters.router import AccessRestrictedError, access_restricted_reason
from app.collector.website import collect_company_careers


@pytest.mark.parametrize("status", [401, 403, 405, 429, 503])
def test_access_restricted_statuses_are_recognized(status: int):
    assert access_restricted_reason(status) == f"HTTP {status}"


def test_access_restricted_page_markers_are_recognized():
    assert access_restricted_reason(200, "<title>安全验证</title>")


def test_access_restricted_error_is_distinct_from_transport_failure():
    assert issubclass(AccessRestrictedError, RuntimeError)


def test_collect_company_records_and_stops_on_access_restriction(monkeypatch):
    class FakeDB:
        def __init__(self):
            self.reviews = []

        def list_blocklist(self):
            return []

        def job_identity_keys_for_company(self, company_name, company_id):
            return set(), set()

        def enqueue_review(self, kind, payload, reason):
            self.reviews.append({"kind": kind, "payload": payload, "reason": reason})

    db = FakeDB()
    company = {
        "id": "restricted-co",
        "name": "Restricted Co",
        "career_urls": ["https://restricted.example/campus", "https://restricted.example/jobs"],
    }
    calls: list[str] = []

    def blocked(url: str, timeout: float = 20.0) -> str:
        calls.append(url)
        raise AccessRestrictedError("疑似反爬/访问受限（HTTP 403）：" + url)

    monkeypatch.setattr("app.collector.website.fetch", blocked)
    stats = collect_company_careers(db, company, max_career_urls=2)

    assert calls == ["https://restricted.example/campus"]
    assert stats["access_restricted"] == 1
    review = db.reviews[0]
    assert review["kind"] == "access_restricted"
    assert review["reason"] == "疑似反爬/访问受限，已跳过该企业"
