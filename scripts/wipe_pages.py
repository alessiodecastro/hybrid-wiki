"""
Utility una-tantum: rimuove pagine wiki (file md) e i relativi vettori in
ChromaDB. Usata per resettare entità "cupe" generate da testi non mitigati
che innescano il content filter Azure durante i merge successivi.

Non opera sui RawStore: i raw immutabili NON vanno mai toccati (design §3.2).

Uso:
    python scripts/wipe_pages.py harry_potter lord_voldemort first_fall_of_voldemort
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stores import WikiStore, VectorDB, EntityIndex
from src.config import WIKI_COLLECTION


def main(page_ids: list[str]) -> None:
    if not page_ids:
        print("Uso: python scripts/wipe_pages.py <page_id> [<page_id> ...]")
        sys.exit(1)

    wiki = WikiStore()
    vdb = VectorDB()
    index = EntityIndex()

    for pid in page_ids:
        removed = wiki.delete_page(pid)
        vdb.delete(WIKI_COLLECTION, [pid])
        # Se è un entity_id consolidato nell'indice, lo rimuoviamo
        # interamente (le source restano nei loro raw, ma l'entry
        # consolidated viene cancellata e potrà essere ri-creata).
        index_removed = index.remove(pid)
        print(f"  {pid:<40} file: {'rimosso' if removed else 'assente'}  "
              f"vettore: cancellato  indice: {'rimosso' if index_removed else 'assente'}")

    index.save()
    print(f"\nFatto. {len(page_ids)} pagine processate.")
    print("Le pagine verranno ricreate al prossimo ingest che le menzioni come entità.")


if __name__ == "__main__":
    main(sys.argv[1:])
