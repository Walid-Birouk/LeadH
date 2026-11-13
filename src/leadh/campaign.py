"""A campaign bundles one customer profile, one lead source, its templates and a lead count, so runs can be
compared. Each campaign keeps its own lead table under data/campaigns/<name>/."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

from leadh.config import Config, load_config
from leadh.models import Status
from leadh.outreach import Template, fill_slots, load_template, render, variant_for
from leadh.signals import BadOutput, Budget, Complete
from leadh.store import Store


class CampaignFile(BaseModel):
    name: str
    # A full config file: who the customer is (NAF families, scoring, rules) and how the pipeline runs.
    profile: Path
    # Overrides of the profile's `registry` section: which companies are scouted.
    source: dict[str, Any] = {}
    # Variant name -> template file. One template means no A/B test; two (or more) split leads between them.
    templates: dict[str, Path]
    # Messages this campaign drafts in total, best-ranked APPROVED leads first.
    lead_count: int

    @field_validator("name")
    @classmethod
    def slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", v):
            raise ValueError("use lowercase letters, digits and dashes")
        return v

    @field_validator("templates")
    @classmethod
    def some(cls, v: dict[str, Path]) -> dict[str, Path]:
        if not v:
            raise ValueError("at least one template")
        return v


class Campaign:
    def __init__(self, spec: CampaignFile, cfg: Config, templates: dict[str, Template]) -> None:
        self.spec = spec
        self.cfg = cfg
        self.templates = templates

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def variants(self) -> list[str]:
        return sorted(self.templates)


def load_campaign(path: Path | str) -> Campaign:
    """Paths inside the file are relative to the folder LeadH runs from, like --config."""
    spec = CampaignFile.model_validate(yaml.safe_load(Path(path).read_text()))
    cfg = load_config(spec.profile)
    cfg.registry = cfg.registry.model_validate({**cfg.registry.model_dump(), **spec.source})
    cfg.data_dir = cfg.data_dir / "campaigns" / spec.name
    templates = {variant: load_template(p) for variant, p in spec.templates.items()}
    return Campaign(spec, cfg, templates)


@dataclass
class DraftResult:
    drafted: int = 0
    needs_edit: int = 0
    skipped: int = 0  # pages expired or the model's answer unusable; tried again next time


def draft_messages(campaign: Campaign, store: Store, complete: Complete, budget: Budget) -> DraftResult:
    """Drafts for APPROVED leads without one, best need x fit first, until the campaign's lead count is reached."""
    leads = [x for x in [*store.by_status(Status.VALIDATED), *store.by_status(Status.HANDED_OFF)]
             if x.verdict and x.verdict.status == "APPROVED"]
    room = campaign.spec.lead_count - sum(1 for x in leads if x.draft)
    # Contacted or opted out since validation, possibly through another campaign: never drafted.
    todo = sorted((x for x in leads if not x.draft and not store.contacted(x.siren)
                   and not store.opted_out(x.siren, x.contact.email.split("@")[1] if x.contact else None)),
                  key=lambda x: -(x.scores.need * x.scores.fit if x.scores else 0))
    result = DraftResult()
    limits, llm = campaign.cfg.limits, campaign.cfg.llm
    for lead in todo[:max(0, room)]:
        variant = variant_for(campaign.name, lead.siren, campaign.variants)
        template = campaign.templates[variant]
        texts = {}
        for ref in lead.pages:
            stored = store.page(ref.sha256)
            if stored:
                texts[ref.url] = stored[1]
        if template.model_slots and not texts:
            result.skipped += 1
            continue
        try:
            fills = fill_slots(complete, budget, template.model_slots, texts, llm.max_chars_per_page,
                               limits.quote_max_words, company=lead.name)
        except BadOutput:
            result.skipped += 1
            continue
        lead.draft = render(template, lead, variant, fills)
        lead.campaign = campaign.name
        store.save(lead)
        result.drafted += 1
        result.needs_edit += lead.draft.status == "needs_edit"
    return result


@dataclass
class VariantStats:
    campaign: str
    variant: str
    template: str
    drafted: int = 0
    sent: int = 0
    replied: int = 0
    meeting: int = 0
    bounced: int = 0

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.sent if self.sent else None


def compare(campaigns: list[Campaign]) -> list[VariantStats]:
    """Drafts and recorded outcomes per campaign and variant."""
    rows: list[VariantStats] = []
    for c in campaigns:
        store = Store(c.cfg.data_dir / "leadh.db", c.cfg.ledger)
        outcomes = store.outcomes()
        by_variant = {v: VariantStats(c.name, v, c.templates[v].name) for v in c.variants}
        for lead in store.all():
            if not lead.draft or lead.draft.variant not in by_variant:
                continue
            stats = by_variant[lead.draft.variant]
            stats.drafted += 1
            events = outcomes.get(lead.lead_id, set())
            stats.sent += "sent" in events
            stats.replied += "replied" in events
            stats.meeting += "meeting" in events
            stats.bounced += "bounced" in events
        store.close()
        rows += by_variant.values()
    return rows


def compare_table(rows: list[VariantStats]) -> str:
    head = f"{'campaign':<28} {'variant':<7} {'template':<24} {'drafted':>7} {'sent':>5} {'replied':>7} " \
           f"{'meeting':>7} {'bounced':>7} {'reply rate':>10}"
    lines = [head, "-" * len(head)]
    for r in rows:
        rate = f"{r.reply_rate:.0%}" if r.reply_rate is not None else "-"
        lines.append(f"{r.campaign:<28} {r.variant:<7} {r.template:<24} {r.drafted:>7} {r.sent:>5} {r.replied:>7} "
                     f"{r.meeting:>7} {r.bounced:>7} {rate:>10}")
    # A reply-rate gap between two variants means little until each has been sent to enough leads.
    if any(0 < r.sent < 30 for r in rows):
        lines.append("Fewer than 30 sent in a variant: too few to call a winner yet.")
    return "\n".join(lines)
