from unittest.mock import MagicMock, patch

from app.collector.link_health import looks_like_legacy_zhiye_url, resolve_zhiye_apply_url


def test_legacy_zhiye_url_is_upgraded_from_api_row():
    response = MagicMock()
    response.json.return_value = {"Data": [{"Id": "abcdef12-3456-7890-abcd-ef1234567890", "JobAdId": 123456789}]}
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    url = "https://demo.zhiye.com/campus/jobs/abcdef12-3456-7890-abcd-ef1234567890"
    with patch("app.collector.link_health.httpx.Client", return_value=client):
        assert resolve_zhiye_apply_url(url) == "https://demo.zhiye.com/campus/detail?jobAdId=123456789"
    assert looks_like_legacy_zhiye_url(url)


def test_non_legacy_url_is_unchanged():
    url = "https://demo.zhiye.com/campus/detail?jobAdId=123"
    assert resolve_zhiye_apply_url(url) == url
    assert not looks_like_legacy_zhiye_url(url)
