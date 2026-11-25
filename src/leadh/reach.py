"""Agent-Reach (https://github.com/Panniantong/Agent-Reach) as an optional helper: a second search engine for
candidate sites, a reader for pages that refuse LeadH's own crawler, and public LinkedIn job ads.

It runs as a separate `agent-reach` command that prints one JSON object. If the command is missing or fails, every
helper here returns nothing and the pipeline carries on as before. Whatever comes back is data only: a found site
still has to show the SIREN, and a job ad still has to carry the quote the Signal reader cites."""

import html
import json
import logging
import re
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from leadh.config import ReachConfig

log = logging.getLogger("leadh")

Runner = Callable[[list[str], float], str]


def run_command(args: list[str], timeout: float) -> str:
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    return done.stdout


@dataclass
class Item:
    title: str
    url: str
    author: str
    text: str
    raw: dict[str, Any]


# Codes after which a channel stays off for the whole run: retrying cannot help until you act.
STOP_FOR_RUN = frozenset({"NEED_LOGIN", "NEED_SETUP", "PLATFORM_BLOCKED"})
# Codes after which a channel waits and is tried again: a rate limit, a full browser, a backend paused upstream.
WAIT_AND_RETRY = frozenset({"RATE_LIMITED", "BUSY", "UPSTREAM_BROKEN", "TIMEOUT"})


def retry_after(data: dict[str, Any]) -> float | None:
    """The wait Agent-Reach asks for, from the error or from its per-backend attempts."""
    values = [(data.get("error") or {}).get("retry_after_seconds")]
    values += [a.get("retry_after_seconds") for a in (data.get("meta") or {}).get("attempts") or []
               if isinstance(a, dict)]
    found = [float(v) for v in values if isinstance(v, int | float) and v > 0]
    return max(found) if found else None


class AgentReach:
    def __init__(self, cfg: ReachConfig, runner: Runner | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.runner = runner
        self.sleep = sleep
        self.clock = clock
        self.missing_warned = False
        self.stopped: set[str] = set()  # channels off for the rest of the run
        self.resume_at: dict[str, float] = {}  # channels waiting out a rate limit, until this clock time
        self.strikes: dict[str, int] = {}  # wait-and-retry answers in a row, per channel
        self.last_call: dict[str, float] = {}
        # Web searches skipped or failed for a passing reason. The pipeline compares it before and after a lead: a
        # lead whose site lookup was cut short is tried again next run instead of being marked no_site.
        self.misses = 0

    def available(self) -> bool:
        if self.runner is not None:
            return True
        if shutil.which(self.cfg.command):
            self.runner = run_command
            return True
        if not self.missing_warned:
            log.warning("agent-reach not found (reach.command=%s); its helpers are skipped", self.cfg.command)
            self.missing_warned = True
        return False

    def call(self, channel: str, action: str, *args: str) -> list[Item]:
        """Items from `agent-reach <channel> <action> <args>`, or [] on any failure."""
        if not self.cfg.enabled or channel in self.stopped or not self.available():
            return []
        searching = (channel, action) == ("web", "search")
        wait = self.resume_at.get(channel, 0.0) - self.clock()
        if wait > 0:
            if wait > self.cfg.max_wait_s:
                self.misses += searching
                return []
            self.sleep(wait)
        pace = self.last_call.get(channel, -1e9) + self.cfg.min_interval_s - self.clock()
        if pace > 0:
            self.sleep(pace)
        self.last_call[channel] = self.clock()
        assert self.runner is not None
        cmd = [self.cfg.command, channel, action, *args, "--timeout", str(int(self.cfg.timeout_s))]
        try:
            out = self.runner(cmd, self.cfg.timeout_s + 15)
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError) as e:
            log.warning("agent-reach %s %s failed: %s", channel, action, type(e).__name__)
            self.misses += searching
            return []
        if not data.get("ok"):
            self.misses += searching and (data.get("error") or {}).get("code") in WAIT_AND_RETRY
            return self._failed(channel, action, data)
        self.strikes[channel] = 0
        items = data.get("items")
        if items is None and isinstance(data.get("result"), dict):
            items = data["result"].get("items")
        return [Item(title=str(i.get("title") or ""), url=str(i.get("url") or ""), author=str(i.get("author") or ""),
                     text=str(i.get("text") or ""), raw=i.get("raw") or {})
                for i in items or [] if isinstance(i, dict)]

    def _failed(self, channel: str, action: str, data: dict[str, Any]) -> list[Item]:
        """Agent-Reach's rule is never to retry in a loop or switch backends: a channel that is told to wait waits
        the time it is given (or `reach.pause_s`) before its next call, and after `max_strikes` such answers in a
        row it is off for the run."""
        code = (data.get("error") or {}).get("code", "UNKNOWN")
        if code in STOP_FOR_RUN:
            self.stopped.add(channel)
            log.warning("agent-reach %s %s: %s; %s is off for this run", channel, action, code, channel)
        elif code in WAIT_AND_RETRY:
            self.strikes[channel] = self.strikes.get(channel, 0) + 1
            if self.strikes[channel] >= self.cfg.max_strikes:
                self.stopped.add(channel)
                log.warning("agent-reach %s %s: %s %d times in a row; %s is off for this run", channel, action,
                            code, self.strikes[channel], channel)
            else:
                pause = retry_after(data) or self.cfg.pause_s
                self.resume_at[channel] = self.clock() + pause
                log.info("agent-reach %s %s: %s; waiting %.0fs", channel, action, code, pause)
        else:
            log.info("agent-reach %s %s: %s", channel, action, code)
        return []

    def search(self, query: str) -> list[str]:
        return [i.url for i in self.call("web", "search", query, "--limit", str(self.cfg.search_limit)) if i.url]

    def read(self, url: str) -> str | None:
        """The page as HTML built from Agent-Reach's Markdown text, or None."""
        items = self.call("web", "read", url)
        if not items or not items[0].text.strip():
            return None
        return markdown_to_html(items[0].title, items[0].text)

    def linkedin_jobs(self, company: str, location: str | None) -> list[Item]:
        args = [company, "--limit", str(self.cfg.jobs_limit)]
        if location:
            args += ["--location", location]
        return self.call("linkedin", "jobs", *args)


class ReachSearch:
    """`search.provider` entry "agentreach": Agent-Reach web search (Exa). Results are only candidates."""

    def __init__(self, reach: AgentReach) -> None:
        self.reach = reach

    def search(self, query: str) -> list[str]:
        return self.reach.search(query)


MD_LINK = re.compile(r"!?\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")


def markdown_to_html(title: str, text: str) -> str:
    """Just enough HTML for the later steps: one <p> per line, Markdown links as <a href>, so the Site matcher can
    follow links and the Evidence check sees the same text the Signal reader saw."""
    out = ["<html><head>", f"<title>{html.escape(title)}</title>" if title else "", "</head><body>"]
    for line in text.splitlines():
        line = line.strip().lstrip("#>*- ").strip()
        if not line:
            continue
        parts, last = [], 0
        for m in MD_LINK.finditer(line):
            parts.append(html.escape(line[last:m.start()]))
            if not m.group(0).startswith("!"):
                parts.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
            last = m.end()
        parts.append(html.escape(line[last:]))
        out.append(f"<p>{''.join(parts).replace('**', '')}</p>")
    out.append("</body></html>")
    return "\n".join(out)


ADMIN_JOB = re.compile(r"comptab|factur|administrati|gestionnaire|assistant|account|invoic|billing|recouvrement|"
                       r"credit manager|payroll|paie|office manager", re.I)
NOT_COMPANY = re.compile(r"\b(sas|sasu|sarl|eurl|sa|sci|snc|selarl|scop|groupe|france)\b")


def _compact(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", NOT_COMPANY.sub(" ", ascii_text))


# LinkedIn's public job URLs end in "<role>-at-<company>-<job id>".
URL_COMPANY = re.compile(r"/jobs/view/(?:.*-)?at-([a-z0-9-]+?)-\d+/?$", re.I)
TITLE_COMPANY = [
    re.compile(r"^(.+?)\s+(?:recrute|is hiring|hiring|embauche)\b", re.I),
    re.compile(r"\s(?:at|chez)\s+(.+?)(?:\s+[—–|-]\s+.*|\s+in\s+.*)?$", re.I),
]


def company_of(item: Item) -> str:
    """The company posting the ad. Agent-Reach's public LinkedIn results carry no company field (author is empty),
    so the name comes from the URL slug, then the title, then the line under the heading in the text."""
    for key in ("company", "company_name", "companyName", "employer", "hiring_organization"):
        value = item.raw.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if value:
            return str(value)
    if item.author:
        return item.author
    m = URL_COMPANY.search(item.url.split("?")[0])
    if m:
        return m.group(1).replace("-", " ")
    for pattern in TITLE_COMPANY:
        m = pattern.search(item.title)
        if m:
            return m.group(1).strip()
    lines = [ln.strip("# ").strip() for ln in item.text.splitlines() if ln.strip()]
    return lines[1] if len(lines) > 1 else ""


ABROAD = re.compile(r"\b(london|new york|san francisco|singapore|berlin|munich|madrid|barcelona|milan|amsterdam|"
                    r"brussels|bruxelles|geneva|gen[eè]ve|zurich|lisbon|dublin|warsaw|bangalore|mumbai|montr[eé]al|"
                    r"toronto|united states|united kingdom|germany|spain|italy|india|canada|usa|uk)\b", re.I)


def in_france(item: Item, city: str | None) -> bool:
    """Results come from every country: keep ads shown in France, or naming the lead's city, or naming nowhere."""
    head = f"{item.title}\n{item.text[:400]}"
    if city and city.lower() in head.lower():
        return True
    host = item.url.split("/")[2] if item.url.count("/") >= 2 else ""
    if host.startswith("fr."):
        return not ABROAD.search(item.title)
    return not ABROAD.search(head)


def job_ads(items: list[Item], company: str, city: str | None = None) -> list[Item]:
    """Admin, accounting or invoicing ads posted by this company (its name, not just a mention in the text), in
    France."""
    names = [n for n in (_compact(p) for p in [*re.findall(r"\(([^)]*)\)", company),
                                                 re.sub(r"\(.*?\)", " ", company)]) if len(n) >= 4]
    out = []
    for item in items:
        posted_by = _compact(company_of(item))
        # The posting name must match the company's whole name: "capgemini invent" is not "capgemini".
        if not item.url or len(posted_by) < 4 or posted_by not in names:
            continue
        if not in_france(item, city):
            continue
        if ADMIN_JOB.search(item.title) or ADMIN_JOB.search(item.text[:300]):
            out.append(item)
    return out


def job_page_html(item: Item) -> str:
    body = item.text.strip() or item.title
    return markdown_to_html(item.title, f"{item.title}\n{company_of(item)}\n{body}")
