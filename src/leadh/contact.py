"""Published contact addresses. Only addresses printed on the company's own pages count; nothing is guessed."""

import re

from bs4 import BeautifulSoup

from leadh.models import Contact
from leadh.pages import full_text

GENERIC_LOCAL_PARTS = frozenset({
    "contact", "info", "infos", "hello", "bonjour", "hi", "office", "accueil", "admin", "administration",
    "compta", "comptabilite", "facturation", "factures", "invoice", "invoices", "billing", "sales", "commercial",
    "vente", "ventes", "team", "equipe", "agence", "studio", "general", "secretariat", "direction", "rh", "jobs",
    "recrutement", "support", "service", "serviceclient", "client", "clients", "devis", "projets", "business",
})
FREE_MAIL = frozenset({
    "gmail.com", "yahoo.fr", "yahoo.com", "hotmail.fr", "hotmail.com", "outlook.fr", "outlook.com", "live.fr",
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "icloud.com", "gmx.fr", "proton.me",
    "protonmail.com", "aol.com", "neuf.fr", "bbox.fr",
})
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
OBFUSCATED_AT = re.compile(r"\s*(?:\[at\]|\(at\)|\[arobase\]|\(arobase\)|\s+at\s+)\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*(?:\[dot\]|\(dot\)|\[point\]|\(point\))\s*", re.I)
NOT_A_MAILBOX = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|css|js)$|^(?:noreply|no-reply|donotreply)@|sentry|wixpress",
                           re.I)


def emails_on(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = [str(a["href"])[7:].split("?")[0] for a in soup.find_all("a", href=True)
             if str(a["href"]).lower().startswith("mailto:")]
    text = OBFUSCATED_DOT.sub(".", OBFUSCATED_AT.sub("@", full_text(html)))
    found += EMAIL.findall(text)
    out: list[str] = []
    for e in (x.strip().strip(".").lower() for x in found):
        if EMAIL.fullmatch(e) and not NOT_A_MAILBOX.search(e) and e not in out:
            out.append(e)
    return out


def is_generic(email: str) -> bool:
    local = email.split("@")[0].split("+")[0]
    return local in GENERIC_LOCAL_PARTS or re.sub(r"[._-]", "", local) in GENERIC_LOCAL_PARTS


def best_contact(pages: list[tuple[str, str]], site_domain: str) -> Contact | None:
    """The address to use, from (url, html) pages: on the site's own domain first, generic before nominative.
    Webmail addresses are kept only when the site publishes nothing else, and are flagged for review."""
    own: list[Contact] = []
    webmail: list[Contact] = []
    for url, html in pages:
        for email in emails_on(html):
            domain = email.split("@")[1]
            c = Contact(email=email, kind="generic" if is_generic(email) else "nominative",
                        free_mail=domain in FREE_MAIL, source_url=url)
            if domain == site_domain or domain.endswith("." + site_domain):
                own.append(c)
            elif c.free_mail:
                webmail.append(c)
    for group in (own, webmail):
        for c in group:
            if c.kind == "generic":
                return c
        if group:
            return group[0]
    return None
