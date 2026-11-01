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
