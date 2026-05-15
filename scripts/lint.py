import sys
import re
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stores import RawStore, WikiStore, VectorDB
from src.config import HOT_LAYER_PATH, RAW_COLLECTION, WIKI_COLLECTION


WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_]+)\]\]")


@click.command()
def main():
    """Health check manuale del walking skeleton — solo report, niente azioni."""
    raw = RawStore()
    wiki = WikiStore()
    vdb = VectorDB()

    click.echo("=== HYBRID WIKI — LINT REPORT ===\n")

    raw_ids = raw.list()
    wiki_ids = wiki.list()
    click.echo(f"Documenti raw : {len(raw_ids)}")
    click.echo(f"Pagine wiki   : {len(wiki_ids)}")
    click.echo(f"Vector raw    : {vdb.count(RAW_COLLECTION)}")
    click.echo(f"Vector wiki   : {vdb.count(WIKI_COLLECTION)}")
    click.echo()

    # Hot layer
    if HOT_LAYER_PATH.exists():
        content = HOT_LAYER_PATH.read_text(encoding="utf-8")
        est_tokens = max(1, len(content) // 4)
        click.echo(f"Hot Layer     : {len(content)} char, ~{est_tokens} token")
    else:
        click.echo("Hot Layer     : (assente)")
    click.echo()

    # Pagine senza sorgenti
    no_sources = []
    referenced_in_index: set[str] = set()
    if HOT_LAYER_PATH.exists():
        referenced_in_index = set(WIKILINK_RE.findall(HOT_LAYER_PATH.read_text(encoding="utf-8")))
    for pid in wiki_ids:
        fm, _ = wiki.get(pid)
        srcs = fm.get("sources") or []
        if not srcs:
            no_sources.append(pid)
    if no_sources:
        click.echo(f"[WARN] Pagine wiki senza sorgenti ({len(no_sources)}): {', '.join(no_sources)}")
    else:
        click.echo("OK   Tutte le pagine wiki hanno almeno una sorgente.")

    # Pagine orfane (non referenziate nell'index dell'Hot Layer)
    orphans = [pid for pid in wiki_ids if pid not in referenced_in_index]
    if orphans:
        click.echo(f"[WARN] Pagine non presenti nell'index del Hot Layer ({len(orphans)}): {', '.join(orphans)}")
    else:
        click.echo("OK   Tutte le pagine sono indicizzate nel Hot Layer.")

    # Sorgenti citate ma non presenti nel raw store
    missing_raw = []
    for pid in wiki_ids:
        fm, _ = wiki.get(pid)
        for s in (fm.get("sources") or []):
            if not raw.exists(s):
                missing_raw.append((pid, s))
    if missing_raw:
        click.echo(f"[WARN] Riferimenti a doc_id non presenti nel raw store: {missing_raw}")
    else:
        click.echo("OK   Tutte le sorgenti citate esistono nel raw store.")

    click.echo("\n=== FINE REPORT ===")


if __name__ == "__main__":
    main()
