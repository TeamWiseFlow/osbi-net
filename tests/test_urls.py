"""Transport URL canonicalization: strip volatile tracking, keep identity."""
from osbi_contracts.urls import canonical_url


def test_wechat_article_keeps_only_identity_parameters():
    url = ("https://mp.weixin.qq.com/s?__biz=MzkzODczOTc0Ng==&mid=2247515861"
           "&idx=1&sn=7f6003cc411664120d78241b52a2bf05"
           "&poc_token=HEqKBWujXpxhDW4MweAFhfrtCRjTZ7fs1wPwXdRt"
           "&chksm=abc&scene=21#wechat_redirect")
    assert canonical_url(url) == (
        "https://mp.weixin.qq.com/s?__biz=MzkzODczOTc0Ng%3D%3D&mid=2247515861"
        "&idx=1&sn=7f6003cc411664120d78241b52a2bf05")


def test_other_urls_drop_tracking_and_anti_bot_tokens():
    assert canonical_url(
        "https://example.org/a?x=1&poc_token=t&captcha_token=c&utm_source=x&y=2"
    ) == "https://example.org/a?x=1&y=2"
