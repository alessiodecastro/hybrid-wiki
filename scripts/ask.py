"""
CLI per interrogare la knowledge base.

Due modalità d'uso:
- Domanda singola:  python scripts/ask.py "Chi è Frodo?"
- Batch dall'eval set: python scripts/ask.py --eval tests/eval_set.yaml

In modalità eval, ogni domanda viene confrontata visivamente con
la risposta attesa: utile per spot-check durante lo sviluppo.
L'output dell'eval, oltre che in console, viene salvato in
tests/results/evalset_results_YYYYMMDD_HHMMSS.txt (un file per run).
Nessun scoring automatico nel walking skeleton.
"""

import sys
from pathlib import Path
from datetime import datetime

import click
import yaml

# Permette di lanciare lo script da qualsiasi cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.query import QueryPipeline
from src.config import EVAL_RESULTS_DIR


def _result_block(question: str, result: dict) -> str:
    """Render leggibile (stringa) della risposta + metadata di una query.

    Restituisce una stringa invece di stampare direttamente: così lo
    stesso identico contenuto può essere mandato in console E scritto
    nel file dei risultati dell'eval, senza divergenze.
    """
    lines = [
        "=" * 70,
        f"Q: {question}",
        "-" * 70,
        result["answer"] or "(nessuna risposta generata)",
        "-" * 70,
        # Le sorgenti sono ciò che davvero permette di verificare la
        # risposta: vanno sempre mostrate, anche quando vuote.
        f"Wiki sources : {', '.join(result['wiki_sources']) or '(nessuna)'}",
        f"Raw  sources : {', '.join(result['raw_sources']) or '(nessuna)'}",
        f"Confidence   : {result['confidence']}",
    ]
    if result.get("gaps"):
        lines.append(f"Gaps         : {result['gaps']}")
    lines.append("=" * 70)
    return "\n".join(lines)


@click.command()
@click.argument("question", required=False)
@click.option(
    "--eval", "eval_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Esegue tutte le domande dell'eval set YAML.",
)
@click.option(
    "--domain", default=None,
    help="Filtra il retrieval a un singolo dominio (es. tolkien, asimov). "
         "Le pagine wiki '_mixed' vengono comunque incluse.",
)
def main(question: str | None, eval_path: str | None, domain: str | None):
    """Pone una domanda alla knowledge base."""
    # Una sola istanza di pipeline anche in modalità batch: i client
    # vengono inizializzati una volta sola.
    pipeline = QueryPipeline()

    if eval_path:
        # Modalità batch: scorri tutte le domande dell'eval set.
        # Output side-by-side: risposta del sistema + attesa dichiarata.
        # Il --domain (se passato) si applica a TUTTE le domande dell'eval.
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = yaml.safe_load(f)
        items = eval_data.get("questions", [])

        # Buffer del report: viene echeggiato in console riga per riga e,
        # a fine run, scritto su file. Stesso contenuto nei due output.
        report: list[str] = [
            f"# Eval set: {eval_path}",
            f"# Eseguito: {datetime.now().isoformat(timespec='seconds')}",
            f"# Filtro dominio CLI: {domain or '(nessuno)'}",
            f"# Domande: {len(items)}",
            "",
        ]

        def emit(text: str) -> None:
            """Tee: console + buffer del file (un'unica fonte di verità)."""
            click.echo(text)
            report.append(text)

        for item in items:
            q = item["question"]
            # Ogni item può sovrascrivere il dominio (utile per eval
            # set cross-dominio dove ogni domanda ha il suo scope).
            item_domain = item.get("domain", domain)
            result = pipeline.ask(q, domain=item_domain)
            emit(_result_block(q, result))
            emit(f"Atteso: {item.get('expected_summary', '(n/a)')}")
            emit(f"Sorgenti attese: {item.get('expected_sources', [])}")
            emit("")

        # Summary cumulativo di tutte le query dell'eval set.
        emit(pipeline.tracker.format_session_summary())

        # Persistenza: un file per run, timestamp al secondo.
        EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = EVAL_RESULTS_DIR / f"evalset_results_{ts}.txt"
        out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        click.echo(f"\nRisultati salvati in: {out_path}")
        return

    if not question:
        raise click.UsageError("Specificare una domanda oppure --eval <file.yaml>")
    result = pipeline.ask(question, domain=domain)
    click.echo(_result_block(question, result))
    # Summary token della singola query — utile per spot-check di costo.
    click.echo(pipeline.tracker.format_session_summary())


if __name__ == "__main__":
    main()
