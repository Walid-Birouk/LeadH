"""LeadH: finds companies that fit a customer profile, scores them as leads and drafts outreach to test."""

import argparse
import csv
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from leadh.campaign import Campaign, compare, compare_table, draft_messages, load_campaign
from leadh.config import Config, load_config, load_dotenv
from leadh.http import Http
from leadh.mailbox import MailboxError, MailboxSettings, microsoft_token, save_drafts
from leadh.models import Lead, Status, Verdict, now
from leadh.outreach import MODEL_SLOTS, mock_slots
from leadh.pipeline import STEPS, Pipeline
from leadh.registry import Registry
from leadh.report import daily_report, review_list, rule_candidates
from leadh.search import make_search
from leadh.signals import Budget, SignalReader, make_complete
from leadh.site import SiteMatcher
from leadh.store import Store


def open_store(cfg: Config) -> Store:
    return Store(cfg.data_dir / "leadh.db", cfg.ledger)


def cmd_run(cfg: Config, args: argparse.Namespace) -> None:
    if args.limit is not None:
        cfg.limits.candidates_per_run = args.limit
    store = open_store(cfg)
    http = Http(cfg.crawler)
    budget = Budget(cfg.limits.llm_tokens_per_run)
    pipeline = Pipeline(
        cfg=cfg, store=store, registry=Registry(cfg, http),
        matcher=SiteMatcher(http, make_search(cfg.search, http), cfg.search.max_candidates, cfg.limits.pages_per_site),
        reader=SignalReader(make_complete(cfg.llm), budget, cfg.llm.max_chars_per_page),
        run_id=args.run_id or date.today().isoformat(), campaign=args.campaign.name if args.campaign else "")
    pipeline.run(args.steps)
    report = daily_report(store, cfg, pipeline.run_id, budget.used_in, budget.used_out)
    out = cfg.data_dir / "reports" / f"{pipeline.run_id}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    print(report)


def cmd_report(cfg: Config, args: argparse.Namespace) -> None:
    print(daily_report(open_store(cfg), cfg, args.run_id or date.today().isoformat()))


def cmd_review(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    print(review_list(store))
    for line in rule_candidates(store):
        print(f"Ready to become a rule: {line}")


def cmd_decide(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    lead = store.get(args.lead_id)
    if lead is None or lead.status != Status.VALIDATED or not lead.verdict or lead.verdict.status != "CONDITIONAL":
        sys.exit(f"{args.lead_id} is not a CONDITIONAL lead")
    store.log_decision(lead.lead_id, args.decision, args.reason)
    lead.verdict = Verdict(status="APPROVED" if args.decision == "approve" else "REJECTED",
                           reasons=lead.verdict.reasons, need=lead.verdict.need, fit=lead.verdict.fit,
                           rules_version=cfg.rules_version, decided_by="human")
    lead.validated_at = now()
    if args.decision == "reject":
        lead = lead.model_copy(update={"name": "", "site": None, "pages": [], "signals": [], "tools": [],
                                       "contact": None, "status": Status.REJECTED})
    store.save(lead)
    print(f"{lead.lead_id}: {args.decision} ({args.reason})")


def cmd_export(cfg: Config, args: argparse.Namespace) -> None:
    """APPROVED and CONDITIONAL leads, best first, as a CSV you can open in a spreadsheet."""
    store = open_store(cfg)
    leads = [x for x in store.by_status(Status.VALIDATED) + store.by_status(Status.HANDED_OFF)
             if x.verdict and x.verdict.status in ("APPROVED", "CONDITIONAL")]
    leads.sort(key=lambda x: (x.verdict.status != "APPROVED" if x.verdict else True,
                              -(x.scores.need * x.scores.fit if x.scores else 0)))
    out = Path(args.out or cfg.data_dir / "leads.csv")
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "verdict", "flags", "need", "fit", "siren", "name", "city", "naf", "site", "email",
                    "email_source", "english", "tools", "signals", "campaign", "variant", "template", "draft_status",
                    "subject", "body", *(f"{s}_quote" for s in MODEL_SLOTS)])
        for x in leads:
            assert x.verdict
            w.writerow([x.lead_id, x.verdict.status, " ".join(x.verdict.reasons), x.scores.need if x.scores else "",
                        x.scores.fit if x.scores else "", x.siren, x.name, x.city, x.naf,
                        x.site.url if x.site else "", x.contact.email if x.contact else "",
                        x.contact.source_url if x.contact else "", "yes" if x.english_at else "",
                        ", ".join(t.name for t in x.tools),
                        " | ".join(f'{s.type}: "{s.quote}"' for s in x.signals if s.checked), *draft_cells(x)])
    print(f"{len(leads)} leads written to {out}")


def draft_cells(lead: Lead) -> list[str]:
    d = lead.draft
    if d is None:
        return [lead.campaign, "", "", "", "", "", *("" for _ in MODEL_SLOTS)]
    quotes = [f'"{d.slots[s].quote}" {d.slots[s].url}' if s in d.slots else "" for s in MODEL_SLOTS]
    return [lead.campaign, d.variant, d.template, d.status, d.subject, d.body, *quotes]


def need_campaign(args: argparse.Namespace) -> Campaign:
    if not args.campaign:
        sys.exit("this command needs --campaign campaigns/<name>.yaml")
    campaign: Campaign = args.campaign
    return campaign


def cmd_draft(cfg: Config, args: argparse.Namespace) -> None:
    campaign = need_campaign(args)
    budget = Budget(cfg.limits.llm_tokens_per_run)
    result = draft_messages(campaign, open_store(cfg), make_complete(cfg.llm, mock=mock_slots), budget)
    print(f"{campaign.name}: {result.drafted} drafted ({result.needs_edit} need your edit), {result.skipped} "
          f"skipped; LLM {budget.used} tokens")


def cmd_outcome(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    lead = store.get(args.lead_id)
    if lead is None or lead.draft is None:
        sys.exit(f"{args.lead_id} has no draft")
    store.add_outcome(lead.lead_id, args.event)
    if args.event == "sent":
        store.add_contacted(lead.siren)


def cmd_to_mailbox(cfg: Config, args: argparse.Namespace) -> None:
    """Ready drafts into your Drafts folder. Nothing is sent: you read, edit and send each one yourself."""
    try:
        settings = MailboxSettings.from_env()
        # The sign-in cache sits next to the shared ledger, so one sign-in serves every campaign.
        cache = (cfg.ledger or cfg.data_dir / "ledger.db").parent / "ms_token_cache.json"
        r = save_drafts(open_store(cfg), settings, lambda: microsoft_token(settings, cache), limit=args.limit)
    except MailboxError as e:
        sys.exit(str(e))
    print(f"{r.saved} drafts saved to {r.folder or 'your Drafts folder'}; {r.already} already there, "
          f"{r.needs_edit} need your edit first, {r.no_contact} skipped (contacted, opted out or no address)")


def cmd_compare(cfg: Config, args: argparse.Namespace) -> None:
    print(compare_table(compare([load_campaign(p) for p in args.campaigns])))


def cmd_optout(cfg: Config, args: argparse.Namespace) -> None:
    open_store(cfg).add_optout(args.kind, args.value)


def cmd_contacted(cfg: Config, args: argparse.Namespace) -> None:
    open_store(cfg).add_contacted(args.siren)


def cmd_purge(cfg: Config, args: argparse.Namespace) -> None:
    """Retention: page copies after 30 days, never-contacted leads after about 6 months. Rejected leads (already
    reduced to SIREN, reasons and date) and the opt-out list are kept."""
    store = open_store(cfg)
    pages = store.purge_pages(now() - timedelta(days=cfg.retention_days.pages))
    cutoff = now() - timedelta(days=cfg.retention_days.never_contacted)
    leads = 0
    for lead in list(store.all()):
        if lead.status not in (Status.REJECTED, Status.HANDED_OFF) and not store.contacted(lead.siren) \
                and lead.updated_at < cutoff:
            store.delete(lead.siren)
            leads += 1
    print(f"purged {pages} pages, {leads} leads")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="leadh", description=__doc__)
    parser.add_argument("--config", default="config/leadh.yaml")
    parser.add_argument("--campaign", type=load_campaign,
                        help="campaign file; replaces --config and keeps its own data")
    parser.add_argument("-q", "--quiet", action="store_true", help="only the report and errors")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="move leads forward through every step")
    p.add_argument("--run-id")
    p.add_argument("--steps", nargs="+", choices=STEPS)
    p.add_argument("--limit", type=int, help="new candidates this run (default: limits.candidates_per_run)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="print a run's daily report")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_report)

    sub.add_parser("review", help="CONDITIONAL leads with their quotes and links").set_defaults(fn=cmd_review)

    p = sub.add_parser("decide", help="approve or reject a CONDITIONAL lead")
    p.add_argument("lead_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("reason", help="reason code, e.g. plateforme_fictive or role_mismatch")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("export", help="APPROVED and CONDITIONAL leads as a CSV")
    p.add_argument("--out", help="file to write (default: <data_dir>/leads.csv)")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("optout", help="add a SIREN or email domain to the opt-out list")
    p.add_argument("kind", choices=["siren", "domain"])
    p.add_argument("value")
    p.set_defaults(fn=cmd_optout)

    p = sub.add_parser("contacted", help="record a SIREN in the contacted ledger")
    p.add_argument("siren")
    p.set_defaults(fn=cmd_contacted)

    sub.add_parser("purge", help="apply the retention periods").set_defaults(fn=cmd_purge)

    sub.add_parser("draft", help="fill the campaign's templates for APPROVED leads (needs --campaign)") \
        .set_defaults(fn=cmd_draft)

    p = sub.add_parser("to-mailbox", help="save ready drafts into your mailbox's Drafts folder (never sends)")
    p.add_argument("--limit", type=int, help="at most this many drafts")
    p.set_defaults(fn=cmd_to_mailbox)

    p = sub.add_parser("outcome", help="record what happened to a drafted message")
    p.add_argument("lead_id")
    p.add_argument("event", choices=["sent", "replied", "meeting", "bounced"])
    p.set_defaults(fn=cmd_outcome)

    p = sub.add_parser("compare", help="drafts, sends and replies per campaign and variant")
    p.add_argument("campaigns", nargs="+", help="campaign files")
    p.set_defaults(fn=cmd_compare)

    load_dotenv()  # before parsing: --campaign loads its config, which reads the environment
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.fn(args.campaign.cfg if args.campaign else load_config(args.config), args)


if __name__ == "__main__":
    main()
