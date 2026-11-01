from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    FOUND = "found"
    MATCHED = "matched"
    READ = "read"
    CHECKED = "checked"
    SCORED = "scored"
    VALIDATED = "validated"
    HANDED_OFF = "handed_off"
    NO_SITE = "no_site"
    REJECTED = "rejected"
    PARKED = "parked"


SignalType = Literal["sells_to_businesses", "sells_to_consumers", "manual_invoicing", "admin_job_ad"]
NEED_SIGNALS: frozenset[str] = frozenset({"sells_to_businesses", "manual_invoicing", "admin_job_ad"})


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Site(BaseModel):
    url: str
    siren_found_at: str
    # "name_address" when the site shows no SIREN but its name and postal code match the registry: weaker, so the
    # Validator sends it to review.
    proof: Literal["siren", "name_address"] = "siren"


class PageRef(BaseModel):
    url: str
    sha256: str
    fetched_at: datetime


class Signal(BaseModel):
    type: SignalType
    value: str = ""
    quote: str
    url: str
    checked: bool = False


class Tool(BaseModel):
    name: str
    url: str


class Contact(BaseModel):
    email: str
    kind: Literal["generic", "nominative"]
    free_mail: bool = False
    source_url: str


class Scores(BaseModel):
    need: int
    fit: int
    breakdown: list[dict[str, str | int]] = []


class Verdict(BaseModel):
    status: Literal["APPROVED", "CONDITIONAL", "REJECTED"]
    reasons: list[str] = []
    need: int = 0
    fit: int = 0
    rules_version: str
    checked_at: datetime = Field(default_factory=now)
    decided_by: Literal["rules", "human"] = "rules"


class Lead(BaseModel):
    lead_id: str
    siren: str
    name: str
    naf: str | None = None
    category: str | None = None
    size_band: str | None = None
    created_on: str | None = None
    city: str | None = None
    postal_code: str | None = None
    region: str | None = None
    active: bool = True
    diffusion: str = "O"
    commercial_opposition: bool = False
    status: Status = Status.FOUND
    park_reason: str | None = None
    site: Site | None = None
    pages: list[PageRef] = []
    english_at: str | None = None
    signals: list[Signal] = []
    signals_dropped: int = 0
    tools: list[Tool] = []
    contact: Contact | None = None
    scores: Scores | None = None
    verdict: Verdict | None = None
    validated_at: datetime | None = None
    run_id: str = ""
    updated_at: datetime = Field(default_factory=now)
