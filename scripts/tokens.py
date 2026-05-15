"""
Report cumulativo del consumo token.

Legge data/token_log.jsonl (alimentato dai TokenTracker delle pipeline)
e produce aggregati per fase, operazione, modello e finestra temporale.

Esempi:
    # report totale dall'inizio della raccolta
    python scripts/tokens.py

    # solo le righe di una specifica fase (prefix match)
    python scripts/tokens.py --phase ingest

    # solo le ultime 24 ore
    python scripts/tokens.py --since 24h
"""

import sys
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import TOKEN_LOG_PATH


def _parse_since(spec: str | None) -> datetime | None:
    """Parsa una specifica relativa tipo '24h', '7d', '30m'.

    Returns:
        datetime soglia (records ANTERIORI vanno scartati) oppure None
        se la specifica è assente.
    """
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", spec.strip())
    if not m:
        raise click.UsageError(f"Formato --since non valido: {spec!r}. Usa es. 24h, 30m, 7d.")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    return datetime.now() - delta


def _load_records(path: Path) -> list[dict]:
    """Carica i record JSONL, scartando le righe corrotte (best effort)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Riga malformata: la saltiamo per non perdere tutto il report
            # a causa di un crash a metà scrittura. Continueremo a notarlo
            # in modo soft tramite il conteggio totale.
            continue
    return out


def _filter(records: list[dict], phase_prefix: str | None, since: datetime | None) -> list[dict]:
    """Applica i filtri da CLI in un unico passaggio."""
    out = []
    for r in records:
        if phase_prefix and not r.get("phase", "").startswith(phase_prefix):
            continue
        if since:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts < since:
                continue
        out.append(r)
    return out


def _aggregate(records: list[dict], group_by: str) -> list[dict]:
    """Aggrega i record per la chiave indicata (phase | operation | model).

    Returns:
        Lista di righe ordinate per total_tokens DESC.
    """
    buckets: dict[str, dict] = {}
    for r in records:
        key = r.get(group_by, "?")
        agg = buckets.setdefault(key, {
            group_by: key, "calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0,
        })
        agg["calls"] += 1
        agg["prompt_tokens"] += r.get("prompt_tokens", 0)
        agg["completion_tokens"] += r.get("completion_tokens", 0)
        agg["cached_tokens"] += r.get("cached_tokens", 0)
        agg["total_tokens"] += r.get("total_tokens", 0)
    return sorted(buckets.values(), key=lambda x: -x["total_tokens"])


def _print_table(title: str, rows: list[dict], key_col: str) -> None:
    """Stampa una tabella aggregata in formato fixed-width."""
    click.echo(f"\n=== {title} ===")
    if not rows:
        click.echo("(nessun dato)")
        return
    click.echo(
        f"{key_col:<35} {'calls':>6} {'prompt':>10} {'compl.':>10} "
        f"{'cached':>10} {'total':>10}"
    )
    click.echo("-" * 90)
    for r in rows:
        click.echo(
            f"{str(r[key_col]):<35} {r['calls']:>6} {r['prompt_tokens']:>10} "
            f"{r['completion_tokens']:>10} {r['cached_tokens']:>10} {r['total_tokens']:>10}"
        )


@click.command()
@click.option("--phase", "phase_prefix", default=None, help="Filtra per prefisso fase (es. 'ingest', 'query:llm').")
@click.option("--since", "since_spec", default=None, help="Finestra temporale: es. 30m, 24h, 7d.")
@click.option("--log", "log_path", type=click.Path(), default=None, help="Path alternativo al log JSONL.")
def main(phase_prefix: str | None, since_spec: str | None, log_path: str | None):
    """Stampa il report cumulativo del consumo token."""
    path = Path(log_path) if log_path else TOKEN_LOG_PATH
    since = _parse_since(since_spec)

    records = _load_records(path)
    filtered = _filter(records, phase_prefix, since)

    click.echo(f"Log: {path}")
    click.echo(f"Record totali     : {len(records)}")
    click.echo(f"Record filtrati   : {len(filtered)}")
    if phase_prefix:
        click.echo(f"Filtro fase       : {phase_prefix}*")
    if since:
        click.echo(f"Filtro temporale  : da {since.isoformat(timespec='seconds')}")

    if not filtered:
        return

    # Totali globali sulla finestra filtrata.
    totals = {
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in filtered),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in filtered),
        "cached_tokens": sum(r.get("cached_tokens", 0) for r in filtered),
        "total_tokens": sum(r.get("total_tokens", 0) for r in filtered),
    }
    click.echo(
        f"\nTotali: prompt={totals['prompt_tokens']:,}  "
        f"completion={totals['completion_tokens']:,}  "
        f"cached={totals['cached_tokens']:,}  "
        f"total={totals['total_tokens']:,}"
    )

    # Tre aggregati per dare angolazioni diverse sullo stesso dataset.
    _print_table("BREAKDOWN PER FASE", _aggregate(filtered, "phase"), "phase")
    _print_table("BREAKDOWN PER OPERAZIONE", _aggregate(filtered, "operation"), "operation")
    _print_table("BREAKDOWN PER MODELLO", _aggregate(filtered, "model"), "model")


if __name__ == "__main__":
    main()
