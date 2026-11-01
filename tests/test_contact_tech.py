from leadh.contact import best_contact, emails_on
from leadh.tech import detect


def test_generic_address_on_own_domain_preferred() -> None:
    pages = [("https://a.fr/equipe", "<p>jean.martin@a.fr</p>"),
             ("https://a.fr/contact", '<a href="mailto:contact@a.fr?subject=Hi">écrire</a> hello@agency.com')]
    c = best_contact(pages, "a.fr")
    assert c and c.email == "contact@a.fr" and c.kind == "generic" and c.source_url == "https://a.fr/contact"


def test_nominative_when_nothing_generic() -> None:
    c = best_contact([("https://a.fr/", "<p>jean.martin@a.fr</p>")], "a.fr")
    assert c and c.kind == "nominative"


def test_webmail_only_as_last_resort_and_flagged() -> None:
    c = best_contact([("https://a.fr/", "<p>monstudio@gmail.com, hello@agency.com</p>")], "a.fr")
    assert c and c.email == "monstudio@gmail.com" and c.free_mail


def test_other_companies_and_assets_ignored() -> None:
    assert best_contact([("https://a.fr/", '<p>hello@agency.com</p><img src="logo@2x.png">')], "a.fr") is None
    assert emails_on("<p>logo@2x.png noreply@a.fr</p>") == []


def test_obfuscated_address() -> None:
    assert emails_on("<p>contact [at] a [dot] fr</p>") == ["contact@a.fr"]


def test_no_address_means_no_contact() -> None:
    assert best_contact([("https://a.fr/contact", "<form><input name=email></form>")], "a.fr") is None


def test_tech_detector_finds_tool_and_page() -> None:
    tools = detect([("https://a.fr/", "<p>hi</p>"),
                    ("https://a.fr/pay", '<script src="https://app.pennylane.com/w.js"></script>')])
    assert [(t.name, t.url) for t in tools] == [("Pennylane", "https://a.fr/pay")]
    assert detect([("https://a.fr/", "<p>Facturation simple</p>")]) == []


def test_guessed_domains_from_name() -> None:
    from leadh.search import GuessDomains

    g = GuessDomains()
    assert g.search('"ÉTOILE & FILS SAS" PARIS') == [
        "https://etoileetfils.fr/", "https://etoileetfils.com/", "https://etoile-et-fils.fr/",
        "https://etoile-et-fils.com/"]
    assert g.search('"ALTRNATIV.COM" PARIS')[0] == "https://altrnativ.com/"
    assert g.search('"JEAN DUPONT (CLOUDBOOST)" PARIS')[:2] == ["https://cloudboost.fr/", "https://cloudboost.com/"]
    assert g.search('"812345674"') == []


def test_brave_query_ranks_snippets_showing_the_siren_first() -> None:
    import httpx

    from leadh.config import load_config
    from leadh.search import BraveSearch, ChainedSearch, StaticSearch

    from .conftest import ROOT, make_http

    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"web": {"results": [
            {"url": "https://blog.example.com/top-agences", "description": "Les meilleures agences"},
            {"url": "https://a.fr/mentions-legales", "description": "RCS Paris 812 345 674"}]}})

    cfg = load_config(ROOT / "config" / "leadh.yaml")
    brave = BraveSearch(make_http(cfg, handler), "key")
    assert brave.search('"812345674"') == ["https://a.fr/mentions-legales", "https://blog.example.com/top-agences"]
    params = seen[0].params
    assert "-site:societe.com" in params["q"] and params["country"] == "FR" and params["spellcheck"] == "false"
    assert seen[0].host == "api.search.brave.com"

    chained = ChainedSearch([brave, StaticSearch({'"812345674"': ["https://a.fr/mentions-legales", "https://b.fr/"]})])
    assert chained.search('"812345674"')[-1] == "https://b.fr/"


def test_company_name_mailbox_is_generic() -> None:
    from leadh.contact import is_generic

    assert is_generic("ypok@ypok.com") and is_generic("short-edition@shortedition.com")
    assert not is_generic("marie@ypok.com")
