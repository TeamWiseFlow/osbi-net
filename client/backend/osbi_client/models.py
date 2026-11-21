"""Local configuration and bounded programmable workflows."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from osbi_contracts.models import StrictModel
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
    kind: Literal["web", "rss"] = "web"
    enabled: bool = True
    intervalMinutes: int = Field(default=0, ge=0, le=525600)
    profile: str = Field(default="default", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    channel: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    steps: list[Step] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_url(self):
        from osbi_contracts.urls import canonical_url
        if not canonical_url(self.url):
            raise ValueError("Source requires an HTTP(S) URL")
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
    preSubmit: bool = True
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


class ClientConfig(StrictModel):
    serverUrl: str = ""
    serverKey: str = Field(default="", max_length=500)
    serverCA: str = Field(default="", max_length=4000)
    focus: str = Field(default="", max_length=4000)
    defaultChannel: str = Field(default="default", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    sources: list[Source] = Field(default_factory=list, max_length=100)
    maxUrls: int = Field(default=20, ge=1, le=200)
    browserConcurrency: int = Field(default=2, ge=1, le=6)
    domainConcurrency: int = Field(default=1, ge=1, le=6)
    timeoutSeconds: int = Field(default=60, ge=10, le=300)
    headless: bool = False
    forbiddenDomains: list[str] = Field(default_factory=list, max_length=100)
    jev: JevConfig = Field(default_factory=JevConfig)
    reportProfiles: list[ReportProfile] = Field(default_factory=default_report_profiles, max_length=30)
    captureCache: CaptureCacheSettings = Field(default_factory=CaptureCacheSettings)
    submissionCache: SubmissionCacheSettings = Field(default_factory=SubmissionCacheSettings)

    @model_validator(mode="after")
    def validate_config(self):
        if self.serverUrl:
            p = urlsplit(self.serverUrl)
            if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise ValueError("Server URL must be HTTPS without credentials, query or fragment")
            self.serverUrl = self.serverUrl.rstrip("/")
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        if len({p.id for p in self.reportProfiles}) != len(self.reportProfiles):
            raise ValueError("Report profile IDs must be unique")
        return self
