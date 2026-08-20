"""Consolidador determinista del Proceso 1 (lógica PURA, sin I/O ni LLM).

Funde los ``HunterResult`` de las lanes en una lista de ``MergedCandidate``:
normaliza nombre/dominio, deduplica de forma CONSERVADORA (ante la duda, no
fusiona: precisión > volumen, D2) y marca los casi-iguales para revisión humana.
Sin red, sin disco, sin modelo: determinista y testeable aislado. El I/O de
salida (CSV/MD) vive en ``report.py``.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse

from startup_hunter.schema import HunterResult, MergedCandidate, Source

# Dominios de plataforma: NO identifican a la empresa (todas las social comparten
# linkedin.com), así que no valen como clave de dominio para deduplicar.
_PLATFORM_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "crunchbase.com", "youtube.com",
}

# Sufijos societarios españoles a recortar del nombre para comparar.
_SUFFIX_RE = re.compile(
    r"[,\s]+(s\.?l\.?u\.?|s\.?l\.?l\.?|s\.?l\.?|s\.?a\.?u\.?|s\.?a\.?|s\.?coop\.?)\s*$",
    re.IGNORECASE,
)

# Umbral fuzzy para erratas/variantes cercanas: solo para MARCAR, nunca fusionar.
# (La contención por tokens, en _is_near_match, captura el patrón "X" ⊂ "X Soluciones",
# que SequenceMatcher.ratio() infravalora por diferencia de longitud.)
_FUZZY_FLAG_THRESHOLD = 0.88


def _strip_accents(text: str) -> str:
    """Quita tildes/diacríticos (NFKD) preservando la letra base."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def normalize_name(name: str) -> str:
    """Forma canónica para comparar nombres: sin tildes, sin sufijo societario, minúsculas."""
    if not name:
        return ""
    n = _strip_accents(name).lower()
    n = _SUFFIX_RE.sub("", n)            # recorta ", s.l." y variantes al final
    n = re.sub(r"[^\w\s]", " ", n)       # puntuación → espacio
    n = re.sub(r"\s+", " ", n).strip()   # colapsa espacios
    return n


def normalize_tax_id(value: str | None) -> str | None:
    """Forma canónica de un id fiscal (CIF): sin espacios ni guiones, mayúsculas. None si vacío."""
    if not value:
        return None
    canonical = re.sub(r"[\s.\-]", "", value).upper()
    return canonical or None


def registrable_domain(url: str | None) -> str | None:
    """Dominio registrable propio de la empresa (eTLD+1 heurístico), o None.

    Devuelve None para URLs de plataforma (LinkedIn, etc.): no identifican a la
    empresa. Heurístico de 'últimas dos etiquetas' (suficiente para .es/.com;
    limitación conocida en TLDs multinivel tipo .co.uk).
    """
    if not url:
        return None
    netloc = urlparse(url if "//" in url else "//" + url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    parts = [p for p in netloc.split(".") if p]
    if len(parts) < 2:
        return None
    dom = ".".join(parts[-2:])
    if dom in _PLATFORM_DOMAINS:
        return None
    return dom


def _is_near_match(a: str, b: str) -> bool:
    """True si dos nombres normalizados son SOSPECHOSAMENTE parecidos (solo para MARCAR).

    Captura dos patrones: (1) uno contiene al otro por tokens (p.ej. 'innova galicia'
    ⊂ 'innova galicia soluciones'), que SequenceMatcher.ratio() infravalora por
    diferencia de longitud; (2) erratas/variantes cercanas (ratio alto). Nunca fusiona,
    solo marca: el coste de un falso positivo es una revisión humana trivial (D2).
    """
    if not a or not b or a == b:
        return False
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):   # contención de tokens en cualquier dirección
        return True
    return SequenceMatcher(None, a, b).ratio() >= _FUZZY_FLAG_THRESHOLD


def _to_merged(sc) -> MergedCandidate:
    """Convierte una StartupCandidate de descubrimiento en MergedCandidate (1 fuente)."""
    return MergedCandidate(
        name=sc.name,
        description=sc.description,
        sector=sc.sector,
        location=sc.location,
        signals=[sc.signal] if sc.signal else [],
        sources=[Source(found_via=sc.found_via, source_url=sc.source_url,
                        source_date=sc.source_date)],
        website=sc.website,
        tax_id=sc.tax_id,
    )


def _absorb(into: MergedCandidate, sc) -> None:
    """Funde una candidata en una ya existente: añade fuente, señal y rellena huecos."""
    into.sources.append(Source(found_via=sc.found_via, source_url=sc.source_url,
                               source_date=sc.source_date))
    if sc.signal and sc.signal not in into.signals:
        into.signals.append(sc.signal)
    into.website = into.website or sc.website
    into.tax_id = into.tax_id or sc.tax_id


def consolidate(results: list[HunterResult]) -> list[MergedCandidate]:
    """Funde N HunterResult en candidatas deduplicadas. PURO y determinista.

    Auto-fusiona SOLO con evidencia fuerte, por prioridad de clave:
      1. ``tax_id`` (CIF) idéntico - identidad canónica oficial. La más fuerte.
      2. mismo dominio propio.
      3. nombre normalizado idéntico.
    Casi-iguales (fuzzy >= umbral) se MARCAN para revisión, no se fusionan (D2: precisión >
    volumen), salvo que dos CIF distintos demuestren que son empresas distintas. El orden de
    entrada se respeta (estable).

    Las notas de cobertura NO se tocan aquí: las indexa el pipeline por nombre de
    conector, que es la única forma de saber quién falló cuando una lane viene vacía.
    """
    merged: list[MergedCandidate] = []
    by_tax_id: dict[str, MergedCandidate] = {}
    by_domain: dict[str, MergedCandidate] = {}
    by_name: dict[str, MergedCandidate] = {}

    for res in results:
        for sc in res.candidates:
            # El CIF identifica de forma canónica; el dominio propio, casi. El source_url es
            # evidencia y puede ser un medio/directorio compartido por muchas candidatas.
            tid = normalize_tax_id(sc.tax_id)
            dom = registrable_domain(sc.website)
            nname = normalize_name(sc.name)

            hit = by_tax_id.get(tid) if tid else None
            if hit is None:
                # Claves más débiles: dominio propio y nombre. Pero un CIF que difiere las
                # VETA: dos entidades con CIF distinto no son la misma aunque compartan
                # dominio o nombre (dos homónimas, una web reutilizada). El id oficial manda.
                weak = (by_domain.get(dom) if dom else None) or by_name.get(nname)
                if weak is not None:
                    wtid = normalize_tax_id(weak.tax_id)
                    if not (tid and wtid and tid != wtid):
                        hit = weak
            if hit is not None:
                _absorb(hit, sc)
                # El CIF SÍ se registra al absorber: es identidad canónica, así que una
                # futura candidata con el mismo CIF debe fundirse aquí (justo el caso que
                # rescata un nombre distinto de la misma empresa). El dominio y el nombre,
                # en cambio, NO se registran al absorber: un dominio/nombre secundario de
                # una candidata ya fusionada no debe arrastrar a futuras candidatas ajenas
                # que lo compartan. Preserva "ante la duda, no fusiona" (D2).
                if tid:
                    by_tax_id.setdefault(tid, hit)
                by_name.setdefault(nname, hit)
                continue

            cand = _to_merged(sc)
            # Antes de aceptarla como nueva, ¿se parece mucho a una existente? → marcar.
            # Pero si ambas traen CIF y difieren, son empresas distintas con certeza: el
            # identificador oficial DESMIENTE el parecido de nombre y no se marca.
            for existing in merged:
                if existing.needs_review:
                    continue
                etid = normalize_tax_id(existing.tax_id)
                if tid and etid and tid != etid:
                    continue
                if _is_near_match(nname, normalize_name(existing.name)):
                    cand.needs_review = True
                    cand.possible_duplicate_of = existing.name
                    break

            merged.append(cand)
            if tid:
                by_tax_id[tid] = cand
            if dom:
                by_domain[dom] = cand
            by_name[nname] = cand

    return merged
