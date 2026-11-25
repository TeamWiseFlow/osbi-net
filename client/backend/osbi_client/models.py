"""Local configuration and bounded programmable workflows."""
import re
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, field_validator, model_validator
from osbi_contracts.models import StrictModel
from .domain_policy import MAX_CONCURRENCY, normalize_domain_limits
from .report_settings import ReportProfile, default_report_profiles
from .cache_settings import CaptureCacheSettings, SubmissionCacheSettings


class Step(StrictModel):
    action: Literal["navigate", "click", "fill", "select", "wait", "scroll", "switch", "capture", "wait_for_user"]
    url: str = Field(default="", max_length=4000)
    selector: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=4000)
    ref: str = Field(default="", max_length=100)
    frame: str = Field(default="", max_length=1000)
    ms: int = Field(default=1000, ge=0, le=30000)
    index: int = Field(default=0, ge=0, le=20)
    amount: int = Field(default=500, ge=-3000, le=3000)


class Source(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    url: str = Field(min_length=1, max_length=4000)
    kind: Literal["web", "rss", "mp"] = "web"
    enabled: bool = True
    intervalMinutes: int = Field(default=0, ge=0, le=525600)
    profile: str = Field(default="default", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    steps: list[Step] = Field(default_factory=list, max_length=40)
    accounts: list[str] = Field(default_factory=list, max_length=100)
    # Global server channels this source contributes to. Multiple channels
    # fan out as separate submissions; an empty list means acquisition only.
    channels: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("channels")
    @classmethod
    def clean_channels(cls, value):
        cleaned = [channel.strip() for channel in value]
        if any(not channel or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel)
               for channel in cleaned):
            raise ValueError("Channel IDs must use lowercase letters, digits, '-' or '_'")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Source channels must be unique")
        return cleaned

    @field_validator("accounts")
    @classmethod
    def clean_accounts(cls, value):
        cleaned = [name.strip() for name in value]
        if any(not name or len(name) > 200 for name in cleaned):
            raise ValueError("Account names must be 1-200 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Account names must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_url(self):
        from osbi_contracts.urls import canonical_url
        if not canonical_url(self.url):
            raise ValueError("Source requires an HTTP(S) URL")
        if self.kind == "mp":
            if not self.accounts:
                raise ValueError("mp source requires a non-empty account whitelist")
            if self.steps:
                raise ValueError("mp source does not support programmable steps")
        elif self.accounts:
            raise ValueError("accounts whitelist is only valid for mp sources")
        for step in self.steps:
            if step.action == "navigate" and not canonical_url(step.url):
                raise ValueError("Navigation steps require an HTTP(S) URL")
            if step.action in {"click", "fill", "select"} and not (step.selector or step.ref):
                raise ValueError("Interaction requires a selector or snapshot ref")
        return self


class JevConfig(StrictModel):
    enabled: bool = False
    baseUrl: str = "https://api.typesafe.ai/v1"
    apiKey: str = Field(default="", max_length=500)
    model: str = Field(default="jev-latest", min_length=1, max_length=200)
    postQuery: bool = True
    discardConfidence: float = Field(default=.8, ge=0, le=1)
    timeoutSeconds: int = Field(default=20, ge=1, le=120)

    @model_validator(mode="after")
    def validate_endpoint(self):
        self.baseUrl = self.baseUrl.strip().rstrip("/")
        self.model = self.model.strip()
        if not self.model:
            raise ValueError("Jev model is required")
        p = urlsplit(self.baseUrl)
        local = p.hostname in {"localhost", "127.0.0.1", "::1"}
        if not p.hostname or p.username or p.password or p.query or p.fragment or not (p.scheme == "https" or (local and p.scheme == "http")):
            raise ValueError("Jev endpoint requires HTTPS, or HTTP on loopback")
        if self.enabled and not local and not self.apiKey:
            raise ValueError("Enabled remote Jev requires its own API key")
        return self


class VisionConfig(StrictModel):
    enabled: bool = False
    baseUrl: str = "https://api.siliconflow.cn/v1"
    apiKey: str = Field(default="", max_length=500)
    model: str = Field(default="Qwen/Qwen3-VL-32B-Instruct", min_length=1, max_length=200)
    fallbackModel: str = Field(default="Qwen/Qwen3-VL-30B-A3B-Instruct", max_length=200)
    extraModel: str = Field(default="zai-org/GLM-4.5V", max_length=200)
    concurrency: int = Field(default=3, ge=1, le=6)
    timeoutSeconds: int = Field(default=240, ge=10, le=600)

    @model_validator(mode="after")
    def validate_endpoint(self):
        self.baseUrl = self.baseUrl.strip().rstrip("/")
        self.model = self.model.strip()
        self.fallbackModel = self.fallbackModel.strip()
        self.extraModel = self.extraModel.strip()
        if not self.model:
            raise ValueError("Vision model is required")
        p = urlsplit(self.baseUrl)
        local = p.hostname in {"localhost", "127.0.0.1", "::1"}
        if not p.hostname or p.username or p.password or p.query or p.fragment or not (p.scheme == "https" or (local and p.scheme == "http")):
            raise ValueError("Vision endpoint requires HTTPS, or HTTP on loopback")
        if self.enabled and not local and not self.apiKey:
            raise ValueError("Enabled remote vision requires its own API key")
        return self


class ReportLlmConfig(StrictModel):
    """Local report editor/writer model; production shadow uses xin_local."""
    enabled: bool = True
    baseUrl: str = "http://127.0.0.1:9997/v1"
    apiKey: str = Field(default="", max_length=500)
    model: str = Field(default="xin_local", min_length=1, max_length=200)
    timeoutSeconds: int = Field(default=240, ge=10, le=600)

    @model_validator(mode="after")
    def validate_endpoint(self):
        self.baseUrl = self.baseUrl.strip().rstrip("/")
        self.model = self.model.strip()
        if not self.model:
            raise ValueError("Report model is required")
        p = urlsplit(self.baseUrl)
        local = p.hostname in {"localhost", "127.0.0.1", "::1"}
        if not p.hostname or p.username or p.password or p.query or p.fragment or not (p.scheme == "https" or (local and p.scheme == "http")):
            raise ValueError("Report endpoint requires HTTPS, or HTTP on loopback")
        if self.enabled and not local and not self.apiKey:
            raise ValueError("Enabled remote report model requires its own API key")
        return self


class DomainConcurrency(StrictModel):
    """Production DOMAIN_CONCURRENCY shape (domain-policy.mjs): a default
    per-domain limit plus base-domain overrides, e.g.
    {"defaultConcurrency": 1, "domains": {"mp.weixin.qq.com": 2, "gov.cn": 3}}."""
    defaultConcurrency: int = Field(default=1, ge=1, le=MAX_CONCURRENCY)
    domains: dict[str, int] = Field(default_factory=dict)

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, value):
        return normalize_domain_limits(value)

    def limit_for(self, domain):
        return self.domains.get(domain, self.defaultConcurrency)


class QueryDefaults(StrictModel):
    """Default query scope for reports; sections/boards may override it."""
    sourceScope: Literal["all", "own"] = "all"


class NodeReportRules(StrictModel):
    """Node-level report wording/locality rules; empty overrides keep defaults."""
    nodeName: str = Field(default="上海市静安区临汾路街道", max_length=120)
    city: str = Field(default="上海市", max_length=60)
    district: str = Field(default="静安区", max_length=60)
    street: str = Field(default="临汾路街道", max_length=60)
    leaderBoost: bool = True
    localityPenalty: bool = True
    genericEntityNames: list[str] = Field(default_factory=list, max_length=200)
    localityScore: dict[str, int] = Field(default_factory=dict)
    leaderOverrides: dict = Field(default_factory=dict)
    relationHints: list[str] = Field(default_factory=list, max_length=100)
    topnewsShout: str = Field(default="", max_length=200)
    topnewsSuffix: str = Field(default="", max_length=1000)


class ClientConfig(StrictModel):
    serverUrl: str = ""
    serverKey: str = Field(default="", max_length=500)
    serverCA: str = Field(default="", max_length=4000)
    sources: list[Source] = Field(default_factory=list, max_length=100)
    maxUrls: int = Field(default=20, ge=1, le=200)
    browserConcurrency: int = Field(default=2, ge=1, le=6)
    domainConcurrency: DomainConcurrency = Field(default_factory=DomainConcurrency)
    timeoutSeconds: int = Field(default=60, ge=10, le=300)
    headless: bool = False
    # URL substring fragments (domain or domain+path prefix), matching the
    # production FORBIDDEN_DOMAINS admission rule; explicit seeds are exempt.
    forbiddenDomains: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("domainConcurrency", mode="before")
    @classmethod
    def compat_domain_concurrency(cls, value):
        # Legacy configs stored one global int; keep them loadable.
        if isinstance(value, int) and not isinstance(value, bool):
            return {"defaultConcurrency": value, "domains": {}}
        return value

    @field_validator("forbiddenDomains")
    @classmethod
    def clean_fragments(cls, value):
        # Production config.py FORBIDDEN_DOMAINS validation: non-empty, unique.
        cleaned = [fragment.strip() for fragment in value]
        if any(not fragment for fragment in cleaned):
            raise ValueError("Forbidden URL fragments must be non-empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Forbidden URL fragments must be unique")
        return cleaned
    jev: JevConfig = Field(default_factory=JevConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    reportLlm: ReportLlmConfig = Field(default_factory=ReportLlmConfig)
    reportProfiles: list[ReportProfile] = Field(default_factory=default_report_profiles, max_length=30)
    nodeRules: NodeReportRules = Field(default_factory=NodeReportRules)
    queryDefaults: QueryDefaults = Field(default_factory=QueryDefaults)
    captureCache: CaptureCacheSettings = Field(default_factory=CaptureCacheSettings)
    submissionCache: SubmissionCacheSettings = Field(default_factory=SubmissionCacheSettings)

    @model_validator(mode="after")
    def validate_config(self):
        if self.serverUrl:
            p = urlsplit(self.serverUrl)
            local = p.hostname in {"localhost", "127.0.0.1", "::1"}
            if not p.hostname or p.username or p.password or p.query or p.fragment or not (
                    p.scheme == "https" or (local and p.scheme == "http")):
                raise ValueError("Server URL must be HTTPS (or HTTP on loopback) without credentials, query or fragment")
            self.serverUrl = self.serverUrl.rstrip("/")
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        if len({p.id for p in self.reportProfiles}) != len(self.reportProfiles):
            raise ValueError("Report profile IDs must be unique")
        return self
