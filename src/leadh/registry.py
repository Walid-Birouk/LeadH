"""Registry scout: candidates from the public Recherche d'entreprises API (Sirene and RNE data, no key)."""

from collections.abc import Iterator
from typing import Any

from leadh.config import Config
from leadh.http import Http
from leadh.models import Lead

# Sirene employee bands ("tranche d'effectif"); 01 to 12 is 1 to 49 employees.
SMALL_BANDS = frozenset({"01", "02", "03", "11", "12"})
SME_BANDS = SMALL_BANDS | {"00", "NN", "21", "22", "31", "32"}


def naf_matches(naf: str | None, prefixes: list[str]) -> bool:
    return bool(naf) and any(str(naf).startswith(p) for p in prefixes)


def is_sme(lead: Lead) -> bool:
    # INSEE computes the category yearly, so companies created since the last run have none yet; for those the
    # employee band (under 250) stands in.
    if lead.category:
        return lead.category == "PME"
    return (lead.size_band or "NN") in SME_BANDS


def to_lead(row: dict[str, Any], lead_id: str, run_id: str) -> Lead:
    siege = row.get("siege") or {}
    return Lead(
        lead_id=lead_id,
        siren=row["siren"],
        name=row.get("nom_raison_sociale") or row.get("nom_complet") or row["siren"],
        naf=row.get("activite_principale"),
        category=row.get("categorie_entreprise"),
        size_band=row.get("tranche_effectif_salarie"),
        created_on=row.get("date_creation"),
        city=siege.get("libelle_commune"),
        postal_code=siege.get("code_postal"),
        region=siege.get("region"),
        active=row.get("etat_administratif") == "A",
        diffusion=row.get("statut_diffusion") or "O",
        commercial_opposition=bool(row.get("opposition_diffusion_commerciale")),
        run_id=run_id,
    )


class Registry:
    def __init__(self, cfg: Config, http: Http) -> None:
        self.cfg = cfg
        self.http = http

    def _search(self, params: dict[str, str]) -> dict[str, Any]:
        resp = self.http.get(f"{self.cfg.registry.base_url}/search", params=params,
                             interval_s=self.cfg.registry.request_interval_s, respect_robots=False)
        data: dict[str, Any] = resp.json()
        return data

    def candidates(self) -> Iterator[dict[str, Any]]:
        """Active SMEs on the configured NAF codes and regions, page by page."""
        reg = self.cfg.registry
        for naf in reg.naf_codes:
            if naf_matches(naf, self.cfg.naf.excluded_prefixes):
                continue
            for region in reg.regions or [""]:
                page = 1
                while True:
                    params = {"activite_principale": naf, "categorie_entreprise": "PME", "etat_administratif": "A",
                              "per_page": str(reg.per_page), "page": str(page)}
                    if region:
                        params["region"] = region
                    data = self._search(params)
                    for row in data.get("results", []):
                        if not self.processable(row):
                            continue
                        yield row
                    if page >= int(data.get("total_pages") or 1):
                        break
                    page += 1

    def processable(self, row: dict[str, Any]) -> bool:
        """Companies that objected to publication or to reuse for prospecting are dropped before they are stored."""
        if row.get("statut_diffusion") == "P":
            return False
        return not (self.cfg.registry.skip_commercial_opposition and row.get("opposition_diffusion_commerciale"))

    def lookup(self, siren: str) -> dict[str, Any] | None:
        """Fresh registry entry for one SIREN, for the Validator's second pass."""
        rows: list[dict[str, Any]] = self._search({"q": siren, "per_page": "1"}).get("results", [])
        return next((row for row in rows if row.get("siren") == siren), None)
