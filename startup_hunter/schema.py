"""Contratos de datos del pipeline: qué guardamos de cada empresa candidata.

Esquema ÚNICO compartido por los conectores, el consolidador y la salida. Define solo
QUÉ guardamos; no contiene lógica de búsqueda ni de juicio comercial.

El flujo de contratos es:

    QuerySpec  ->  RawHit (por conector)  ->  StartupCandidate  ->  MergedCandidate
                                                                          |
                                                                    HuntResult

Principios:
- Hechos, no juicio: el pipeline recoge y normaliza; quien decide si una empresa encaja
  es el agente que lee la salida (ver knowledge/decisiones.md, A2).
- Evidencia obligatoria: sin ``source_url`` no hay candidata (anti-alucinación).
- Tolerante con empresas pequeñas o poco indexadas: lo no esencial es opcional (``None``).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Nombre del conector que encontró la candidata (sirve para filtrar y deduplicar).
# Es un str libre, no un Literal: el registro de conectores es extensible y el esquema
# no debería tener que cambiar cada vez que se añade una fuente.
FoundVia = str


def _validate_http_url(value: str) -> str:
    """Valida evidencia publica como URL absoluta http(s), manteniendo el tipo str."""
    value = value.strip()
    if any(ch.isspace() for ch in value):
        raise ValueError("source_url must not contain whitespace")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source_url must be an absolute http(s) URL")
    return value


class QuerySpec(BaseModel):
    """Lo que se pide a un hunt. Es la entrada de todos los conectores.

    ``query`` es texto libre tal y como lo escribiría una persona; ``sector`` y
    ``location`` son opcionales y sirven para que los conectores estructurados
    (que filtran por campos, no por texto) puedan acotar.
    """

    query: str = Field(description="Free-text request, e.g. 'agritech viñedos'.")
    location: Optional[str] = Field(
        default=None, description="Region or country to scope the hunt, e.g. 'Galicia'."
    )
    sector: Optional[str] = Field(default=None, description="Sector hint, if any.")
    max_per_source: int = Field(
        default=20, ge=1, description="Cap of hits kept per connector."
    )

    def describe(self) -> str:
        """Etiqueta legible del hunt, para notas e informes."""
        parts = [self.query]
        if self.location:
            parts.append(f"en {self.location}")
        return " ".join(parts)


class RawHit(BaseModel):
    """Un resultado crudo de un conector, antes de normalizar.

    Es el mínimo común denominador entre una búsqueda web y una tabla de subvenciones:
    un nombre de empresa y una URL que lo demuestra. Todo lo demás es opcional porque
    cada fuente expone campos distintos; ``raw`` conserva el payload original por si
    hace falta depurar o extraer más adelante.
    """

    name: str = Field(description="Company name as the source reports it.")
    source_url: str = Field(description="URL of the evidence. Mandatory.")
    source: str = Field(description="Connector that produced this hit.")
    description: str = Field(default="", description="What it does, if the source says.")
    signal: str = Field(default="", description="Why it surfaced (grant, round, press…).")
    sector: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None)
    website: Optional[str] = Field(default=None)
    source_date: Optional[str] = Field(default=None, description="ISO date of the signal.")
    tax_id: Optional[str] = Field(
        default=None,
        description="Official company registration id (in Spain, the CIF), when the source "
        "reports it. The strongest identity key there is: it disambiguates name variants.",
    )
    raw: dict[str, Any] = Field(default_factory=dict, description="Original payload.")

    @field_validator("source_url")
    @classmethod
    def _source_url_must_be_http(cls, value: str) -> str:
        return _validate_http_url(value)


class StartupCandidate(BaseModel):
    """A potential client company discovered for a given sector + location."""

    # --- Identidad (obligatorio) ---
    name: str = Field(description="The company's name.")
    description: str = Field(description="What it does, in a single sentence.")

    # --- Encaje con la búsqueda (obligatorio) ---
    sector: str = Field(description="The sector it operates in (e.g. 'foodtech').")
    location: str = Field(description="Region or city (e.g. 'Vigo, Galicia').")

    # --- Señal y evidencia (obligatorio) ---
    signal: str = Field(
        description=(
            "Why it surfaced: funding round, launch, demo day, award, "
            "hiring, press mention, etc."
        )
    )
    source_url: str = Field(
        description="URL of the evidence. Mandatory: no source, not included."
    )
    found_via: FoundVia = Field(
        description="Connector that discovered it, e.g. 'tavily', 'bdns'."
    )

    # --- Contexto opcional (puede faltar en empresas pequeñas o muy tempranas) ---
    website: Optional[str] = Field(
        default=None,
        description="Own website or domain, if known.",
    )
    tax_id: Optional[str] = Field(
        default=None,
        description="Official registration id (CIF in Spain), if the source reports it.",
    )
    source_date: Optional[str] = Field(
        default=None,
        description="Date of the signal in ISO format (YYYY-MM-DD), if known.",
    )

    @field_validator("source_url")
    @classmethod
    def _source_url_must_be_http(cls, value: str) -> str:
        return _validate_http_url(value)


class HunterResult(BaseModel):
    """Salida normalizada de UN conector: candidatas validadas + nota de cobertura.

    Envuelve la lista de ``StartupCandidate`` para que la salida de cada conector tenga
    una forma conocida y validada, lista para que el consolidador la funda. La nota
    registra qué funcionó y qué no -incluido "este conector falló"-, de modo que un
    fallo aislado quede visible en el informe en vez de desaparecer.
    """

    candidates: list[StartupCandidate] = Field(
        default_factory=list,
        description="Companies found that match the requirement. May be empty.",
    )
    notes: str = Field(
        description="Which search angles worked and which didn't, for future passes.",
    )


# ---------------------------------------------------------------------------
# Contratos de salida del consolidador
# (distintos de StartupCandidate/HunterResult, que son contratos de lane)
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """One piece of evidence that surfaced a candidate (one per connector hit)."""

    found_via: FoundVia = Field(description="Connector that produced this evidence.")
    source_url: str = Field(description="URL of the evidence.")
    source_date: Optional[str] = Field(
        default=None, description="Date of the signal (YYYY-MM-DD), if known."
    )

    @field_validator("source_url")
    @classmethod
    def _source_url_must_be_http(cls, value: str) -> str:
        return _validate_http_url(value)


class DecisionMaker(BaseModel):
    """Decisor público de una candidata.

    Lista blanca deliberada de cuatro campos. Aunque la fuente devuelva email o teléfono,
    NO existe campo donde guardarlos: es la defensa de GDPR del proyecto y hay un test que
    la protege. Ver knowledge/lecciones-scraping.md §6.
    """

    name: str = Field(
        description="Decision-maker name, or 'no_confirmado' when public evidence is missing."
    )
    role: str = Field(
        description="Confirmed role, or 'no_confirmado' when public evidence is missing."
    )
    linkedin_url: str = Field(
        description="Public LinkedIn URL, or 'no_confirmado' when it cannot be confirmed."
    )
    source_url: str = Field(
        description="Public source proving the person is linked to the company, or 'no_confirmado'."
    )


class Enrichment(BaseModel):
    """Datos públicos añadidos a una candidata DESPUÉS de consolidar.

    Solo se rellena para las candidatas que se enriquecen (por defecto las primeras N),
    para minimizar el scraping de datos personales. Todo es opcional: una fuente que no
    expone un campo deja ``None``, nunca un valor inventado.
    """

    linkedin_url: Optional[str] = Field(default=None)
    website: Optional[str] = Field(default=None)
    funding: Optional[str] = Field(default=None, description="Round / total raised, as reported.")
    employees: Optional[str] = Field(default=None)
    founded: Optional[str] = Field(default=None)
    headquarters: Optional[str] = Field(default=None)
    decision_makers: list[DecisionMaker] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list, description="What could not be confirmed, or which step failed."
    )


class MergedCandidate(BaseModel):
    """A deduplicated candidate after merging the connectors' raw discoveries.

    Distinct from ``StartupCandidate``: a single company may have been found by
    several connectors, so it carries a LIST of sources (and signals) instead of one.
    """

    name: str = Field(description="The company's name (canonical form kept).")
    description: str = Field(description="What it does, in a single sentence.")
    sector: str = Field(description="The sector it operates in.")
    location: str = Field(description="Region or city.")
    signals: list[str] = Field(
        default_factory=list, description="All buy-signals collected across connectors."
    )
    sources: list[Source] = Field(
        default_factory=list, description="Evidence, one per connector hit. Never empty."
    )
    website: Optional[str] = Field(default=None, description="Own website/domain, if known.")
    tax_id: Optional[str] = Field(
        default=None,
        description="Official registration id (CIF in Spain). Canonical identity when present.",
    )
    # --- Marcas de deduplicación (para revisión humana) ---
    needs_review: bool = Field(
        default=False,
        description="True when a fuzzy near-match was flagged but NOT auto-merged.",
    )
    possible_duplicate_of: Optional[str] = Field(
        default=None,
        description="Name of the candidate this one might duplicate (human to confirm).",
    )
    # --- Memoria entre runs ---
    is_new: Optional[bool] = Field(
        default=None,
        description=(
            "True if never reported before, False if already reported. "
            "None means the ledger was not consulted - do NOT read it as 'already known'."
        ),
    )
    # Se rellena en el paso de enriquecimiento, despues de consolidar. El consolidador
    # nunca lo toca, por eso tiene default.
    enrichment: Optional[Enrichment] = Field(default=None)


# ---------------------------------------------------------------------------
# Contrato de salida del pipeline (lo que consume el agente)
# ---------------------------------------------------------------------------


class HuntResult(BaseModel):
    """Resultado completo de un hunt. Es lo que se serializa a ``candidates.json``.

    Este es el contrato canónico entre el pipeline y el agente. Deliberadamente NO
    contiene ningún juicio comercial (ni scoring, ni "encaja/no encaja"): solo hechos
    con evidencia. Quien decide es el agente.
    """

    query: str = Field(description="The request this hunt answered.")
    generated_at: str = Field(description="ISO timestamp of the run.")
    candidates: list[MergedCandidate] = Field(default_factory=list)
    source_notes: dict[str, str] = Field(
        default_factory=dict,
        description="Per-connector coverage note, including failures.",
    )
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="Counters: raw hits per connector, merged total, flagged duplicates.",
    )
