"""Per-domain acquisition scheduling policy ported from production wiseflow.

Source of truth: camoufox_runtime/domain-policy.mjs (loadPolicy) and
common.mjs (getBaseDomain). Production reads DOMAIN_CONCURRENCY
{defaultConcurrency, domains} from the customer config, normalizes domain
keys to base domains, caps total scheduler concurrency at 6, keeps a 1s gap
between attempts on one domain, retries retryable capture failures after
20s/40s and cools non-WeChat verification domains down for 60s. The osbi
daemon applies the same values to its persistent run scheduler.

url_rules.json carries the two grouping lists verbatim from the production
wis/url_rules.json (separateDomains, secondLevelDomains).
"""
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

_RULES = json.loads(Path(__file__).with_name("url_rules.json").read_text())
SEPARATE_DOMAINS = frozenset(_RULES["separateDomains"])
SECOND_LEVEL_DOMAINS = frozenset(_RULES["secondLevelDomains"])

# domain-policy.mjs loadPolicy() frozen scheduler constants.
MAX_CONCURRENCY = 6
DOMAIN_GAP_MS = 1000
RETRY_DELAYS_MS = (20000, 40000)
VERIFICATION_DELAYS_MS = (60000,)
WECHAT_DOMAIN = "mp.weixin.qq.com"


def base_domain(url):
    """Port of common.mjs getBaseDomain: WeChat isolated from qq.com, IP hosts
    kept intact, www stripped, three labels under second-level registries
    (fgj.sh.gov.cn -> sh.gov.cn, www.gov.cn -> gov.cn)."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not host:
            return ""
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        if host in SEPARATE_DOMAINS:
            return host
        parts = host.removeprefix("www.").split(".")
        count = 3 if len(parts) > 2 and parts[-2] in SECOND_LEVEL_DOMAINS else 2
        return ".".join(parts[-count:])
    except (ValueError, TypeError):
        return ""


def normalize_domain_limits(domains):
    """Port of loadPolicy() key validation: positive integer limits, no URL-ish
    characters in keys, keys normalized to base domains, conflicting limits
    for one normalized domain rejected. Limits above the scheduler hard cap
    (6) are unreachable in production and rejected here as well."""
    normalized = {}
    for key, limit in domains.items():
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CONCURRENCY:
            raise ValueError(f"Invalid domain concurrency for {key}: must be 1-{MAX_CONCURRENCY}")
        if not key or re.search(r"[\s/@?#]", key):
            raise ValueError(f"Invalid domain/concurrency entry: {key}")
        domain = base_domain("https://" + key)
        if not domain:
            raise ValueError(f"Invalid domain: {key}")
        if domain in normalized and normalized[domain] != limit:
            raise ValueError(f"Conflicting limits for normalized domain: {domain}")
        normalized[domain] = limit
    return normalized
