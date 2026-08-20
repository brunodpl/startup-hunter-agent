"""Registro de conectores.

Añadir una fuente = escribir la clase y añadirla aquí. El nombre del conector es lo que
se pasa a ``--sources`` en la CLI y lo que acaba en ``found_via``.

Los conectores se construyen perezosamente (una factory por entrada) para que importar
este módulo nunca exija credenciales ni clientes de red.
"""

from __future__ import annotations

from typing import Callable, Mapping

from startup_hunter.connectors.base import Connector, ConnectorUnavailable
from startup_hunter.connectors.bdns import BdnsConnector
from startup_hunter.connectors.linkedin import LinkedInEnricher
from startup_hunter.connectors.tavily import TavilyConnector

#: Conectores de DESCUBRIMIENTO. El orden es el orden de ejecución por defecto.
#:
#: `bdns` va primero y siempre: es gratis, oficial y aporta la empresa como dato
#: estructurado con su señal de compra. `tavily` complementa con webs corporativas.
_REGISTRY: Mapping[str, Callable[[], Connector]] = {
    "bdns": BdnsConnector,
    "tavily": TavilyConnector,
}

#: Los que se usan si no se pasa --sources. Hoy son todos: el registro es pequeño a
#: propósito y cada fuente que entra tiene que ganarse el sitio.
DEFAULT_SOURCES = ("bdns", "tavily")


def available() -> list[str]:
    """Nombres de todos los conectores de descubrimiento registrados."""
    return list(_REGISTRY)


def build(name: str, *, grant_theme: str | None = None) -> Connector:
    """Instancia un conector por nombre.

    ``grant_theme`` solo lo entiende ``bdns``; para el resto se ignora en silencio, para
    que añadir una opción específica de una fuente no obligue a tocar la CLI ni el resto
    de conectores.
    """
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Conector desconocido: {name!r}. Disponibles: {', '.join(available())}."
        ) from None
    if name == "bdns":
        return factory(theme=grant_theme)
    return factory()


def build_many(names: list[str], *, grant_theme: str | None = None) -> list[Connector]:
    return [build(name, grant_theme=grant_theme) for name in names]


__all__ = [
    "DEFAULT_SOURCES",
    "Connector",
    "ConnectorUnavailable",
    "LinkedInEnricher",
    "available",
    "build",
    "build_many",
]
