"""Daily report and the CONDITIONAL review list. Lead IDs and reason codes only: never names or addresses in the
report, since it goes to a chat channel."""

from collections import Counter

from leadh.config import Config
from leadh.models import Lead, Status
from leadh.store import Store

# Steps whose zero output, with leads waiting, means something silently broke.
WATCHED = ["scout", "match", "read", "check", "score", "validate"]
RULE_STREAK = 20


def daily_report(store: Store, cfg: Config, run_id: str, tokens_in: int = 0, tokens_out: int = 0) -> str:
    events = store.events(run_id)
    seen_in: Counter[str] = Counter()
    out: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    parked: Counter[str] = Counter()
    errors: list[str] = []
    dropped = 0
    shadow = 0
    for e in events:
        if e["kind"] == "in":
            seen_in[e["step"]] += 1
        elif e["kind"] == "out":
            out[e["step"]] += 1
        elif e["kind"] == "reason":
            reasons[e["detail"]] += 1
        elif e["kind"] == "parked":
            parked[e["detail"]] += 1
        elif e["kind"] == "dropped":
            dropped += int(e["detail"])
        elif e["kind"] == "shadow":
            shadow += 1
        elif e["kind"] in ("error", "budget_alert"):
            errors.append(f"{e['step']} {e['lead_id'] or '-'} {e['kind']}: {e['detail']}")

    cost = tokens_in / 1e6 * cfg.llm.usd_per_mtok_in + tokens_out / 1e6 * cfg.llm.usd_per_mtok_out
    approved = sum(1 for x in store.by_status(Status.VALIDATED) if x.verdict and x.verdict.status == "APPROVED")
    kept = sum(1 for e in events if e["step"] == "check" and e["kind"] == "out")
    lines = [f"LeadH run {run_id} ({cfg.rules_version}{', shadow mode' if cfg.shadow_mode else ''})", ""]
    lines.append("Leads by status: " + ", ".join(f"{k} {v}" for k, v in sorted(store.status_counts().items())))
    lines.append("This run, per step (in -> out): " + ", ".join(
        f"{s} {seen_in[s]}->{out[s]}" for s in WATCHED + ["handoff"] if seen_in[s] or out[s]))
    if reasons:
        lines.append("Top reasons: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common(8)))
    if parked:
        lines.append("Parked: " + ", ".join(f"{k} {v}" for k, v in parked.most_common()))
    lines.append(f"Evidence check dropped {dropped} quotes across {kept} leads")
    lines.append(f"APPROVED waiting: {approved}; {'would hand off' if cfg.shadow_mode else 'handed off'}: "
                 f"{shadow if cfg.shadow_mode else out['handoff']}")
    lines.append(f"LLM: {tokens_in + tokens_out} tokens, about ${cost:.2f}"
                 + (f", ${cost / approved:.3f} per APPROVED lead" if approved and cost else ""))
    alerts = [f"ALERT {s}: {seen_in[s]} leads in, none out" for s in WATCHED if seen_in[s] and not out[s]]
    lines += alerts
    if errors:
        lines.append(f"Errors ({len(errors)}):")
        lines += [f"  {e}" for e in errors[:20]]
    return "\n".join(lines)


def review_list(store: Store) -> str:
    leads = [x for x in store.by_status(Status.VALIDATED) if x.verdict and x.verdict.status == "CONDITIONAL"]
    if not leads:
        return "No CONDITIONAL leads to review."
    blocks = []
    for lead in sorted(leads, key=lambda x: -(x.scores.need * x.scores.fit if x.scores else 0)):
        blocks.append(review_block(lead))
    blocks.append("Decide with: leadh decide <lead_id> approve|reject <reason_code>. If unsure, look the SIREN up "
                  "in the e-invoicing directory by hand; \"plateforme fictive\" is a strong reason to approve.")
    return "\n\n".join(blocks)


def review_block(lead: Lead) -> str:
    assert lead.verdict
    s = lead.scores
    lines = [f"{lead.lead_id}  SIREN {lead.siren}  {lead.name} ({lead.city or '?'})",
             f"  need {s.need if s else 0}, fit {s.fit if s else 0}; flags: {', '.join(lead.verdict.reasons)}",
             f"  site: {lead.site.url} (SIREN on {lead.site.siren_found_at})" if lead.site else "  site: -"]
    if lead.contact:
        lines.append(f"  contact: {lead.contact.email} ({lead.contact.kind}) from {lead.contact.source_url}")
    for sig in lead.signals:
        if sig.checked:
            lines.append(f"  - {sig.type}: \"{sig.quote}\" {sig.url}")
    if lead.tools:
        lines.append("  tools: " + ", ".join(f"{t.name} ({t.url})" for t in lead.tools))
    return "\n".join(lines)


def rule_candidates(store: Store) -> list[str]:
    """Reason codes you decided the same way RULE_STREAK times in a row: ready to become rules."""
    flags_by_lead: dict[str, list[str]] = {}
    streak: dict[str, tuple[str, int]] = {}
    for row in store.decisions():
        lead_id = row["lead_id"]
        if lead_id not in flags_by_lead:
            lead = store.get(lead_id)
            flags_by_lead[lead_id] = lead.verdict.reasons if lead and lead.verdict else []
        for code in {row["reason_code"], *flags_by_lead[lead_id]}:
            last, n = streak.get(code, (row["decision"], 0))
            streak[code] = (row["decision"], n + 1 if last == row["decision"] else 1)
    return [f"{code}: {d} x{n}" for code, (d, n) in sorted(streak.items()) if n >= RULE_STREAK]
