import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import IngestPipeline
from src.config import VALID_LEVELS, VALID_SUBTYPES


@click.command()
@click.option("--file", "file_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Percorso del documento sorgente (testo o markdown).")
@click.option("--title", required=True, help="Titolo leggibile del documento.")
@click.option("--level", required=True, type=click.Choice(sorted(VALID_LEVELS)), help="Livello di elaborazione: L0/L1/L2.")
@click.option("--subtype", default=None, type=click.Choice(sorted(VALID_SUBTYPES)), help="Suggerimento per il subtype dell'entità principale (solo L2).")
def main(file_path: str, title: str, level: str, subtype: str | None):
    """Ingestione di un singolo documento nella knowledge base."""
    click.echo(f"-> Ingest [{level}] {title}  (file={file_path})")
    pipeline = IngestPipeline()
    result = pipeline.ingest(file_path, title=title, level=level, subtype=subtype)
    click.echo(f"   doc_id     : {result['doc_id']}")
    click.echo(f"   wiki pages : {', '.join(result['wiki_pages']) if result['wiki_pages'] else '(nessuna)'}")
    click.echo("OK")


if __name__ == "__main__":
    main()
