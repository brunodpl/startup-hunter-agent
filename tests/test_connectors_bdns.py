"""Tests del conector BDNS (API oficial del SNPSAP).

Los payloads reproducen la forma REAL verificada contra la API el 20-jul-2026: respuesta
Spring (``content`` + ``totalElements``), ``beneficiario`` como ``"CIF NOMBRE"``, fechas
ISO en la respuesta y ``DD/MM/AAAA`` en los parámetros.
"""

from __future__ import annotations

from startup_hunter.budget import Budget
from startup_hunter.connectors.bdns import BdnsConnector, is_company, split_beneficiary
from startup_hunter.schema import QuerySpec

_REGIONS = [
    {
        "id": 1,
        "descripcion": "ES - ESPAÑA",
        "children": [
            {
                "id": 3,
                "descripcion": "ES11 - GALICIA",
                "children": [{"id": 7, "descripcion": "ES114 - Pontevedra", "children": []}],
            },
            {"id": 12, "descripcion": "ES30 - COMUNIDAD DE MADRID", "children": []},
        ],
    }
]


def _record(beneficiario: str, **kwargs) -> dict:
    base = {
        "id": 155470512,
        "codConcesion": "SB155470512",
        "fechaConcesion": "2026-07-16",
        "beneficiario": beneficiario,
        "importe": 16625.0,
        "convocatoria": "Orden de 21 de mayo de 2026 por la que se aprueban las bases",
        "numeroConvocatoria": "885616",
        "nivel1": "AUTONOMICA",
        "nivel2": "GALICIA",
        "nivel3": "AGENCIA GALLEGA DE INNOVACION",
        "urlBR": "https://www.xunta.gal/diario-oficial-galicia",
    }
    base.update(kwargs)
    return base


class _FakeApi:
    """Doble del API: sirve páginas y registra los parámetros pedidos."""

    def __init__(self, *pages: list[dict]) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if url.endswith("/regiones"):
            return _REGIONS
        if not self._pages:
            return {"content": [], "totalElements": 0}
        return {"content": self._pages.pop(0), "totalElements": 999}

    @property
    def search_params(self) -> list[dict]:
        return [p for u, p in self.calls if "concesiones" in u]


def _spec(**kwargs) -> QuerySpec:
    return QuerySpec(query="agritech", location="Galicia", **kwargs)


# --- clasificacion de beneficiario ----------------------------------------


def test_companies_are_recognised_by_their_cif():
    assert is_company("B57285959 EMPRESA DEMO AMBIENTAL, SL")
    assert is_company("A15022510 INDUSTRIAS DEL NOROESTE SA")


def test_masked_individuals_are_rejected():
    """La BDNS publica ayudas a particulares con el NIF enmascarado. No son clientes, y
    son datos personales que no pintan nada en la cartera."""
    assert not is_company("***8386** FERNANDO EMILIO FREIRE VAZQUEZ")
    assert not is_company("****1234* MARIA LOPEZ GARCIA")
    assert not is_company("")


def test_legal_form_is_a_fallback_when_the_cif_is_missing():
    assert is_company("BODEGAS RIAS BAIXAS SOCIEDAD COOPERATIVA")
    assert not is_company("JUAN PEREZ GOMEZ")


def test_split_beneficiary_separates_cif_from_name():
    assert split_beneficiary("B57285959 EMPRESA DEMO, SL") == (
        "EMPRESA DEMO, SL",
        "B57285959",
    )
    assert split_beneficiary("BODEGAS SIN CIF SL") == ("BODEGAS SIN CIF SL", None)


# --- consulta --------------------------------------------------------------


def test_galicia_resolves_to_its_nuts_region_id():
    api = _FakeApi([_record("B57285959 AGRO VIGO SL")])

    BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert api.search_params[0]["regiones"] == 3


def test_unknown_location_falls_back_to_the_live_region_tree():
    api = _FakeApi([_record("B57285959 MADRID TECH SL")])

    BdnsConnector(fetcher=api).fetch(
        QuerySpec(query="saas", location="Comunidad de Madrid"), Budget()
    )

    assert api.search_params[0]["regiones"] == 12


def test_a_location_that_cannot_be_resolved_searches_without_geo_filter():
    """Mejor una busqueda sin acotar que un fallo: el pipeline sigue teniendo datos."""
    api = _FakeApi([_record("B57285959 AGRO SL")])

    BdnsConnector(fetcher=api).fetch(QuerySpec(query="agritech", location="Laponia"), Budget())

    assert "regiones" not in api.search_params[0]


def test_dates_use_the_api_format_not_iso():
    """El API pide DD/MM/AAAA en los parametros aunque devuelva ISO en la respuesta."""
    import re

    api = _FakeApi([_record("B57285959 AGRO SL")])
    BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    params = api.search_params[0]
    assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", params["fechaDesde"])
    assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", params["fechaHasta"])


# --- mapeo -----------------------------------------------------------------


def test_hit_carries_amount_date_and_evidence():
    api = _FakeApi([_record("B57285959 AGRO VIGO SL")])

    hits = BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert len(hits) == 1
    hit = hits[0]
    assert hit.name == "AGRO VIGO SL"
    assert hit.raw["cif"] == "B57285959"
    # El CIF sube al esquema como identidad canonica, no solo enterrado en raw.
    assert hit.tax_id == "B57285959"
    assert "16625" in hit.signal
    assert "2026-07-16" in hit.signal
    assert hit.source_date == "2026-07-16"
    assert hit.location == "GALICIA"
    # La evidencia enlaza a la CONVOCATORIA concreta, no al buscador generico.
    assert hit.source_url == "https://www.infosubvenciones.es/bdnstrans/GE/es/convocatoria/885616"


def test_evidence_url_is_specific_per_convocatoria():
    """Auditoria #4: dos ayudas de convocatorias distintas ya NO comparten la misma URL."""
    api = _FakeApi(
        [
            _record("B57285959 AGRO SL", numeroConvocatoria="111111"),
            _record("B36201903 OTRA SL", numeroConvocatoria="222222"),
        ]
    )

    hits = BdnsConnector(fetcher=api).fetch(_spec(max_per_source=5), Budget())

    urls = {h.source_url for h in hits}
    assert len(urls) == 2
    assert all("/convocatoria/" in u for u in urls)


def test_evidence_falls_back_to_the_generic_page_without_a_convocatoria_number():
    """Sin numeroConvocatoria no inventamos una URL: cae al buscador publico honesto."""
    api = _FakeApi([_record("B57285959 AGRO SL", numeroConvocatoria=None)])

    hits = BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert hits[0].source_url == "https://www.infosubvenciones.es/bdnstrans/GE/es/concesiones"


def test_individuals_are_filtered_out_of_the_results():
    api = _FakeApi(
        [
            _record("***8386** FERNANDO EMILIO FREIRE VAZQUEZ"),
            _record("B57285959 AGRO VIGO SL"),
        ]
    )

    hits = BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert [h.name for h in hits] == ["AGRO VIGO SL"]


def test_the_same_company_with_several_grants_appears_once():
    api = _FakeApi(
        [
            _record("B57285959 AGRO VIGO SL", importe=1000.0),
            _record("B57285959 AGRO VIGO SL", importe=2000.0),
        ]
    )

    hits = BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert len(hits) == 1


def test_pagination_stops_on_an_empty_page():
    api = _FakeApi([_record("B57285959 AGRO SL")], [])

    BdnsConnector(fetcher=api).fetch(_spec(max_per_source=50), Budget())

    assert len(api.search_params) == 2


def test_budget_caps_the_pages_requested():
    pages = [[_record(f"B5728595{i} EMPRESA {i} SL")] for i in range(5)]
    api = _FakeApi(*pages)

    BdnsConnector(fetcher=api).fetch(_spec(max_per_source=99), Budget(max_calls_per_source=2))

    assert len(api.search_params) == 2


def test_respects_max_per_source():
    page = [_record(f"B572859{i:02d} EMPRESA {i} SL") for i in range(20)]
    api = _FakeApi(page)

    hits = BdnsConnector(fetcher=api).fetch(_spec(max_per_source=3), Budget())

    assert len(hits) == 3


# --- acotado por tema de convocatoria -------------------------------------


def test_theme_is_passed_as_the_description_filter():
    api = _FakeApi([_record("B57285959 AGRO VIGO SL")])

    BdnsConnector(fetcher=api, theme="digitalizacion").fetch(_spec(), Budget())

    assert api.search_params[0]["descripcion"] == "digitalizacion"


def test_no_theme_means_no_description_filter():
    api = _FakeApi([_record("B57285959 AGRO VIGO SL")])

    BdnsConnector(fetcher=api).fetch(_spec(), Budget())

    assert "descripcion" not in api.search_params[0]


def test_a_theme_matching_nothing_retries_without_it_and_says_so():
    """Un tema inexistente vaciaria el resultado en silencio: el peor fallo posible."""
    api = _FakeApi([], [_record("B57285959 AGRO VIGO SL")])
    connector = BdnsConnector(fetcher=api, theme="agritech")

    hits = connector.fetch(_spec(), Budget())

    assert [h.name for h in hits] == ["AGRO VIGO SL"]
    assert "agritech" in connector.notes[0]
    # El reintento va sin el filtro de tema.
    assert "descripcion" not in api.search_params[-1]


def test_api_error_disguised_as_http_200_is_not_read_as_no_results():
    """El API responde 200 con {"codigo": "ERR_MANTENIMIENTO_BBDD"} cuando la consulta le
    pesa. Leerlo como lista vacia presentaria un fallo del servidor como "no hay
    empresas": la peor mentira que puede contar este pipeline.

    El conector NO propaga el fallo (eso perderia las paginas ya recogidas): lo anota como
    nota visible. La distincion se preserva -un cero honesto no deja nota, este si.
    """
    class _BrokenApi:
        def __call__(self, url, params):
            if url.endswith("/regiones"):
                return _REGIONS
            return {
                "codigo": "ERR_MANTENIMIENTO_BBDD",
                "error": "Aplicación en Mantenimiento.",
            }

    connector = BdnsConnector(fetcher=_BrokenApi())
    hits = connector.fetch(_spec(), Budget())   # no lanza

    assert hits == []
    assert any("ERR_MANTENIMIENTO_BBDD" in note for note in connector.notes)
    assert any("página 1" in note for note in connector.notes)


def test_a_failing_page_keeps_the_companies_already_collected():
    """El fallo de una pagina no debe tirar a la basura las anteriores (auditoria #2).

    El endpoint tiene una varianza enorme (8-44 s medidos) y un timeout en la pagina 4 no
    puede convertir un run de 300 empresas en un run de 0.
    """
    page0 = [_record(f"B572859{i:02d} EMPRESA {i} SL") for i in range(3)]

    class _FlakyApi:
        def __init__(self):
            self.data_calls = 0

        def __call__(self, url, params):
            if url.endswith("/regiones"):
                return _REGIONS
            self.data_calls += 1
            if self.data_calls == 1:
                return {"content": page0, "totalElements": 999}
            raise RuntimeError("timeout leyendo la página 2")

    connector = BdnsConnector(fetcher=_FlakyApi())
    hits = connector.fetch(_spec(max_per_source=99), Budget())   # no lanza

    # Las 3 de la pagina 1 sobreviven al fallo de la pagina 2.
    assert len(hits) == 3
    assert any("página 2" in note and "timeout" in note for note in connector.notes)


def test_a_page_error_on_a_themed_search_falls_through_to_the_retry():
    """Si la busqueda por tema falla en la pagina 0, el reintento sin tema debe alcanzarse.

    Antes del fix #2 el fallo propagaba y el reintento -el codigo que existe justo para no
    devolver un cero enganoso- quedaba inalcanzable.
    """
    class _ThemedFailsApi:
        def __call__(self, url, params):
            if url.endswith("/regiones"):
                return _REGIONS
            if "descripcion" in params:           # la busqueda temada revienta
                raise RuntimeError("ERR_MANTENIMIENTO_BBDD: consulta demasiado pesada")
            return {"content": [_record("B57285959 AGRO VIGO SL")], "totalElements": 1}

    connector = BdnsConnector(fetcher=_ThemedFailsApi(), theme="digitalizacion")
    hits = connector.fetch(_spec(), Budget())

    assert [h.name for h in hits] == ["AGRO VIGO SL"]   # el reintento sin tema salvo el run
    assert any("falló" in note for note in connector.notes)


def test_drift_note_when_the_api_shape_changes():
    """Si llegan registros pero sin el campo 'beneficiario', la API cambio: no es un cero
    legitimo, es deriva de formato. Se anota para no fiarse de un resultado vacio."""
    class _RenamedFieldApi:
        def __call__(self, url, params):
            if url.endswith("/regiones"):
                return _REGIONS
            # La API devuelve registros pero con el campo renombrado.
            return {"content": [{"empresa": "B57285959 AGRO SL", "importe": 1000}]}

    connector = BdnsConnector(fetcher=_RenamedFieldApi())
    hits = connector.fetch(_spec(), Budget())

    assert hits == []
    assert any("cambio de formato" in n for n in connector.notes)


def test_no_drift_note_on_a_legitimate_empty_result():
    """Cero registros es un cero honesto, no deriva: no debe disparar la nota."""
    api = _FakeApi([])  # una pagina vacia
    connector = BdnsConnector(fetcher=api)

    connector.fetch(_spec(), Budget())

    assert not any("cambio de formato" in n for n in connector.notes)


def test_no_drift_note_when_records_have_the_expected_field():
    """Registros de solo particulares traen 'beneficiario' (aunque se filtren): no es deriva."""
    api = _FakeApi([_record("***8386** PERSONA FISICA")])  # se filtra, pero tiene el campo
    connector = BdnsConnector(fetcher=api)

    hits = connector.fetch(_spec(), Budget())

    assert hits == []
    assert not any("cambio de formato" in n for n in connector.notes)


def test_a_theme_narrows_the_date_window():
    """Con texto libre el servidor se rinde si la ventana es de 12 meses."""
    from datetime import datetime

    def window(theme):
        api = _FakeApi([_record("B57285959 AGRO SL")])
        BdnsConnector(fetcher=api, theme=theme).fetch(_spec(), Budget())
        p = api.search_params[0]
        fmt = "%d/%m/%Y"
        return (datetime.strptime(p["fechaHasta"], fmt) - datetime.strptime(p["fechaDesde"], fmt)).days

    assert window("digitalizacion") < window(None)
