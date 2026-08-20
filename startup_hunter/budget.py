"""Tope de llamadas externas por run - el backstop de coste del pipeline.

Por qué existe: en junio de 2026 un solo día costó ~$18,78 porque el agente sobre-buscaba
(18-22 búsquedas por run, cada una reenviando todo el contexto). Ver
knowledge/lecciones-scraping.md §8.

El pipeline determinista elimina la causa raíz -ya no hay un modelo decidiendo cuántas
llamadas hacer- pero el tope se conserva: protege igual frente a una consulta desmedida o
una fuente que pagina sin fin.

A diferencia del backstop anterior, que contaba mensajes del historial del agente y por
tanto era aproximado, este contador es EXACTO: el pipeline pasa el mismo ``Budget`` a
todos los conectores y cada uno pide permiso antes de llamar.
"""

from __future__ import annotations

import os
import threading

# Generoso a propósito: solo muerde en runaways, no en hunts disciplinados.
_DEFAULT_MAX_CALLS = 12
_MAX_CALLS_ENV = "STARTUP_HUNTER_MAX_CALLS"


def default_max_calls() -> int:
    """Tope de llamadas por conector y por run, configurable por entorno."""
    raw = os.environ.get(_MAX_CALLS_ENV)
    if raw is None:
        return _DEFAULT_MAX_CALLS
    try:
        return max(1, int(raw))
    except ValueError:
        # Un valor mal escrito no debe romper un run: se cae al valor por defecto.
        return _DEFAULT_MAX_CALLS


class BudgetExhausted(RuntimeError):
    """Se alcanzó el tope de llamadas de un conector.

    No es un error de verdad: es la señal de parar. El pipeline la captura, anota la
    cobertura recortada y sigue con lo que ya tiene.
    """


class Budget:
    """Contador de llamadas externas, con un cupo independiente por conector.

    Thread-safe porque los conectores pueden ejecutarse en paralelo.
    """

    def __init__(self, max_calls_per_source: int | None = None) -> None:
        self._max = max_calls_per_source or default_max_calls()
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def max_calls_per_source(self) -> int:
        return self._max

    def used(self, source: str) -> int:
        with self._lock:
            return self._used.get(source, 0)

    def remaining(self, source: str) -> int:
        return max(0, self._max - self.used(source))

    def consume(self, source: str) -> None:
        """Registra una llamada a punto de hacerse; lanza si el cupo está agotado.

        Se llama ANTES de la petición, no después, para que un cupo agotado no llegue a
        gastar crédito.
        """
        with self._lock:
            current = self._used.get(source, 0)
            if current >= self._max:
                raise BudgetExhausted(
                    f"{source}: alcanzado el tope de {self._max} llamadas por run "
                    f"(configurable con {_MAX_CALLS_ENV}). Cobertura recortada."
                )
            self._used[source] = current + 1

    def snapshot(self) -> dict[str, int]:
        """Llamadas gastadas por conector, para las estadísticas del informe."""
        with self._lock:
            return dict(self._used)
