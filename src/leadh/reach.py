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


class AgentReach:
    def __init__(self, cfg: ReachConfig, runner: Runner | None = None) -> None:
        self.cfg = cfg
        self.runner = runner
        self.missing_warned = False
        self.paused: set[str] = set()  # channels that answered "stop" (rate limit, captcha, login) this run

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
        if not self.cfg.enabled or channel in self.paused or not self.available():
            return []
        assert self.runner is not None
        cmd = [self.cfg.command, channel, action, *args, "--timeout", str(int(self.cfg.timeout_s))]
        try:
            out = self.runner(cmd, self.cfg.timeout_s + 15)
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError) as e:
            log.warning("agent-reach %s %s failed: %s", channel, action, type(e).__name__)
            return []
        if not data.get("ok"):
            code = (data.get("error") or {}).get("code", "UNKNOWN")
            # Agent-Reach's own rule: on these, stop this channel instead of retrying or switching backends.
            if code in ("RATE_LIMITED", "PLATFORM_BLOCKED", "NEED_LOGIN", "BUSY", "UPSTREAM_BROKEN", "NEED_SETUP"):
                self.paused.add(channel)
            log.info("agent-reach %s %s: %s", channel, action, code)
            return []
        items = data.get("items")
        if items is None and isinstance(data.get("result"), dict):
            items = data["result"].get("items")
        return [Item(title=str(i.get("title") or ""), url=str(i.get("url") or ""), author=str(i.get("author") or ""),
                     text=str(i.get("text") or ""), raw=i.get("raw") or {})
                for i in items or [] if isinstance(i, dict)]

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


def company_of(item: Item) -> str:
    for key in ("company", "company_name", "companyName", "employer", "hiring_organization"):
        value = item.raw.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if value:
            return str(value)
    return item.author


def job_ads(items: list[Item], company: str) -> list[Item]:
    """Admin, accounting or invoicing ads posted by this company (its name, not just a mention in the text)."""
    names = [n for n in (_compact(p) for p in [*re.findall(r"\(([^)]*)\)", company),
                                                 re.sub(r"\(.*?\)", " ", company)]) if len(n) >= 4]
    out = []
    for item in items:
        posted_by = _compact(company_of(item)) or _compact(item.title)
        if not item.url or len(posted_by) < 4 or not any(n in posted_by or posted_by in n for n in names):
            continue
        if ADMIN_JOB.search(item.title) or ADMIN_JOB.search(item.text[:300]):
            out.append(item)
    return out


def job_page_html(item: Item) -> str:
    body = item.text.strip() or item.title
    return markdown_to_html(item.title, f"{item.title}\n{company_of(item)}\n{body}")
