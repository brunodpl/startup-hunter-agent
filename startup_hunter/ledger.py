"""Registro de candidatas reportadas - la memoria entre runs, en dos fases.

Por qué existe: sin él, un hunt diario repite las mismas empresas cada día. Los
beneficiarios de una subvención no cambian de un día para otro, así que el segundo mensaje
ya es ruido y el tercero enseña a ignorar el canal. Revisa A5 (knowledge/decisiones.md).

**Por qué DOS fases (`reported` → `confirmed`).**

La entrega por un canal externo es *at-least-once*: un mensaje puede perderse sin aviso
(móvil apagado, gateway caído) - el llamado *silent delivery failure*. Si marcáramos una
candidata como vista en el mismo momento de "enviar", un mensaje que no llegó la enterraría
para siempre: desaparecería de todos los hunts futuros sin que nadie la viera. Eso es
justo la pérdida silenciosa que el resto del pipeline evita con cuidado.

La defensa que recomienda la práctica es idempotencia + un estado intermedio reversible:

1. ``mark_reported`` - "se ha enviado". Estado ``reported``, reversible.
2. ``confirm`` - "el destinatario lo recibió". Estado ``confirmed``.
3. ``stale_unconfirmed`` - lo que se envió pero nunca se confirmó pasado un plazo: son los
   sospechosos de haberse perdido, y el runbook los reenvía. Peor caso: se recibe una
   empresa dos veces (molesto pero recuperable); nunca cero (un cliente perdido).

Ambas fases cuentan como "ya reportada" para ``is_new``: no se re-lanza en cada run, solo
tras el plazo de ``stale_unconfirmed``.

La clave es ``candidate_identity.candidate_key``: CIF si se conoce, si no dominio, si no
nombre normalizado + ubicación. Estable entre runs sin depender del texto de un modelo.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from startup_hunter.candidate_identity import candidate_key
from startup_hunter.schema import MergedCandidate

#: Fuera de ``output/``: ese directorio es de artefactos por run y se puede borrar entero
#: sin perder nada. El registro es estado que tiene que sobrevivir a eso.
_DEFAULT_PATH = Path(".startup-hunter") / "seen.json"
_PATH_ENV = "STARTUP_HUNTER_LEDGER"

STATUS_REPORTED = "reported"
STATUS_CONFIRMED = "confirmed"


def default_ledger_path() -> Path:
    """Ruta del registro, configurable por entorno."""
    raw = os.environ.get(_PATH_ENV)
    return Path(raw) if raw else _DEFAULT_PATH


def _today() -> str:
    return date.today().isoformat()


def _days_between(earlier_iso: str, later_iso: str) -> int:
    """Días entre dos fechas ISO. 0 si alguna no parsea (no marcamos como stale por error)."""
    try:
        a = datetime.fromisoformat(earlier_iso).date()
        b = datetime.fromisoformat(later_iso).date()
    except (ValueError, TypeError):
        return 0
    return (b - a).days


class Ledger:
    """Qué candidatas se han reportado, en qué estado y desde cuándo."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_ledger_path()
        self._entries: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        """Lee el registro. Un fichero ausente o corrupto NO tumba el run.

        Acepta el formato legacy ``{clave: "2026-07-20"}`` (de antes de las dos fases) y lo
        trata como ``confirmed``: lo ya registrado se daba por entregado. Ante un registro
        ilegible se empieza de cero: repetir unas candidatas es recuperable;
        abortar el hunt por no poder leer un fichero de estado, no.
        """
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(payload, dict):
            return {}

        entries: dict[str, dict] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, str):  # formato legacy: fecha suelta -> confirmada
                entries[key] = {
                    "status": STATUS_CONFIRMED,
                    "first_seen": value,
                    "reported_at": value,
                    "confirmed_at": value,
                }
            elif isinstance(value, dict) and value.get("status") in (
                STATUS_REPORTED,
                STATUS_CONFIRMED,
            ):
                entries[key] = value
        return entries

    def __len__(self) -> int:
        return len(self._entries)

    def is_new(self, candidate: MergedCandidate) -> bool:
        """True si esta candidata no se ha reportado nunca (ni pendiente ni confirmada)."""
        return candidate_key(candidate) not in self._entries

    def status(self, candidate: MergedCandidate) -> str | None:
        """Estado de una candidata: ``reported``, ``confirmed`` o None si no está."""
        entry = self._entries.get(candidate_key(candidate))
        return entry["status"] if entry else None

    def _is_stale_reported(self, key: str, days: int, today: str) -> bool:
        entry = self._entries.get(key)
        return bool(
            entry
            and entry["status"] == STATUS_REPORTED
            and _days_between(entry["reported_at"], today) >= days
        )

    def annotate(
        self, candidates: list[MergedCandidate], resend_after_days: int | None = None
    ) -> int:
        """Marca ``is_new`` en cada candidata y devuelve cuántas hay que reportar. NO escribe.

        "Hay que reportar" = nunca vista, **o** enviada pero sin confirmar hace
        ``>= resend_after_days`` días (recuperación de una entrega que pudo perderse). Con
        ``resend_after_days=None`` solo cuentan las nunca vistas (comportamiento estricto).
        """
        today = _today()
        new = 0
        for candidate in candidates:
            key = candidate_key(candidate)
            fresh = key not in self._entries
            stale = resend_after_days is not None and self._is_stale_reported(
                key, resend_after_days, today
            )
            candidate.is_new = fresh or stale
            if candidate.is_new:
                new += 1
        return new

    def mark_reported(
        self, candidates: list[MergedCandidate], today: str | None = None
    ) -> int:
        """Fase 1: registra las candidatas como enviadas, pendientes de confirmar.

        Solo añade claves nuevas; nunca degrada una ya ``confirmed`` a ``reported``.
        Idempotente: reejecutarla tras un fallo es seguro. Devuelve cuántas se añadieron.
        """
        stamp = today or _today()
        added = 0
        for candidate in candidates:
            key = candidate_key(candidate)
            if key not in self._entries:
                self._entries[key] = {
                    "status": STATUS_REPORTED,
                    "first_seen": stamp,
                    "reported_at": stamp,
                    "confirmed_at": None,
                }
                added += 1
        self._write()
        return added

    def confirm(
        self, candidates: list[MergedCandidate] | None = None, today: str | None = None
    ) -> int:
        """Fase 2: promueve a ``confirmed`` las pendientes.

        Con ``candidates``, solo esas; sin argumento, TODAS las que estén ``reported`` (útil
        cuando llega un acuse: prueba de que se recibió el último lote). Devuelve cuántas se
        promovieron. Idempotente.
        """
        stamp = today or _today()
        if candidates is None:
            keys = [k for k, e in self._entries.items() if e["status"] == STATUS_REPORTED]
        else:
            keys = [candidate_key(c) for c in candidates]

        promoted = 0
        for key in keys:
            entry = self._entries.get(key)
            if entry and entry["status"] == STATUS_REPORTED:
                entry["status"] = STATUS_CONFIRMED
                entry["confirmed_at"] = stamp
                promoted += 1
        if promoted:
            self._write()
        return promoted

    def stale_unconfirmed(self, days: int, today: str | None = None) -> list[str]:
        """Claves que siguen ``reported`` y se enviaron hace ``>= days`` días.

        Son los sospechosos de un fallo de entrega silencioso: se enviaron pero nunca
        dio señal de haberlos recibido. El runbook los reenvía (at-least-once)."""
        now = today or _today()
        return [
            key
            for key, entry in self._entries.items()
            if entry["status"] == STATUS_REPORTED
            and _days_between(entry["reported_at"], now) >= days
        ]

    def mark(self, candidates: list[MergedCandidate], today: str | None = None) -> int:
        """Atajo del camino confiado: registra y confirma en un paso.

        Para cuando la entrega no está en duda (uso manual, o un canal con acuse fiable).
        El camino robusto por defecto es ``mark_reported`` + ``confirm``.
        """
        added = self.mark_reported(candidates, today)
        self.confirm(candidates, today)
        return added

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
