"""Fixed weights, so the same lead always gets the same score and every point traces to a signal."""

from datetime import date

from leadh.config import Config
from leadh.models import Lead, Scores
from leadh.registry import SMALL_BANDS, naf_matches


def months_since(iso_day: str, today: date) -> int:
    d = date.fromisoformat(iso_day)
    return (today.year - d.year) * 12 + today.month - d.month - (today.day < d.day)


def score(lead: Lead, cfg: Config, today: date) -> Scores:
    w_need, w_fit = cfg.scoring.need, cfg.scoring.fit
    breakdown: list[dict[str, str | int]] = []

    def add(kind: str, signal: str, source: str) -> int:
        points = (w_need if kind == "need" else w_fit).get(signal, 0)
        if points:
            breakdown.append({"score": kind, "signal": signal, "points": points, "source": source})
        return points

    checked = {s.type: s for s in lead.signals if s.checked}
    need = 0
    if "sells_to_businesses" in checked:
        need += add("need", "sells_to_businesses", checked["sells_to_businesses"].url)
    if "manual_invoicing" in checked:
        need += add("need", "manual_invoicing", checked["manual_invoicing"].url)
    if lead.size_band in SMALL_BANDS:
        need += add("need", "employees_1_to_49", "registry")
    if lead.tools:
        need += add("need", "invoicing_tool_detected", lead.tools[0].url)
    elif lead.site:
        need += add("need", "no_invoicing_tool", lead.site.url)
    if lead.created_on and months_since(lead.created_on, today) < cfg.limits.recent_creation_months:
        need += add("need", "created_recently", "registry")
    if "admin_job_ad" in checked:
        need += add("need", "admin_job_ad", checked["admin_job_ad"].url)

    fit = 0
    if lead.english_at:
        fit += add("fit", "english_pages", lead.english_at)
    if lead.region == "11":
        fit += add("fit", "head_office_ile_de_france", "registry")
    if naf_matches(lead.naf, cfg.naf.b2b_prefixes):
        fit += add("fit", "naf_b2b", "registry")
    if lead.contact and lead.contact.kind == "generic":
        fit += add("fit", "generic_contact", lead.contact.source_url)

    return Scores(need=max(0, min(100, need)), fit=max(0, min(100, fit)), breakdown=breakdown)
