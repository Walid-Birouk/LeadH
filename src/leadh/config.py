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


class NafConfig(BaseModel):
    b2b_prefixes: list[str]
    excluded_prefixes: list[str]


class SearchConfig(BaseModel):
    provider: str = "none"
    api_key_env: str = "BRAVE_API_KEY"
    max_candidates: int = 3


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
    shadow_mode: bool = True
    registry: RegistryConfig
    naf: NafConfig
    search: SearchConfig = SearchConfig()
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
    return cfg
