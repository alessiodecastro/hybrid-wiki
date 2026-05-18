"""
CLI per l'ingest di un singolo documento.

Esempio:
    python scripts/ingest_doc.py --file data/raw/incoming/frodo_intro.txt \
        --title "Frodo Baggins — introduzione" --level L2 --subtype character

Il livello è scelto manualmente dall'utente — convenzione del walking
skeleton, vedi schema/AGENTS.md sezione "Criteri L0/L1/L2".
"""

import sys
from pathlib import Path

import click

# Aggiunge la root del progetto al path per poter importare `src` come
# package quando lo script viene invocato da qualsiasi cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import IngestPipeline
from src.config import VALID_LEVELS, VALID_SUBTYPES, DEFAULT_DOMAIN


@click.command()
@click.option(
    "--file", "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Percorso del documento sorgente (testo o markdown).",
)
@click.option(
    "--title", required=True,
    help="Titolo leggibile del documento. Usato come metadato e come base per il doc_id.",
)
@click.option(
    "--level", required=True,
    type=click.Choice(sorted(VALID_LEVELS)),
    help="Livello di elaborazione: L0 (solo raw), L1 (raw + source page), L2 (L1 + integrazione entità).",
)
@click.option(
    "--subtype", default=None,
    type=click.Choice(sorted(VALID_SUBTYPES)),
    help="Suggerimento per il subtype dell'entità principale (solo L2).",
)
@click.option(
    "--domain", default=DEFAULT_DOMAIN,
    help=f"Etichetta di dominio (default: {DEFAULT_DOMAIN}). Stringa libera, "
         "abilita il filtro --domain in ask.py.",
)
def main(file_path: str, title: str, level: str, subtype: str | None, domain: str):
    """Ingestione di un singolo documento nella knowledge base."""
    click.echo(f"-> Ingest [{level}] {title}  (file={file_path}, domain={domain})")
    # Inizializzazione pipeline = creazione client LLM/embedder + connessione
    # ChromaDB. Cost-effective per ingest singolo; per batch si preferirà
    # uno script dedicato che riusa la pipeline.
    pipeline = IngestPipeline()
    result = pipeline.ingest(file_path, title=title, level=level, subtype=subtype, domain=domain)
    click.echo(f"   doc_id     : {result['doc_id']}")
    click.echo(f"   wiki pages : {', '.join(result['wiki_pages']) if result['wiki_pages'] else '(nessuna)'}")
    click.echo("OK")
    # Stampa il breakdown dei token consumati da questa singola ingest.
    # Il log persistente su disco continua ad accumulare tra invocazioni;
    # questo summary mostra solo la sessione corrente.
    click.echo(pipeline.tracker.format_session_summary())


if __name__ == "__main__":
    main()
