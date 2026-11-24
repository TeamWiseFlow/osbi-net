"""Ported production domain policy: base-domain grouping, values and config compat."""
import pytest
from pydantic import ValidationError

from osbi_client.domain_policy import (DOMAIN_GAP_MS, MAX_CONCURRENCY, RETRY_DELAYS_MS,
                                       VERIFICATION_DELAYS_MS, base_domain, normalize_domain_limits)
from osbi_client.material_urls import forbidden_url
from osbi_client.models import ClientConfig


def test_base_domain_groups_second_level_registries():
    # common.mjs getBaseDomain: www stripped, three labels under second-level
    # registries, WeChat isolated from qq.com, IP hosts kept intact.
    assert base_domain("https://www.gov.cn/zhengce/") == "gov.cn"
    assert base_domain("https://fgj.sh.gov.cn/list") == "sh.gov.cn"
    assert base_domain("https://mp.weixin.qq.com/s?__biz=x") == "mp.weixin.qq.com"
    assert base_domain("https://www.example.com/a") == "example.com"
    assert base_domain("https://news.example.co.uk/a") == "example.co.uk"
    assert base_domain("http://127.0.0.1:8080/x") == "127.0.0.1"
    assert base_domain("not a url") == ""


def test_policy_values_match_production():
    # domain-policy.mjs loadPolicy(): concurrency 6, domainGapMs 1000,
    # retryDelaysMs [20000, 40000], verificationDelaysMs [60000].
    assert MAX_CONCURRENCY == 6
    assert DOMAIN_GAP_MS == 1000
    assert RETRY_DELAYS_MS == (20000, 40000)
    assert VERIFICATION_DELAYS_MS == (60000,)


def test_domain_keys_normalized_and_conflicts_rejected():
    assert normalize_domain_limits({"www.gov.cn": 3}) == {"gov.cn": 3}
    assert normalize_domain_limits({"mp.weixin.qq.com": 2}) == {"mp.weixin.qq.com": 2}
    with pytest.raises(ValueError):
        normalize_domain_limits({"gov.cn": 3, "www.gov.cn": 2})
    with pytest.raises(ValueError):
        normalize_domain_limits({"bad domain": 1})
    with pytest.raises(ValueError):
        normalize_domain_limits({"gov.cn/x": 1})
    with pytest.raises(ValueError):
        normalize_domain_limits({"gov.cn": 0})
    with pytest.raises(ValueError):
        normalize_domain_limits({"gov.cn": MAX_CONCURRENCY + 1})


def test_config_legacy_int_and_structured_policy():
    legacy = ClientConfig.model_validate({"domainConcurrency": 2})
    assert legacy.domainConcurrency.defaultConcurrency == 2
    assert legacy.domainConcurrency.domains == {}
    structured = ClientConfig.model_validate({"domainConcurrency": {
        "defaultConcurrency": 1, "domains": {"mp.weixin.qq.com": 2, "gov.cn": 3}}})
    assert structured.domainConcurrency.limit_for("mp.weixin.qq.com") == 2
    assert structured.domainConcurrency.limit_for("gov.cn") == 3
    assert structured.domainConcurrency.limit_for("example.com") == 1
    assert ClientConfig().domainConcurrency.defaultConcurrency == 1
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"domainConcurrency": {"defaultConcurrency": 0}})


def test_forbidden_fragments_cleaned_nonempty_and_unique():
    # Production config.py FORBIDDEN_DOMAINS validation: non-empty, unique.
    with pytest.raises(ValidationError):
        ClientConfig(forbiddenDomains=["a", "a"])
    with pytest.raises(ValidationError):
        ClientConfig(forbiddenDomains=[" "])
    assert ClientConfig(forbiddenDomains=[" gov.cn/home "]).forbiddenDomains == ["gov.cn/home"]


def test_forbidden_url_substring_matching():
    # material_urls.py forbidden_url: fragments may be domains or domain+path
    # prefixes; matching is a plain URL substring test, not host-endswith.
    fragments = ["www.gov.cn/home", "english.shanghai.gov.cn/"]
    assert forbidden_url("https://www.gov.cn/home/x", fragments)
    assert forbidden_url("https://www.gov.cn/home", fragments)
    assert not forbidden_url("https://www.gov.cn/zhengce/x", fragments)
    assert forbidden_url("https://english.shanghai.gov.cn/", fragments)
    assert not forbidden_url("https://www.gov.cn/fuwu/jiucuo", ["www.gov.cn/home"])
    assert forbidden_url("https://www.gov.cn/fuwu/jiucuo", ["www.gov.cn/fuwu/jiucuo"])
    assert not forbidden_url("https://example.org/", [])
