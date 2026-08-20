"""Conector BDNS - Base de Datos Nacional de Subvenciones.

Fuente: ``https://www.infosubvenciones.es/bdnstrans/api`` - API REST **oficial, pública y
gratuita** del Ministerio de Hacienda (SNPSAP). Sin token, sin límite publicado, ~28 M de
concesiones. Verificada en vivo el 20-jul-2026.

Por qué es la fuente principal de descubrimiento: una subvención concedida es
**presupuesto confirmado, fechado y nominal**. La empresa viene como dato estructurado,
no hay que interpretar texto libre para saber de quién habla. Es la buy-signal más limpia
del catálogo (knowledge/fuentes.md §1).

Endpoint y parámetros verificados
---------------------------------
``GET /concesiones/busqueda`` con:

- ``regiones``   - id del árbol NUTS de ``GET /regiones``. **Galicia = 3** (``ES11``);
                   sus provincias son A Coruña 4, Lugo 5, Ourense 6, Pontevedra 7.
- ``fechaDesde`` / ``fechaHasta`` - formato ``DD/MM/AAAA`` (ojo: distinto del ISO que
                   devuelve luego en ``fechaConcesion``).
- ``descripcion`` - texto libre contra la descripción de la CONVOCATORIA. Filtra de
                   verdad (114.463 → 767 con «innovacion», 67 con «digitalizacion»).
- ``page`` / ``pageSize`` - paginación estilo Spring; la respuesta trae ``totalElements``.

Qué acota ``descripcion`` y qué NO
----------------------------------
Busca en el nombre del **programa de ayudas**, no en el sector de la empresa. «agritech»
o «I+D» devuelven cero porque las órdenes de convocatoria no usan esas palabras; hay que
pensar en términos administrativos («innovacion», «digitalizacion», «agraria»).

Por eso NO se mapea automáticamente desde ``--sector``: un término que no existe en las
convocatorias vaciaría el resultado en silencio, que es el peor fallo posible. Se pasa
explícito con ``--grant-theme`` y, si deja la búsqueda a cero, el conector **reintenta sin
él** y lo anota, en vez de devolver una lista vacía engañosa.

El encaje real con el sector pedido lo juzga el agente leyendo la salida.

Otros nombres que parecen razonables (``ccaa``, ``region``, ``nivel1``, ``vpd``) se
**ignoran silenciosamente**: devuelven el total sin filtrar. Si añades un filtro nuevo,
compruébalo mirando que ``totalElements`` cambie, no que la petición dé 200.

Solo personas JURÍDICAS
-----------------------
La BDNS publica también ayudas a personas físicas (autónomos, agricultores, estudiantes),
con el NIF enmascarado: ``***8386** FERNANDO EMILIO FREIRE VAZQUEZ``. **Se descartan.** No
son clientes potenciales y son datos personales de particulares, que no pintan nada en la
cartera. Solo pasan los beneficiarios con CIF de entidad (``B57285959 EMPRESA, SL``).

Aviso legal
-----------
La reutilización está sujeta al aviso legal del SNPSAP
(``https://www.infosubvenciones.es/bdnstrans/GE/es/avisolegal``) y la Administración puede
restringir el API ante abuso. De ahí el ``pageSize`` moderado y el tope de páginas.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import httpx

from startup_hunter.budget import Budget, BudgetExhausted
from startup_hunter.connectors.base import clamp
from startup_hunter.schema import QuerySpec, RawHit

_BASE_URL = "https://www.infosubvenciones.es/bdnstrans/api/concesiones/busqueda"
_REGIONS_URL = "https://www.infosubvenciones.es/bdnstrans/api/regiones"
#: Página pública de la CONVOCATORIA concreta que recibió la empresa. Verificada en vivo el
#: 20-jul-2026: ``api/convocatorias?numConv=<n>`` devuelve el detalle, así que la ruta de
#: frontend ``/GE/es/convocatoria/<n>`` resuelve a contenido real. Es la evidencia más
#: específica que el SNPSAP expone de forma enlazable: el portal NO publica un deep-link por
#: concesión individual (``api/concesiones/<id>`` da 404), así que la prueba de que ESTA
#: empresa recibió ESTA ayuda es la convocatoria + el CIF/importe/fecha que ya llevamos.
_CONVOCATORIA_URL = "https://www.infosubvenciones.es/bdnstrans/GE/es/convocatoria"
#: Respaldo cuando un registro no trae ``numeroConvocatoria``: el buscador público genérico.
_PUBLIC_URL = "https://www.infosubvenciones.es/bdnstrans/GE/es/concesiones"
#: Generoso a propósito. Una búsqueda sin ``descripcion`` responde en <1 s, pero la
#: búsqueda por texto recorre 28 M de registros y se ha medido entre 8 s y 44 s para la
#: MISMA consulta (varianza del servidor, no del tamaño de página). Con 60 s daba timeout.
_TIMEOUT = 150.0
_PAGE_SIZE = 100
_MAX_PAGES = 5
#: Meses hacia atrás cuando se acota por tema. Ver `_date_range`.
_THEMED_MONTHS_BACK = 3

_USER_AGENT = "startup-hunter-agent/1.0 (+https://github.com/brunodpl/startup-hunter-agent)"

#: Ids del árbol NUTS de /regiones. Se cachean aquí porque son estables y así el caso
#: normal no gasta una llamada extra. `_resolve_region` cae al árbol vivo si no acierta.
_REGION_IDS = {
    "galicia": 3,
    "a coruña": 4, "a coruna": 4, "coruña": 4, "coruna": 4,
    "lugo": 5,
    "ourense": 6, "orense": 6,
    "pontevedra": 7,
    "asturias": 8, "principado de asturias": 8,
}

#: CIF de persona jurídica: letra de entidad + 8 caracteres. Excluye NIF de particular.
_CIF_RE = re.compile(r"^([ABCDEFGHJNPQRSUVW])(\d{7}[0-9A-J])\b", re.IGNORECASE)
#: NIF enmascarado de persona física, tal como lo publica la BDNS.
_MASKED_PERSON_RE = re.compile(r"^\*{2,}")

#: Formas societarias, como red de seguridad si el CIF viniera ausente o mal formado.
_LEGAL_FORM_RE = re.compile(
    r"\b(S\.?L\.?U?\.?|S\.?A\.?U?\.?|S\.?C\.?|S\.?COOP\.?|SOCIEDAD|COOPERATIVA|"
    r"ASOCIACION|ASOCIACIÓN|FUNDACION|FUNDACIÓN|CLUSTER|CLÚSTER)\b",
    re.IGNORECASE,
)


class ApiError(RuntimeError):
    """El API respondió 200 pero con un cuerpo de error en vez de datos."""


def _default_fetcher(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.get(
        url,
        params=params,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _resolve_region(location: str | None, fetcher: Callable[..., Any]) -> int | None:
    """Traduce un nombre de zona al id de región de la BDNS.

    Primero por la tabla local; si no está, se baja el árbol de ``/regiones`` y se busca
    por descripción (``"ES11 - GALICIA"``). Devuelve ``None`` si no hay forma de acotar,
    en cuyo caso la búsqueda sale sin filtro geográfico en vez de fallar.
    """
    if not location:
        return None
    key = location.strip().casefold()
    if key in _REGION_IDS:
        return _REGION_IDS[key]

    try:
        tree = fetcher(_REGIONS_URL, {})
    except Exception:  # noqa: BLE001 - sin catálogo, seguimos sin filtro geográfico
        return None

    match: list[int] = []

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            desc = str(node.get("descripcion", "")).casefold()
            # El formato es "ES11 - GALICIA": comparamos contra la parte del nombre.
            if key and key in desc and isinstance(node.get("id"), int):
                match.append(node["id"])
            walk(node.get("children"))

    walk(tree if isinstance(tree, list) else [tree])
    return match[0] if match else None


def is_company(beneficiary: str) -> bool:
    """True si el beneficiario es una persona jurídica.

    Descarta a los particulares, que la BDNS publica con el NIF enmascarado. Es a la vez
    calidad de dato (un autónomo no es cliente de un servicio de growth) y minimización:
    no queremos datos personales de particulares en la cartera.
    """
    value = beneficiary.strip()
    if not value or _MASKED_PERSON_RE.match(value):
        return False
    return bool(_CIF_RE.match(value)) or bool(_LEGAL_FORM_RE.search(value))


def split_beneficiary(beneficiary: str) -> tuple[str, str | None]:
    """Separa ``"B57285959 EMPRESA, SL"`` en ``("EMPRESA, SL", "B57285959")``."""
    value = beneficiary.strip()
    match = _CIF_RE.match(value)
    if not match:
        return value, None
    cif = match.group(0)
    return value[len(cif):].strip(" ,-") or value, cif


class BdnsConnector:
    """Concesiones de subvenciones. El fetcher se inyecta para testear sin red."""

    name = "bdns"

    def __init__(
        self,
        fetcher: Callable[[str, dict[str, Any]], Any] | None = None,
        months_back: int = 12,
        theme: str | None = None,
    ) -> None:
        self._fetch = fetcher or _default_fetcher
        self._months_back = months_back
        self._theme = (theme or "").strip() or None
        #: Se rellena si el tema deja la búsqueda a cero y hubo que reintentar sin él.
        self.notes: list[str] = []

    def _date_range(self, theme: str | None) -> tuple[str, str]:
        """Ventana reciente: una subvención de hace años ya no es señal de presupuesto.

        Con ``theme`` la ventana se estrecha: la búsqueda por texto recorre los 28 M de
        registros y con 12 meses el servidor se rinde a los 60 s con
        ``ERR_MANTENIMIENTO_BBDD``. Medido: 12 meses → error; 2,5 meses → 2,2 s y 64
        resultados. Se prefiere una ventana corta con datos a una larga sin ellos.
        """
        from datetime import date, timedelta

        months = _THEMED_MONTHS_BACK if theme else self._months_back
        today = date.today()
        since = today - timedelta(days=30 * months)
        return since.strftime("%d/%m/%Y"), today.strftime("%d/%m/%Y")

    def fetch(self, spec: QuerySpec, budget: Budget) -> list[RawHit]:
        region_id = _resolve_region(spec.location, self._fetch)

        hits = self._search(spec, budget, region_id, self._theme)
        if not hits and self._theme:
            # El tema puede dejar la búsqueda a cero por dos vías: porque no casa con
            # ninguna convocatoria, o porque la consulta por texto pesa tanto que el
            # servidor se rinde (ver `_records`). En ambos casos reintentar sin tema es la
            # recuperación correcta -la búsqueda sin texto es mucho más ligera- y mejor
            # decirlo que devolver un cero engañoso. Si hubo un fallo, `_search` ya dejó su
            # propia nota explicando el porqué.
            self.notes.append(
                f"La búsqueda con el tema «{self._theme}» no devolvió resultados; "
                "se repitió sin acotar por tema."
            )
            hits = self._search(spec, budget, region_id, None)
        return hits

    def _search(
        self,
        spec: QuerySpec,
        budget: Budget,
        region_id: int | None,
        theme: str | None,
    ) -> list[RawHit]:
        since, until = self._date_range(theme)
        hits: list[RawHit] = []
        seen: set[str] = set()
        # Log de deriva: distinguir «cero empresas» legítimo de «la API cambió de formato».
        # Si llegan registros pero NINGUNO trae el campo `beneficiario`, la forma de la
        # respuesta ha cambiado y estaríamos devolviendo cero en silencio por un fallo
        # nuestro, no por ausencia de datos. Se detecta y se anota.
        saw_records = False
        saw_expected_field = False

        for page in range(_MAX_PAGES):
            if len(hits) >= spec.max_per_source:
                break
            try:
                budget.consume(self.name)
            except BudgetExhausted:
                break

            params: dict[str, Any] = {
                "page": page,
                "pageSize": _PAGE_SIZE,
                "fechaDesde": since,
                "fechaHasta": until,
            }
            if region_id is not None:
                params["regiones"] = region_id
            if theme:
                params["descripcion"] = theme

            try:
                payload = self._fetch(_BASE_URL, params)
                records = self._records(payload)
            except Exception as exc:  # noqa: BLE001 - el fallo de una página no debe perder las anteriores
                # El endpoint tiene una varianza enorme y devuelve ERR_MANTENIMIENTO bajo
                # carga (ver `_records` y `_TIMEOUT`). Si esto tumbara el conector, un
                # timeout en la página 4 tiraría a la basura las empresas ya recogidas en
                # las tres anteriores. Se corta la paginación conservando lo obtenido y se
                # deja constancia de hasta dónde se llegó, en vez de devolver o propagar
                # un fallo total.
                self.notes.append(
                    f"BDNS falló al leer la página {page + 1} ({exc}); "
                    "se devuelve lo recogido hasta esa página."
                )
                break

            if not records:
                break

            saw_records = True
            for record in records:
                if "beneficiario" in record:
                    saw_expected_field = True
                hit = self._to_hit(record, spec)
                if hit is None:
                    continue
                key = hit.name.casefold()
                if key in seen:
                    # La misma empresa suele cobrar varias ayudas; nos quedamos con una.
                    continue
                seen.add(key)
                hits.append(hit)

        if saw_records and not saw_expected_field:
            self.notes.append(
                "Posible cambio de formato de la API de BDNS: se recibieron registros "
                "pero ninguno trae el campo «beneficiario». Revisa el conector antes de "
                "fiarte de un resultado vacío."
            )

        return clamp(hits, spec.max_per_source)

    @staticmethod
    def _records(payload: Any) -> list[dict[str, Any]]:
        """Extrae los registros, distinguiendo «no hay» de «la consulta falló».

        El API devuelve **HTTP 200 con un cuerpo de error** cuando la consulta es
        demasiado pesada: ``{"codigo": "ERR_MANTENIMIENTO_BBDD", "error": "Aplicación en
        Mantenimiento..."}`` tras ~60 s. Si eso se leyera como lista vacía, un fallo del
        servidor se presentaría como «no hay empresas», que es la peor mentira que puede
        contar este pipeline. Se convierte en excepción para que el pipeline lo anote
        como ERROR y el agente lo vea.
        """
        if isinstance(payload, dict):
            if payload.get("error") or payload.get("codigo"):
                raise ApiError(
                    f"{payload.get('codigo', 'ERROR')}: {payload.get('error', '')} "
                    "(la consulta fue demasiado pesada para el servidor; prueba una "
                    "ventana de fechas más corta)"
                )
            content = payload.get("content")
            if isinstance(content, list):
                return [r for r in content if isinstance(r, dict)]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []

    def _to_hit(self, record: dict[str, Any], spec: QuerySpec) -> RawHit | None:
        beneficiary = str(record.get("beneficiario") or "").strip()
        if not beneficiary or not is_company(beneficiary):
            return None

        name, cif = split_beneficiary(beneficiary)
        amount = record.get("importe")
        granted_on = str(record.get("fechaConcesion") or "").strip() or None
        call = str(record.get("convocatoria") or "").strip()
        num_conv = str(record.get("numeroConvocatoria") or "").strip()

        signal = "Subvención pública concedida"
        if amount:
            signal += f" por {amount} €"
        if granted_on:
            signal += f" el {granted_on}"
        if call:
            signal += f" ({call[:120]})"

        # Evidencia: la convocatoria concreta si se conoce (verificable en el portal), y el
        # buscador genérico solo como respaldo. Es lo que abre un humano.
        source_url = (
            f"{_CONVOCATORIA_URL}/{num_conv}" if num_conv.isdigit() else _PUBLIC_URL
        )

        return RawHit(
            name=name,
            source_url=source_url,
            source=self.name,
            description="Empresa beneficiaria de una subvención pública (BDNS).",
            signal=signal,
            sector=spec.sector,
            location=record.get("nivel2") or spec.location,
            # El CIF es la identidad canónica: sube al esquema para deduplicar y para el
            # registro de ya-vistas, en vez de quedarse enterrado en `raw`.
            tax_id=cif,
            source_date=granted_on if granted_on and granted_on[:4].isdigit() else None,
            raw={
                "cif": cif,
                "importe": amount,
                "fechaConcesion": granted_on,
                "convocatoria": call,
                "numeroConvocatoria": num_conv or None,
                "organo": record.get("nivel3"),
                "urlBR": record.get("urlBR"),
                "codConcesion": record.get("codConcesion"),
            },
        )
