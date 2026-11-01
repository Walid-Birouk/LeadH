"""The morning run. No coordinator model: each step takes the leads in its input status, works through them and
writes the next status, so a run that dies halfway resumes where it stopped."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from leadh import evidence, scorer, tech
from leadh.config import Config
from leadh.contact import best_contact
from leadh.http import CircuitOpen, Deadline, FetchError
from leadh.models import NEED_SIGNALS, Lead, PageRef, Site, Status, now
from leadh.pages import clean_text, domain_of, is_english, sha256
from leadh.registry import Registry, to_lead
from leadh.signals import BadOutput, BudgetExhausted, SignalReader
from leadh.site import SiteMatcher
from leadh.store import Store, expired
from leadh.validator import MxCheck, has_mx, validate

log = logging.getLogger("leadh")

STEPS = ["scout", "match", "read", "check", "score", "validate", "handoff"]


@dataclass
class Pipeline:
    cfg: Config
    store: Store
    registry: Registry
    matcher: SiteMatcher
    reader: SignalReader
    run_id: str
    mx: MxCheck = has_mx
    today: Callable[[], date] = date.today

    def run(self, steps: list[str] | None = None) -> None:
        self.requeue_on_new_rules()
        for step in steps or STEPS:
            getattr(self, step)()

    def _event(self, step: str, kind: str, lead: Lead | None = None, detail: str = "") -> None:
        self.store.event(self.run_id, step, kind, lead.lead_id if lead else None, detail)
        if kind == "error":
            log.warning("%s %s %s", step, lead.lead_id if lead else "-", detail)
        elif kind in ("out", "parked", "reason", "refetch", "shadow"):
            log.info("%-10s %s %s %s", step, lead.lead_id if lead else "-", kind if kind != "out" else "->", detail)

    def _advance(self, lead: Lead, step: str, status: Status) -> None:
        lead.status = status
        lead.run_id = self.run_id
        self.store.save(lead)
        self.store.mark_step(lead.lead_id, step, self.cfg.rules_version, self.run_id)
        self._event(step, "out", lead, status.value)

    def _park(self, lead: Lead, step: str, reason: str) -> None:
        lead.status = Status.PARKED
        lead.park_reason = reason
        self.store.save(lead)
        self._event(step, "parked", lead, reason)

    def requeue_on_new_rules(self) -> None:
        """Validated leads scored under an older rules version are scored and validated again."""
        for lead in self.store.by_status(Status.VALIDATED):
            if lead.verdict and lead.verdict.decided_by == "human":
                continue
            if not self.store.step_done(lead.lead_id, "validate", self.cfg.rules_version):
                lead.status = Status.CHECKED
                self.store.save(lead)

    # Stage 1: find

    def scout(self) -> None:
        added = 0
        try:
            for row in self.registry.candidates():
                if added >= self.cfg.limits.candidates_per_run:
                    break
                if self.store.has(row["siren"]):
                    continue
                lead = to_lead(row, self.store.next_lead_id(), self.run_id)
                self._event("scout", "in", lead)
                self._advance(lead, "scout", Status.FOUND)
                added += 1
        except FetchError as e:
            self._event("scout", "error", detail=str(e))

    def match(self) -> None:
        for lead in self.store.by_status(Status.FOUND):
            self._event("match", "in", lead)
            try:
                m = self.matcher.match(lead, Deadline(self.cfg.limits.seconds_per_lead))
            except TimeoutError:
                self._park(lead, "match", "timeout")
                continue
            except CircuitOpen as e:
                self._event("match", "error", lead, f"circuit open: {e}")
                return
            except FetchError as e:
                self._event("match", "error", lead, str(e))
                continue
            if m is None:
                self._advance(lead, "match", Status.NO_SITE)
                continue
            fetched_at = now()
            lead.site = Site(url=m.site_url, siren_found_at=m.siren_found_at, proof=m.proof)
            lead.pages = []
            for page in m.pages:
                digest = sha256(page.html)
                self.store.put_page(digest, page.url, page.html, clean_text(page.html), fetched_at)
                lead.pages.append(PageRef(url=page.url, sha256=digest, fetched_at=fetched_at))
            html_pages = [(p.url, p.html) for p in m.pages]
            lead.tools = tech.detect(html_pages)
            lead.contact = best_contact(html_pages, domain_of(m.site_url))
            lead.english_at = next((u for u, h in html_pages if is_english(h, u)), None)
            self._advance(lead, "match", Status.MATCHED)

    def _texts(self, lead: Lead) -> dict[str, str] | None:
        texts = {}
        for ref in lead.pages:
            stored = self.store.page(ref.sha256)
            if stored is None:
                return None
            texts[ref.url] = stored[1]
        return texts

    def _refetch(self, lead: Lead, step: str) -> None:
        """Pages purged or stale: start the lead again from the Site matcher instead of trusting old copies."""
        lead.site, lead.pages, lead.signals, lead.tools, lead.contact = None, [], [], [], None
        lead.english_at, lead.scores, lead.verdict, lead.signals_dropped = None, None, None, 0
        self.store.clear_steps(lead.lead_id)
        lead.status = Status.FOUND
        self.store.save(lead)
        self._event(step, "refetch", lead)

    # Stage 2: read

    def read(self) -> None:
        for lead in self.store.by_status(Status.MATCHED):
            self._event("read", "in", lead)
            texts = self._texts(lead)
            if texts is None:
                self._refetch(lead, "read")
                continue
            try:
                lead.signals = self.reader.read(list(texts.items()))
            except BudgetExhausted as e:
                self._event("read", "error", lead, f"token budget: {e}")
                return
            except BadOutput:
                self._park(lead, "read", "malformed_output")
                continue
            except Exception as e:  # provider errors park the lead rather than stop the run
                self._event("read", "error", lead, type(e).__name__)
                self._park(lead, "read", "llm_error")
                continue
            if self.reader.budget.over_alert():
                self._event("read", "budget_alert", detail=f"{self.reader.budget.used}/{self.reader.budget.limit}")
            self._advance(lead, "read", Status.READ)

    def check(self) -> None:
        for lead in self.store.by_status(Status.READ):
            self._event("check", "in", lead)
            texts = self._texts(lead)
            if texts is None:
                self._refetch(lead, "check")
                continue
            lead.signals, dropped = evidence.check(lead.signals, texts, self.cfg.limits.quote_max_words)
            lead.signals_dropped = dropped
            if dropped:
                self._event("check", "dropped", lead, str(dropped))
            self._advance(lead, "check", Status.CHECKED)

    # Stage 3: decide

    def score(self) -> None:
        for lead in self.store.by_status(Status.CHECKED):
            self._event("score", "in", lead)
            lead.scores = scorer.score(lead, self.cfg, self.today())
            self._advance(lead, "score", Status.SCORED)

    def _decide(self, lead: Lead, step: str) -> None:
        human = lead.verdict if lead.verdict and lead.verdict.decided_by == "human" else None
        lead.verdict = validate(lead, self.cfg, self.store, self.mx)
        if human and lead.verdict.status != "REJECTED":
            lead.verdict = human  # your decision stands unless a hard rule now fails
        lead.validated_at = now()
        for reason in lead.verdict.reasons:
            self._event(step, "reason", lead, reason)
        if lead.verdict.status == "REJECTED":
            self._reject(lead, step)
        else:
            self._advance(lead, step, Status.VALIDATED)

    def _reject(self, lead: Lead, step: str) -> None:
        # Rejected leads keep only SIREN, reason codes and date, so they are never processed twice.
        assert lead.verdict is not None
        kept = Lead(lead_id=lead.lead_id, siren=lead.siren, name="", verdict=lead.verdict,
                    validated_at=lead.validated_at)
        self._advance(kept, step, Status.REJECTED)

    def validate(self) -> None:
        for lead in self.store.by_status(Status.SCORED):
            self._event("validate", "in", lead)
            self._decide(lead, "validate")

    def handoff(self) -> None:
        """Second Validator pass on fresh registry data, then the top APPROVED leads go to the Write stage."""
        ready: list[Lead] = []
        for lead in self.store.by_status(Status.VALIDATED):
            if not lead.verdict or lead.verdict.status != "APPROVED":
                continue
            self._event("handoff", "in", lead)
            if any(expired(p.fetched_at, self.cfg.limits.freshness_days) for p in lead.pages):
                self._refetch(lead, "handoff")
                continue
            try:
                row = self.registry.lookup(lead.siren)
            except FetchError as e:
                self._event("handoff", "error", lead, str(e))
                continue
            if row is None:
                lead.active = False  # gone from the registry search: treat as closed
            else:
                fresh = to_lead(row, lead.lead_id, self.run_id)
                lead.active, lead.category, lead.size_band = fresh.active, fresh.category, fresh.size_band
                lead.diffusion, lead.commercial_opposition = fresh.diffusion, fresh.commercial_opposition
            self._decide(lead, "revalidate")
            if lead.status == Status.VALIDATED and lead.verdict and lead.verdict.status == "APPROVED" \
                    and handoff_ready(lead):
                ready.append(lead)

        ready.sort(key=lambda x: (x.scores.need * x.scores.fit) if x.scores else 0, reverse=True)
        today = self.today().isoformat()
        room = max(0, self.cfg.limits.handoffs_per_day - self.store.handed_off_on(today))
        if self.cfg.shadow_mode:
            for lead in ready[:room]:
                self._event("handoff", "shadow", lead)
            return
        out = self.cfg.data_dir / "handoff" / f"{today}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            for lead in ready[:room]:
                f.write(json.dumps(handoff_record(lead), ensure_ascii=False) + "\n")
                self._advance(lead, "handoff", Status.HANDED_OFF)


def handoff_ready(lead: Lead) -> bool:
    return bool(
        lead.contact and lead.contact.email and lead.contact.source_url and lead.name
        and any(s.checked and s.type in NEED_SIGNALS for s in lead.signals)
        and lead.validated_at and now() - lead.validated_at <= timedelta(hours=24))


def handoff_record(lead: Lead) -> dict[str, object]:
    """What the Write stage gets: enough to write from the checked signals without crawling the site again, and
    the address's source so the email can say where it came from."""
    assert lead.contact and lead.scores
    return {
        "lead_id": lead.lead_id, "siren": lead.siren, "name": lead.name, "city": lead.city,
        "site": lead.site.url if lead.site else None,
        "contact": {"email": lead.contact.email, "source_url": lead.contact.source_url},
        "signals": [s.model_dump(include={"type", "value", "quote", "url"}) for s in lead.signals if s.checked],
        "tools": [t.name for t in lead.tools], "english": lead.english_at is not None,
        "need": lead.scores.need, "fit": lead.scores.fit,
        "validated_at": lead.validated_at.isoformat() if lead.validated_at else None,
    }
