from __future__ import annotations


def test_merged_candidate_aggregates_sources():
    from startup_hunter.schema import MergedCandidate, Source

    c = MergedCandidate(
        name="AgriTech Corp",
        description="Soil health through tech",
        sector="agritech",
        location="Vigo, Galicia",
        sources=[
            Source(found_via="web", source_url="https://acme-agritech.example/", source_date="2026-05-01"),
            Source(found_via="social", source_url="https://www.linkedin.com/company/example-acme-agritech/"),
        ],
        signals=["seed round 2026", "2K LinkedIn followers"],
    )
    assert len(c.sources) == 2
    assert c.needs_review is False
    assert c.possible_duplicate_of is None


import pytest


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bodegas García, S.L.", "bodegas garcia"),
        ("AGRITECH CORP S.L.U.", "agritech corp"),
        ("Innova Galicia S.A.", "innova galicia"),
        ("  Foo   Bar  ", "foo bar"),
        ("Cárnicas Ñoño SL", "carnicas nono"),
    ],
)
def test_normalize_name(raw, expected):
    from startup_hunter.consolidate import normalize_name

    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.acme-agritech.example/about", "acme-agritech.example"),
        ("http://Acme-AgriTech.example", "acme-agritech.example"),
        ("acme-agritech.example", "acme-agritech.example"),
        ("https://www.linkedin.com/company/agritech/", None),  # plataforma → no sirve
        ("", None),
        (None, None),
    ],
)
def test_registrable_domain(url, expected):
    from startup_hunter.consolidate import registrable_domain

    assert registrable_domain(url) == expected


def _hr(candidates, notes="ok", **_):
    from startup_hunter.schema import HunterResult
    return HunterResult(candidates=candidates, notes=notes)


def _sc(name, found_via, url, website=None, signal="signal", sector="agritech",
        location="Galicia", tax_id=None):
    from startup_hunter.schema import StartupCandidate
    return StartupCandidate(
        name=name, description="d", sector=sector, location=location,
        signal=signal, source_url=url, found_via=found_via, website=website,
        tax_id=tax_id,
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("B57285959", "B57285959"),
        ("b57285959", "B57285959"),
        (" B-57.285.959 ", "B57285959"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_tax_id(raw, expected):
    from startup_hunter.consolidate import normalize_tax_id

    assert normalize_tax_id(raw) == expected


def test_same_cif_merges_across_name_variants():
    """El caso que el nombre normalizado NO captura: la misma empresa escrita distinto.

    Es justo lo que hace fragil el registro de ya-vistas sin CIF (A5)."""
    from startup_hunter.consolidate import consolidate

    a = _sc("ACME CRISTALERIA SL", "bdns", "https://x.es/a", tax_id="B36000001")
    b = _sc("Cristalería Tea, S.L.U.", "bdns", "https://x.es/b", tax_id="B36000001")

    out = consolidate([_hr([a, b])])

    assert len(out) == 1
    assert len(out[0].sources) == 2
    assert out[0].tax_id == "B36000001"


def test_different_cif_is_not_merged_even_with_identical_name():
    """Dos empresas homónimas con CIF distinto son distintas: el CIF manda sobre el nombre."""
    from startup_hunter.consolidate import consolidate

    a = _sc("INNOVA SL", "bdns", "https://x.es/a", tax_id="B11111111")
    b = _sc("INNOVA SL", "bdns", "https://x.es/b", tax_id="B22222222")

    out = consolidate([_hr([a, b])])

    assert len(out) == 2


def test_different_cif_disconfirms_a_near_match_flag():
    """Un CIF distinto DEMUESTRA que no son duplicadas: no se marca needs_review."""
    from startup_hunter.consolidate import consolidate

    a = _sc("Innova Galicia", "bdns", "https://x.es/a", tax_id="B11111111")
    b = _sc("Innova Galicia Soluciones", "bdns", "https://x.es/b", tax_id="B22222222")

    out = consolidate([_hr([a, b])])

    assert len(out) == 2
    assert all(not c.needs_review for c in out)


def test_cif_absorbed_from_a_later_hit_becomes_a_merge_key():
    """Si una candidata sin CIF absorbe otra con CIF por nombre, el CIF pasa a fusionar."""
    from startup_hunter.consolidate import consolidate

    no_cif = _sc("AGRO VIGO SL", "tavily", "https://x.es/a", website="https://agrovigo.es")
    with_cif = _sc("AGRO VIGO SL", "bdns", "https://x.es/b", tax_id="B36000009")
    variant = _sc("Agro Vigo, S.L.U.", "bdns", "https://x.es/c", tax_id="B36000009")

    out = consolidate([_hr([no_cif, with_cif, variant])])

    assert len(out) == 1
    assert out[0].tax_id == "B36000009"
    assert len(out[0].sources) == 3


def test_merges_same_company_across_lanes_by_domain():
    from startup_hunter.consolidate import consolidate

    web = _hr([_sc("Acme AgriTech S.L.", "web", "https://acme-agritech.example/news", website="https://acme-agritech.example")])
    soc = _hr([_sc("AgriTech", "social", "https://www.linkedin.com/company/agritech/",
                   website="http://www.acme-agritech.example")], notes="social ok")

    out = consolidate([web, soc])

    assert len(out) == 1
    c = out[0]
    assert {s.found_via for s in c.sources} == {"web", "social"}
    assert c.needs_review is False


def test_distinct_companies_stay_separate():
    from startup_hunter.consolidate import consolidate

    out = consolidate([_hr([
        _sc("Acme AgriTech", "web", "https://acme-agritech.example", website="https://acme-agritech.example"),
        _sc("FoodCorp", "web", "https://foodcorp.es", website="https://foodcorp.es"),
    ])])
    assert len(out) == 2
    # Pin del límite inferior: nombres claramente distintos NO se marcan (no over-flag).
    assert all(not c.needs_review for c in out)


def test_distinct_companies_from_same_media_domain_stay_separate():
    """Dos empresas citadas por el mismo medio no son duplicados si no comparten web."""
    from startup_hunter.consolidate import consolidate

    out = consolidate([_hr([
        _sc("Alpha Robotics", "web", "https://capital-riesgo.es/article/alpha"),
        _sc("Beta Drones", "web", "https://capital-riesgo.es/article/beta"),
    ])])

    assert [c.name for c in out] == ["Alpha Robotics", "Beta Drones"]


def test_near_match_is_flagged_not_merged():
    from startup_hunter.consolidate import consolidate

    # Mismo nombre base + 'soluciones', sin dominio propio compartido → NO fusiona, MARCA.
    out = consolidate([_hr([
        _sc("Innova Galicia", "web", "https://innova.es", website="https://innova.es"),
        _sc("Innova Galicia Soluciones", "social",
            "https://www.linkedin.com/company/innova-sol/"),
    ])])
    assert len(out) == 2
    flagged = [c for c in out if c.needs_review]
    assert len(flagged) == 1
    assert flagged[0].possible_duplicate_of == "Innova Galicia"


def test_secondary_domain_does_not_alias_unrelated_company():
    """Regresión: un dominio secundario de una candidata fusionada-por-nombre NO debe
    convertirse en clave de fusión para otra empresa distinta que comparta ese dominio."""
    from startup_hunter.consolidate import consolidate

    # A y B comparten NOMBRE → se fusionan; B aporta un 2º dominio (beta.es).
    a = _sc("Alpha Startup", "web", "https://alpha.es/n", website="https://alpha.es")
    b = _sc("Alpha Startup", "social", "https://www.linkedin.com/company/alpha/",
            website="https://beta.es")
    # C es OTRA empresa que casualmente usa beta.es: NO debe fundirse en A.
    c = _sc("Completamente Diferente", "web", "https://beta.es/news", website="https://beta.es")

    out = consolidate([_hr([a, b, c])])
    assert sorted(x.name for x in out) == ["Alpha Startup", "Completamente Diferente"]


def test_empty_input_is_valid():
    from startup_hunter.consolidate import consolidate
    out = consolidate([])
    assert out == []
