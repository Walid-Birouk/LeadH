"""Validator: plain code. One failed hard rule rejects; a review flag sends the lead to you; the score decides the
rest. It runs after scoring and again right before hand-off."""

from collections.abc import Callable

import dns.exception
import dns.resolver

from leadh.config import Config
from leadh.models import Lead, Verdict
from leadh.pages import domain_of
from leadh.registry import is_sme
from leadh.search import is_directory
from leadh.store import Store

MxCheck = Callable[[str], bool]


def has_mx(domain: str) -> bool:
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=10)) > 0
    except dns.resolver.NoAnswer:
        # RFC 5321: with no MX, mail goes to the domain's A record.
        try:
            return len(dns.resolver.resolve(domain, "A", lifetime=10)) > 0
        except dns.exception.DNSException:
            return False
    except dns.exception.DNSException:
        return False


def hard_failures(lead: Lead, store: Store, mx: MxCheck, skip_commercial_opposition: bool) -> list[str]:
    reasons = []
    domain = lead.contact.email.split("@")[1] if lead.contact else None
    if not lead.active:
        reasons.append("inactive")
    if not is_sme(lead):
        reasons.append("not_sme")
    if lead.diffusion == "P":
        reasons.append("diffusion_partial")
    if skip_commercial_opposition and lead.commercial_opposition:
        reasons.append("commercial_opposition")
    if store.opted_out(lead.siren, domain):
        reasons.append("opted_out")
    if store.contacted(lead.siren):
        reasons.append("already_contacted")
    if lead.site is None:
        reasons.append("no_site_match")
    elif is_directory(domain_of(lead.site.url)) or is_directory(domain_of(lead.site.siren_found_at)):
        # A directory or scraper page showing the SIREN (matches made before that check existed) is not the site.
        reasons.append("directory_site")
    types = {s.type for s in lead.signals if s.checked}
    if "sells_to_consumers" in types and "sells_to_businesses" not in types:
        reasons.append("b2c_only")
    if lead.contact is None:
        reasons.append("no_published_contact")
    elif domain and not mx(domain):
        reasons.append("no_mx")
    return reasons


def review_flags(lead: Lead, cfg: Config) -> list[str]:
    flags = []
    if lead.site and lead.site.proof != "siren":
        flags.append("site_unproven")
    if lead.contact and lead.contact.kind == "nominative":
        flags.append("nominative_address")
    if lead.contact and lead.contact.free_mail:
        flags.append("free_mail_domain")
    types = {s.type for s in lead.signals if s.checked}
    if {"sells_to_consumers", "sells_to_businesses"} <= types:
        flags.append("mixed_customers")
    t = cfg.scoring.thresholds
    need, fit = (lead.scores.need, lead.scores.fit) if lead.scores else (0, 0)
    if need >= t.review_need and fit >= t.review_fit and (need < t.approve_need or fit < t.approve_fit):
        flags.append("score_band")
    return flags


def validate(lead: Lead, cfg: Config, store: Store, mx: MxCheck = has_mx) -> Verdict:
    need, fit = (lead.scores.need, lead.scores.fit) if lead.scores else (0, 0)
    t = cfg.scoring.thresholds

    def verdict(status: str, reasons: list[str]) -> Verdict:
        return Verdict.model_validate({"status": status, "reasons": reasons, "need": need, "fit": fit,
                                       "rules_version": cfg.rules_version})

    failures = hard_failures(lead, store, mx, cfg.registry.skip_commercial_opposition)
    if failures:
        return verdict("REJECTED", failures)
    # A flag sends the lead to review only if its scores reach the review floor; below it, nothing to review.
    if need < t.review_need or fit < t.review_fit:
        return verdict("REJECTED", ["low_score"])
    flags = review_flags(lead, cfg)
    if flags:
        return verdict("CONDITIONAL", flags)
    return verdict("APPROVED", [])
