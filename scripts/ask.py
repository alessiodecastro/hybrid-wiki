"""
CLI per interrogare la knowledge base.

Due modalità d'uso:
- Domanda singola:  python scripts/ask.py "Chi è Frodo?"
- Batch dall'eval set: python scripts/ask.py --eval tests/eval_set.yaml

In modalità eval, ogni domanda viene confrontata visivamente con
la risposta attesa: utile per spot-check durante lo sviluppo.
Nessun scoring automatico nel walking skeleton.
"""

import sys
from pathlib import Path

import click
import yaml

# Permette di lanciare lo script da qualsiasi cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.query import QueryPipeline


def _print_result(question: str, result: dict) -> None:
    """Render leggibile della risposta + metadata di una singola query."""
    click.echo("=" * 70)
    click.echo(f"Q: {question}")
    click.echo("-" * 70)
    click.echo(result["answer"] or "(nessuna risposta generata)")
    click.echo("-" * 70)
    # Le sorgenti sono ciò che davvero permette di verificare la risposta:
    # vanno sempre mostrate, anche quando vuote (per evidenziare il problema).
    click.echo(f"Wiki sources : {', '.join(result['wiki_sources']) or '(nessuna)'}")
    click.echo(f"Raw  sources : {', '.join(result['raw_sources']) or '(nessuna)'}")
    click.echo(f"Confidence   : {result['confidence']}")
    if result.get("gaps"):
        click.echo(f"Gaps         : {result['gaps']}")
    click.echo("=" * 70)


@click.command()
@click.argument("question", required=False)
@click.option(
    "--eval", "eval_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Esegue tutte le domande dell'eval set YAML.",
)
def main(question: str | None, eval_path: str | None):
    """Pone una domanda alla knowledge base."""
    # Una sola istanza di pipeline anche in modalità batch: i client
    # vengono inizializzati una volta sola.
    pipeline = QueryPipeline()

    if eval_path:
        # Modalità batch: scorri tutte le domande dell'eval set.
        # Output side-by-side: risposta del sistema + attesa dichiarata.
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = yaml.safe_load(f)
        items = eval_data.get("questions", [])
        for item in items:
            q = item["question"]
            result = pipeline.ask(q)
            _print_result(q, result)
            click.echo(f"Atteso: {item.get('expected_summary', '(n/a)')}")
            click.echo(f"Sorgenti attese: {item.get('expected_sources', [])}")
            click.echo()
        return

    if not question:
        raise click.UsageError("Specificare una domanda oppure --eval <file.yaml>")
    result = pipeline.ask(question)
    _print_result(question, result)


if __name__ == "__main__":
    main()
