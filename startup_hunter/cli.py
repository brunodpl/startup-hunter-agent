"""CLI del Startup Hunter - la superficie que usa el agente.

    python -m startup_hunter hunt "agritech viñedos" --location Galicia
    python -m startup_hunter sources

Diseñada para que la invoque un agente por su tool de terminal, así que:
- el resumen legible va a **stdout** y los avisos a **stderr**, para poder separarlos;
- **cero candidatas sale con código 0**: es un resultado válido, no un error;
- solo un fallo real (consulta inválida, ningún conector utilizable) devuelve código != 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from startup_hunter import connectors as connector_registry
from startup_hunter.budget import Budget
from startup_hunter.connectors import LinkedInEnricher
from startup_hunter.ledger import Ledger
from startup_hunter.pipeline import run_hunt
from startup_hunter.report import write_output
from startup_hunter.schema import HuntResult, QuerySpec

_DEFAULT_OUT_DIR = "output"
#: Días sin confirmar tras los que una candidata enviada vuelve a contar como nueva. Ventana
#: de recuperación at-least-once: es preferible duplicar una entrega a perderla.
_DEFAULT_RESEND_DAYS = 4
_RESEND_DAYS_ENV = "STARTUP_HUNTER_RESEND_DAYS"


def _resend_after_days() -> int:
    raw = os.environ.get(_RESEND_DAYS_ENV)
    if raw is None:
        return _DEFAULT_RESEND_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_RESEND_DAYS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m startup_hunter",
        description=(
            "Busca empresas candidatas a cliente con evidencia pública verificable. "
            "El pipeline recoge hechos con evidencia; no decide si encajan."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    hunt = sub.add_parser("hunt", help="Ejecuta una búsqueda y escribe los resultados.")
    hunt.add_argument("query", help="Qué buscar, en lenguaje natural. Ej: 'agritech viñedos'.")
    hunt.add_argument(
        "--location",
        default=None,
        help="Zona a la que acotar (ej. Galicia). Si se omite, no se filtra por geografía.",
    )
    hunt.add_argument("--sector", default=None, help="Pista de sector para las fuentes que filtran.")
    hunt.add_argument(
        "--grant-theme",
        default=None,
        help=(
            "Solo para 'bdns': acota por el texto de la CONVOCATORIA, no por el sector de "
            "la empresa. Usa términos administrativos ('innovacion', 'digitalizacion', "
            "'agraria'); 'agritech' o 'I+D' no aparecen en las órdenes y dan cero. Si no "
            "casa con nada, se reintenta sin él y se anota."
        ),
    )
    hunt.add_argument(
        "--sources",
        default=",".join(connector_registry.DEFAULT_SOURCES),
        help=(
            "Conectores separados por comas. Disponibles: "
            f"{', '.join(connector_registry.available())}. "
            f"Por defecto: {','.join(connector_registry.DEFAULT_SOURCES)}."
        ),
    )
    hunt.add_argument(
        "--max-per-source",
        type=int,
        default=20,
        help="Máximo de resultados conservados por conector (por defecto 20).",
    )
    hunt.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "Enriquece las primeras candidatas con LinkedIn público (perfil + decisor). "
            "Consume créditos de Apify."
        ),
    )
    hunt.add_argument(
        "--out",
        default=None,
        help=(
            "Directorio de salida. Por defecto STARTUP_HUNTER_OUTPUT_DIR, "
            f"o '{_DEFAULT_OUT_DIR}' si no está definida."
        ),
    )

    sub.add_parser("sources", help="Lista los conectores disponibles.")

    mark = sub.add_parser(
        "mark-seen",
        help=(
            "Fase 1: marca las candidatas de un hunt como ENVIADAS, pendientes de "
            "confirmar. Ejecútalo justo después de mandarle el mensaje. Reversible: si el "
            "mensaje no llegó, 'confirm-seen' no las confirmará y volverán a aparecer."
        ),
    )
    mark.add_argument(
        "--file",
        default=None,
        help=(
            "candidates.json a registrar. Por defecto, el del directorio de salida "
            f"habitual ('{_DEFAULT_OUT_DIR}/candidates.json')."
        ),
    )

    confirm = sub.add_parser(
        "confirm-seen",
        help=(
            "Fase 2: confirma que el destinatario recibió lo enviado. "
            "Sin --file, confirma TODAS las pendientes."
        ),
    )
    confirm.add_argument(
        "--file",
        default=None,
        help="candidates.json concreto a confirmar. Por defecto, confirma todas las pendientes.",
    )

    pending = sub.add_parser(
        "pending-seen",
        help=(
            "Lista las candidatas enviadas pero nunca confirmadas hace >= N días: los "
            "sospechosos de una entrega perdida, para reenviarlas (at-least-once)."
        ),
    )
    pending.add_argument(
        "--days", type=int, default=_DEFAULT_RESEND_DAYS,
        help=f"Días sin confirmar para considerar perdida una entrega (por defecto {_DEFAULT_RESEND_DAYS}).",
    )
    return parser


def _cmd_sources() -> int:
    print("Conectores disponibles:")
    for name in connector_registry.available():
        mark = " (por defecto)" if name in connector_registry.DEFAULT_SOURCES else ""
        print(f"  - {name}{mark}")
    return 0


def _cmd_hunt(args: argparse.Namespace) -> int:
    query = args.query.strip()
    if not query:
        print("La consulta no puede estar vacía.", file=sys.stderr)
        return 2

    names = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not names:
        print("Hay que indicar al menos un conector en --sources.", file=sys.stderr)
        return 2
    try:
        sources = connector_registry.build_many(names, grant_theme=args.grant_theme)
    except KeyError as exc:
        print(str(exc).strip('"'), file=sys.stderr)
        return 2

    spec = QuerySpec(
        query=query,
        location=args.location,
        sector=args.sector,
        max_per_source=args.max_per_source,
    )
    result = run_hunt(
        spec,
        sources,
        enricher=LinkedInEnricher() if args.enrich else None,
        budget=Budget(),
        ledger=Ledger(),
        resend_after_days=_resend_after_days(),
    )
    out_dir = args.out or os.environ.get("STARTUP_HUNTER_OUTPUT_DIR") or _DEFAULT_OUT_DIR
    json_path = write_output(result, out_dir)
    _print_summary(result, json_path)
    return 0


def _load_candidates(path: Path) -> HuntResult | int:
    """Lee un candidates.json; devuelve el HuntResult o un código de salida de error."""
    if not path.exists():
        print(f"No existe {path}. Lanza un hunt antes.", file=sys.stderr)
        return 2
    try:
        return HuntResult.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - el mensaje importa más que el tipo
        print(f"{path} no es un candidates.json válido: {exc}", file=sys.stderr)
        return 2


def _cmd_mark_seen(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else Path(_DEFAULT_OUT_DIR) / "candidates.json"
    result = _load_candidates(path)
    if isinstance(result, int):
        return result

    ledger = Ledger()
    added = ledger.mark_reported(result.candidates)
    print(
        f"{added} candidata(s) marcada(s) como enviada(s), pendientes de confirmar "
        f"({len(result.candidates)} en el fichero, {len(ledger)} en el registro)."
    )
    print("Cuando el destinatario confirme la recepción: python -m startup_hunter confirm-seen")
    print(f"Registro: {ledger.path}")
    return 0


def _cmd_confirm_seen(args: argparse.Namespace) -> int:
    ledger = Ledger()
    if args.file:
        result = _load_candidates(Path(args.file))
        if isinstance(result, int):
            return result
        promoted = ledger.confirm(result.candidates)
    else:
        promoted = ledger.confirm()  # todas las pendientes
    print(f"{promoted} candidata(s) confirmada(s) como recibida(s).")
    print(f"Registro: {ledger.path}")
    return 0


def _cmd_pending_seen(args: argparse.Namespace) -> int:
    ledger = Ledger()
    stale = ledger.stale_unconfirmed(args.days)
    if not stale:
        print(f"Sin entregas pendientes de confirmar de hace {args.days}+ días.")
        return 0
    print(
        f"{len(stale)} candidata(s) enviada(s) sin confirmar hace {args.days}+ días "
        "(posible entrega perdida; el próximo hunt las volverá a proponer si reaparecen):"
    )
    for key in stale:
        print(f"  - {key}")
    return 0


def _print_summary(result, json_path: Path) -> None:
    total = len(result.candidates)
    new = result.stats.get("new")
    header = f"\n{result.query} - {total} candidata(s)"
    print(f"{header}, {new} nueva(s)." if new is not None else f"{header}.")

    if total:
        review = sum(1 for c in result.candidates if c.needs_review)
        for i, candidate in enumerate(result.candidates[:10], 1):
            signal = candidate.signals[0] if candidate.signals else "sin señal registrada"
            mark = "" if candidate.is_new is not False else " [ya reportada]"
            print(f"  {i}. {candidate.name} - {signal}{mark}")
        if total > 10:
            print(f"  … y {total - 10} más (ver {json_path}).")
        if review:
            print(f"\n{review} candidata(s) marcada(s) como posible duplicado.")
    else:
        print("Cero candidatas. Es un resultado válido: la consulta no dio empresas con evidencia.")

    print("\nCobertura por fuente:")
    for source, note in result.source_notes.items():
        print(f"  - {source}: {note}")

    print(f"\nSalida:\n  {json_path}  (contrato para el agente)")
    if result.stats.get("new"):
        print("\nTras enviar el mensaje: python -m startup_hunter mark-seen")


def main(argv: list[str] | None = None) -> int:
    # Carga el .env si existe. Idempotente y no-op sin fichero, así que es seguro siempre.
    load_dotenv()
    args = _build_parser().parse_args(argv)
    if args.command == "sources":
        return _cmd_sources()
    if args.command == "mark-seen":
        return _cmd_mark_seen(args)
    if args.command == "confirm-seen":
        return _cmd_confirm_seen(args)
    if args.command == "pending-seen":
        return _cmd_pending_seen(args)
    return _cmd_hunt(args)


if __name__ == "__main__":
    raise SystemExit(main())
