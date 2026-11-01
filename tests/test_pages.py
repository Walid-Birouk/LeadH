from leadh.pages import clean_text, domain_of, is_english, same_site, siren_in

SIREN = "812345674"


def test_siren_in_any_spelling() -> None:
    assert siren_in("RCS Paris 812 345 674", SIREN)
    assert siren_in("SIREN : 812.345.674", SIREN)
    assert siren_in("SIRET 812 345 674 00012", SIREN)
    assert siren_in("TVA FR12812345674", SIREN)
    assert siren_in("TVA FR 12 812345674", SIREN)


def test_siren_in_rejects_other_numbers() -> None:
    assert not siren_in("RCS Paris 812 345 675", SIREN)
    assert not siren_in("Tél. 9812345674", SIREN)
    assert not siren_in("SIRET 812345674000123", SIREN)


def test_clean_text_drops_menus_footers_and_scripts() -> None:
    html = "<nav>Menu</nav><main><p>Bonjour  le\n monde</p><script>x=1</script></main><footer>Pied</footer>"
    assert clean_text(html) == "Bonjour le\nmonde"


def test_english_from_lang_hreflang_or_path() -> None:
    assert is_english('<html lang="en-GB"></html>', "https://a.fr/")
    assert is_english('<link rel="alternate" hreflang="en" href="/en/">', "https://a.fr/")
    assert is_english("<p>x</p>", "https://a.fr/en/about")
    assert not is_english('<html lang="fr"></html>', "https://a.fr/entreprise")


def test_domains() -> None:
    assert domain_of("https://www.Exemple.fr:443/x") == "exemple.fr"
    assert same_site("https://blog.exemple.fr/a", "exemple.fr")
    assert not same_site("https://exemple.fr.evil.com/", "exemple.fr")
