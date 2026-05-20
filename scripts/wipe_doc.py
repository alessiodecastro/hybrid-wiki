"""
Utility una-tantum: rimuove uno o più documenti completi (raw + source
page + tutti i vettori associati) dato il loro doc_id.

Usata per ripulire duplicati lasciati da ingest L2 falliti a metà:
ingest_l2 crea raw + source page PRIMA di entrare in _integrate_entities;
se quello step crasha (es. content filter), i passi precedenti non
vengono rollbackati, e ogni retry crea un nuovo doc_id con timestamp
diverso → accumulo di duplicati.

NB: il principio "raw immutabile" (design §3.2) si applica al raw
INGESTATO con successo; un raw lasciato da un ingest fallito è materiale
spurio e può essere rimosso. Per pulire raw integri usare la promozione,
non questo script.

Uso:
    python scripts/wipe_doc.py harry_potter_20260520114729 [...altri doc_id...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stores import VectorDB
from src.config import RAW_DIR, WIKI_DIR, RAW_COLLECTION, WIKI_COLLECTION


def wipe_doc(doc_id: str, vdb: VectorDB) -> dict:
    """Rimuove raw file + source page file + vettori raw chunks + vettore
    source page. Idempotente: chiamarlo su un doc_id già pulito è no-op.
    """
    out = {"doc_id": doc_id}

    raw_path = RAW_DIR / f"{doc_id}.md"
    out["raw_md"] = raw_path.exists()
    if raw_path.exists():
        raw_path.unlink()

    source_id = f"source_{doc_id}"
    source_path = WIKI_DIR / f"{source_id}.md"
    out["source_md"] = source_path.exists()
    if source_path.exists():
        source_path.unlink()

    # Chunks raw: id = "{doc_id}__chunk_NNN", metadata.doc_id = doc_id.
    # delete_where con filtro metadata pulisce tutti in un colpo.
    vdb.delete_where(RAW_COLLECTION, {"doc_id": doc_id})
    out["raw_chunks_deleted"] = "(via where)"

    # Vettore source page: id deterministico.
    vdb.delete(WIKI_COLLECTION, [source_id])
    out["source_vector_deleted"] = True

    return out


def main(doc_ids: list[str]) -> None:
    if not doc_ids:
        print("Uso: python scripts/wipe_doc.py <doc_id> [<doc_id> ...]")
        sys.exit(1)

    vdb = VectorDB()
    for did in doc_ids:
        res = wipe_doc(did, vdb)
        print(f"  {did}")
        print(f"     raw md       : {'rimosso' if res['raw_md'] else 'assente'}")
        print(f"     source md    : {'rimosso' if res['source_md'] else 'assente'}")
        print(f"     raw chunks   : cancellati (where doc_id=={did})")
        print(f"     source vec   : cancellato")

    print(f"\nFatto. {len(doc_ids)} documenti puliti.")


if __name__ == "__main__":
    main(sys.argv[1:])
