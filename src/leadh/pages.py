import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

BOILERPLATE = ["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "iframe", "template"]


def sha256(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def same_site(url: str, site_domain: str) -> bool:
    host = domain_of(url)
    return host == site_domain or host.endswith("." + site_domain)


def clean_text(html: str) -> str:
    """What the Signal reader sees: body text without menus, footers or scripts, one block per line."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(BOILERPLATE):
        tag.decompose()
    lines = (" ".join(s.split()) for s in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def full_text(html: str) -> str:
    """All visible text, footer included: legal notices and contact addresses usually live there."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute url, link text) for every <a href>, fragments removed."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if url.startswith(("http://", "https://")):
            out.append((url, " ".join(a.get_text(" ").split())))
    return out


def siren_in(text: str, siren: str) -> bool:
    """SIREN, SIRET or VAT number written with any spacing or dots."""
    sep = r"[\s.  -]?"
    body = sep.join(siren)
    pattern = rf"(?:(?<!\d)|(?<=FR\d\d)){body}(?:(?:{sep}\d){{5}})?(?!\d)"
    return re.search(pattern, text) is not None


def is_english(html: str, url: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("html")
    if root is not None and str(root.get("lang", "")).lower().startswith("en"):
        return True
    for link in soup.find_all("link", hreflang=True):
        if str(link["hreflang"]).lower().startswith("en"):
            return True
    path = urlsplit(url).path.lower()
    return path == "/en" or path.startswith("/en/")
