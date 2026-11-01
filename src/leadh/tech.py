"""Tech detector: invoicing and accounting tools visible in a site's code. A known tool often covers compliance."""

import re

from leadh.models import Tool

# Starting fingerprints: script hosts, embed and payment-portal URLs, generator tags. Extend from labelled sites.
FINGERPRINTS: dict[str, list[str]] = {
    "Pennylane": [r"pennylane\.com", r"pennylane\.tech"],
    "Sellsy": [r"sellsy\.(?:com|fr|net)"],
    "Axonaut": [r"axonaut\.com"],
    "Odoo": [r"<meta[^>]+name=[\"']generator[\"'][^>]+odoo", r"/web/assets/[^\"']*web\.assets_frontend",
             r"odoo\.com"],
    "QuickBooks": [r"quickbooks\.intuit\.com", r"connect\.intuit\.com"],
    "Zoho Invoice / Books": [r"zohopublic\.(?:com|eu)", r"invoice\.zoho\.(?:com|eu)", r"books\.zoho\.(?:com|eu)"],
    "Tiime": [r"tiime\.fr"],
    "Evoliz": [r"evoliz\.(?:com|fr|io)"],
    "Henrri": [r"henrri\.com"],
    "Freebe": [r"freebe\.me"],
    "Abby": [r"abby\.fr"],
    "Facture.net": [r"facture\.net"],
    "Stripe Invoicing": [r"invoice\.stripe\.com", r"pay\.stripe\.com/invoice"],
    "Chargebee": [r"chargebee\.com"],
    "Cegid": [r"cegid\.(?:com|cloud)"],
    "Sage": [r"sage\.(?:com|fr)/[^\"']*(?:invoice|facture|portal)"],
    "Dolibarr": [r"dolibarr", r"/public/payment/newpayment\.php"],
}

COMPILED = {name: [re.compile(p, re.I) for p in patterns] for name, patterns in FINGERPRINTS.items()}


def detect(pages: list[tuple[str, str]]) -> list[Tool]:
    """Tools found in (url, html) pages, first page each was seen on."""
    found: dict[str, str] = {}
    for url, html in pages:
        for name, patterns in COMPILED.items():
            if name not in found and any(p.search(html) for p in patterns):
                found[name] = url
    return [Tool(name=n, url=u) for n, u in found.items()]
