import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class RegistryConfig(BaseModel):
    base_url: str = "https://recherche-entreprises.api.gouv.fr"
    naf_codes: list[str]
    regions: list[str] = []
    per_page: int = 25
    request_interval_s: float = 0.2
    skip_commercial_opposition: bool = True
    employee_bands: list[str] = []
    exclude_sole_traders: bool = False


class NafConfig(BaseModel):
    b2b_prefixes: list[str]
    excluded_prefixes: list[str]


class SearchConfig(BaseModel):
    provider: str = "none"
    api_key_env: str = "BRAVE_SEARCH_API_KEY"
    interval_s: float = 1.0
    max_candidates: int = 3


class ReachConfig(BaseModel):
    """Agent-Reach helpers. Each one does nothing when the `agent-reach` command is not installed."""
    enabled: bool = False
    command: str = "agent-reach"
    timeout_s: float = 60
    search_limit: int = 8
    # Pacing per channel, and what to do when Agent-Reach says to wait (rate limit, busy, backend paused): that
    # channel rests for the time it gives, or pause_s. Calls in the meantime are skipped, unless the rest left is at
    # most max_wait_s, which is slept instead; a lead whose site search was skipped stays "found" for the next run.
    # max_strikes such answers in a row turn the channel off for the run.
    min_interval_s: float = 3.0
    pause_s: float = 120
    max_wait_s: float = 15
    max_strikes: int = 3
    # Read a page through Agent-Reach when the site refuses LeadH's crawler (HTTP 401, 403, 406, 429 or 503).
    # Never used for a page robots.txt disallows.
    read_fallback: bool = False
    # Public LinkedIn job search (no login) for admin, accounting or invoicing ads by the lead's company.
    linkedin_jobs: bool = False
    jobs_limit: int = 10


class CrawlerConfig(BaseModel):
    user_agent: str
    request_interval_s: float = 1.0
    timeout_s: float = 15
    max_tries: int = 5
    breaker_threshold: int = 3
    breaker_pause_s: float = 60


class LlmConfig(BaseModel):
    base_url: str = "mock"
    model: str = "mock"
    api_key_env: str = "LLM_API_KEY"
    json_mode: bool = False
    extra_body: dict[str, Any] = {}
    timeout_s: float = 60
    max_chars_per_page: int = 6000
    usd_per_mtok_in: float = 0.0
    usd_per_mtok_out: float = 0.0


class Limits(BaseModel):
    candidates_per_run: int = 300
    pages_per_site: int = 5
    seconds_per_lead: float = 60
    llm_tokens_per_run: int = 400_000
    handoffs_per_day: int = 30
    freshness_days: int = 30
    quote_max_words: int = 25
    recent_creation_months: int = 18


class Thresholds(BaseModel):
    approve_need: int = 50
    approve_fit: int = 60
    review_need: int = 35
    review_fit: int = 40


class Scoring(BaseModel):
    need: dict[str, int]
    fit: dict[str, int]
    thresholds: Thresholds = Thresholds()


class Retention(BaseModel):
    pages: int = 30
    never_contacted: int = 182


class Config(BaseModel):
    rules_version: str
    data_dir: Path = Path("data")
    # Opt-out list and contacted ledger, shared by all campaigns; defaults to <data_dir>/ledger.db.
    ledger: Path | None = None
    shadow_mode: bool = True
    registry: RegistryConfig
    naf: NafConfig
    search: SearchConfig = SearchConfig()
    reach: ReachConfig = ReachConfig()
    crawler: CrawlerConfig
    llm: LlmConfig = LlmConfig()
    limits: Limits = Limits()
    scoring: Scoring
    retention_days: Retention = Retention()


def load_dotenv(path: Path | str = ".env") -> None:
    """KEY=value lines from .env; variables already set in the environment win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: Path | str = "config/leadh.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    cfg = Config.model_validate(raw)
    # Env overrides switch provider or data folder without editing the file.
    cfg.llm.base_url = os.environ.get("LLM_BASE_URL", cfg.llm.base_url)
    cfg.llm.model = os.environ.get("LLM_MODEL", cfg.llm.model)
    cfg.search.provider = os.environ.get("LEADH_SEARCH", cfg.search.provider)
    if "LEADH_DATA" in os.environ:
        cfg.data_dir = Path(os.environ["LEADH_DATA"])
    cfg.ledger = cfg.ledger or cfg.data_dir / "ledger.db"
    return cfg
