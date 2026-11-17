"""Validated local capture and submission-cache policies."""
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from osbi_contracts.models import StrictModel


class CaptureCacheSettings(StrictModel):
    defaultTtlSeconds: int = Field(default=21600, ge=0, le=31536000)
    domainTtlSeconds: dict[str, int] = Field(default_factory=dict, max_length=200)
    maxEntries: int = Field(default=1000, ge=1, le=100000)
    maxBytes: int = Field(default=134217728, ge=1, le=2147483648)

    @model_validator(mode="after")
    def validate_domains(self):
        domains = {}
        for name, ttl in self.domainTtlSeconds.items():
            if type(ttl) is not int or not 0 <= ttl <= 31536000:
                raise ValueError("Domain cache TTL must be seconds between 0 and 31536000")
            if not name or any(c in name for c in "/:@?#* \t\n"):
                raise ValueError("Cache domains must be hostnames without ports or wildcard prefixes")
            host = name.rstrip('.').encode('idna').decode().lower()
            if not host or host in domains:
                raise ValueError("Cache domains must be unique")
            domains[host] = ttl
        self.domainTtlSeconds = domains
        return self

    def ttl(self, url):
        host = (urlsplit(url).hostname or '').encode('idna').decode().lower().rstrip('.')
        matches = [name for name in self.domainTtlSeconds if host == name or host.endswith('.' + name)]
        return self.domainTtlSeconds[max(matches, key=len)] if matches else self.defaultTtlSeconds


class SubmissionCacheSettings(StrictModel):
    ttlSeconds: int = Field(default=604800, ge=0, le=31536000)
    maxEntries: int = Field(default=10000, ge=1, le=100000)
    historyDays: int = Field(default=30, ge=1, le=3650)
