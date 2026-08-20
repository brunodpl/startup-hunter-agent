"""Contrato de conector: cómo una fuente entrega candidatas al pipeline.

Un conector es deliberadamente tonto. Recibe una ``QuerySpec``, hace sus llamadas y
devuelve ``RawHit`` con evidencia. NO decide si una empresa encaja, no puntúa y no
deduplica: de eso se encarga el consolidador (dedup) y el agente (juicio).

Añadir una fuente = escribir una clase con ``name`` y ``fetch``, y registrarla en
``connectors/__init__.py``. Nada más.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from startup_hunter.budget import Budget
from startup_hunter.schema import QuerySpec, RawHit


@runtime_checkable
class Connector(Protocol):
    """Una fuente de candidatas."""

    #: Identificador corto y estable. Acaba en ``found_via`` y en los flags de la CLI.
    name: str

    def fetch(self, spec: QuerySpec, budget: Budget) -> list[RawHit]:
        """Devuelve hits crudos para esta consulta.

        Debe pedir ``budget.consume(self.name)`` antes de cada llamada externa y dejar
        que ``BudgetExhausted`` propague: el pipeline la captura y la anota como
        cobertura recortada.

        Puede lanzar cualquier excepción; el pipeline aísla el fallo por conector.
        """
        ...


class ConnectorUnavailable(RuntimeError):
    """El conector no puede operar (falta una API key, por ejemplo).

    Es distinto de un fallo: el pipeline lo trata como "desactivado", lo anota y sigue
    con el resto de fuentes en vez de considerarlo un error del run.
    """


def clamp(hits: Iterable[RawHit], limit: int) -> list[RawHit]:
    """Recorta al tope pedido. Los conectores lo aplican antes de devolver."""
    return list(hits)[: max(0, limit)]
