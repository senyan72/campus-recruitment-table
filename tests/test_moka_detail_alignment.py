from app.collector.adapters.base import ParseResult
from app.collector.fill_from_url import FillCandidate, _moka_direct_candidate_from_context


def test_moka_detail_missing_from_first_batch_keeps_original_uuid():
    detail_url = (
        "https://app.mokahr.com/campus-recruitment/aftershokzhr/36940"
        "#/job/009217cf-2040-4369-b15f-5f332d2dad41"
    )
    context = FillCandidate(
        fields={"title": "structure", "source_url": detail_url, "apply_url": detail_url},
        label="structure",
        summary="",
        needs_fetch=True,
        detail_url=detail_url,
        list_hint=ParseResult(
            extras={
                "adapter": "moka",
                "aes_iv": "de7c21ed8d6f50fe",
                "org_id": "aftershokzhr",
                "site_id": 36940,
            }
        ),
    )
    direct = _moka_direct_candidate_from_context([context], detail_url)
    assert direct is not None
    assert direct.detail_url == detail_url
    assert direct.list_hint is not None
    assert direct.list_hint.extras["job_id"] == "009217cf-2040-4369-b15f-5f332d2dad41"
