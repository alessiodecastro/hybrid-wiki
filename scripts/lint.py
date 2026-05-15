"""
Health check manuale della knowledge base (walking skeleton).

NON è la "lint pipeline" automatica del design (sezione 6.3): qui produce
solo un report leggibile da un essere umano, senza eseguire azioni.
Si chiama `lint` per coerenza con il nome che avrà il modulo automatizzato.

Verifiche eseguite:
- Conteggi base (documenti raw, pagine wiki, vettori).
- Dimensione del Hot Layer in token stimati.
- Pagine wiki senza sorgenti (anomalia: ogni pagina dovrebbe citare almeno
  un doc_id da cui deriva).
- Pagine wiki non referenziate nell'index del Hot Layer (potenziali orfani).
- Sorgenti citate ma non presenti nel raw store (broken references).
"""

import sys
import re
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stores import RawStore, WikiStore, VectorDB
from src.config import HOT_LAYER_PATH, RAW_COLLECTION, WIKI_COLLECTION


# Regex per catturare i wikilink `[[id]]` usati come riferimenti incrociati.
# Solo id "puliti" (alfanumerici + underscore): esclude false positive
# come `[[esempio con spazi]]` o markup decorativo.
WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_]+)\]\]")


@click.command()
def main():
    """Health check manuale del walking skeleton — solo report, niente azioni."""
    raw = RawStore()
    wiki = WikiStore()
    vdb = VectorDB()

    click.echo("=== HYBRID WIKI — LINT REPORT ===\n")

    # -------------------- Conteggi base --------------------
    raw_ids = raw.list()
    wiki_ids = wiki.list()
    click.echo(f"Documenti raw : {len(raw_ids)}")
    click.echo(f"Pagine wiki   : {len(wiki_ids)}")
    click.echo(f"Vector raw    : {vdb.count(RAW_COLLECTION)}")
    click.echo(f"Vector wiki   : {vdb.count(WIKI_COLLECTION)}")
    click.echo()

    # -------------------- Dimensione Hot Layer --------------------
    # Stima 1 token ≈ 4 caratteri (stesso criterio del modulo hot_layer).
    if HOT_LAYER_PATH.exists():
        content = HOT_LAYER_PATH.read_text(encoding="utf-8")
        est_tokens = max(1, len(content) // 4)
        click.echo(f"Hot Layer     : {len(content)} char, ~{est_tokens} token")
    else:
        click.echo("Hot Layer     : (assente)")
    click.echo()

    # -------------------- Pagine senza sorgenti --------------------
    # Sintomo di un bug nell'ingest: una pagina wiki dovrebbe sempre
    # tracciare almeno un doc_id da cui deriva (audit trail).
    no_sources = []
    # Costruisce l'insieme degli id referenziati nel Hot Layer per il check
    # successivo sugli orfani. Fatto qui per leggere il file una sola volta.
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

    # -------------------- Pagine orfane nell'index --------------------
    # Una pagina che NON compare nell'index del Hot Layer è di fatto invisibile
    # alle query basate su orientamento. Anomalia: il rebuild dovrebbe sempre
    # includere ogni pagina wiki esistente.
    orphans = [pid for pid in wiki_ids if pid not in referenced_in_index]
    if orphans:
        click.echo(f"[WARN] Pagine non presenti nell'index del Hot Layer ({len(orphans)}): {', '.join(orphans)}")
    else:
        click.echo("OK   Tutte le pagine sono indicizzate nel Hot Layer.")

    # -------------------- Riferimenti raw rotti --------------------
    # Citazione di un doc_id che non esiste nello store: indica un bug
    # nella generazione (es. il modello ha inventato un id) o un raw
    # cancellato a mano. Non dovrebbe mai succedere a regime.
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
