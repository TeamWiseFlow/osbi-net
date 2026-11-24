"""Source model validation for the mp (WeChat Official Account) kind."""
import pytest
from pydantic import ValidationError

from osbi_client.models import Source

SENTINEL = "https://mp.weixin.qq.com/"


def mp_source(**overrides):
    base = dict(id="mp-economy", url=SENTINEL, kind="mp", accounts=["第一财经", "经济日报"])
    base.update(overrides)
    return Source(**base)


def test_mp_source_with_accounts_and_sentinel_validates():
    source = mp_source()
    assert source.kind == "mp"
    assert source.accounts == ["第一财经", "经济日报"]
    assert source.url == SENTINEL


def test_mp_requires_non_empty_accounts():
    with pytest.raises(ValidationError):
        mp_source(accounts=[])


def test_mp_rejects_non_empty_steps():
    with pytest.raises(ValidationError):
        mp_source(steps=[{"action": "navigate", "url": "https://mp.weixin.qq.com/"}])


def test_web_rejects_accounts():
    with pytest.raises(ValidationError):
        Source(id="web-1", url="https://example.com/news", kind="web", accounts=["第一财经"])


def test_rss_rejects_accounts():
    with pytest.raises(ValidationError):
        Source(id="rss-1", url="https://example.com/feed.xml", kind="rss", accounts=["第一财经"])


def test_accounts_are_stripped_and_duplicates_rejected():
    source = mp_source(accounts=["  第一财经  ", "经济日报"])
    assert source.accounts == ["第一财经", "经济日报"]
    with pytest.raises(ValidationError):
        mp_source(accounts=["第一财经", "第一财经"])


def test_accounts_reject_blank_names():
    with pytest.raises(ValidationError):
        mp_source(accounts=["第一财经", "   "])


def test_web_and_rss_round_trip_unchanged():
    web = Source(id="web-1", url="https://example.com/news", kind="web")
    rss = Source(id="rss-1", url="https://example.com/feed.xml", kind="rss")
    assert web.accounts == []
    assert rss.accounts == []
    assert Source.model_validate(web.model_dump()).model_dump() == web.model_dump()
    assert Source.model_validate(rss.model_dump()).model_dump() == rss.model_dump()


def test_extra_field_still_forbidden():
    with pytest.raises(ValidationError):
        mp_source(unknown_field="x")
