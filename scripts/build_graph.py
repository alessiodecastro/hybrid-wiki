"""
Costruisce il grafo strutturale delle entità dal WikiStore + EntityIndex.

Modalità base (zero token LLM):
  NODI:  ogni entry nell'EntityIndex → un nodo Entity con snippet.
  ARCHI: co-menzione bidirezionale (CO_MENZIONATO) da doc_id condivisi.

Modalità --llm-relations (estrazione semantica):
  Dopo la fase base, per ogni entità chiama il LLM sulla pagina wiki
  più ricca disponibile ed estrae triple tipate:
    CONOSCE, E_UN, SI_TROVA_IN, PARTE_DI, OPPONE, ALLEATO_DI,
    POSSIEDE, CREA, GOVERNA, DISTRUGGE, MEMBRO_DI
  Le triple vengono aggiunte al grafo come archi aggiuntivi ai CO_MENZIONATO.
  Costo stimato: ~30 chiamate LLM × ~600 tok/call ≈ 18.000 token (one-time).

Uso:
    python scripts/build_graph.py                          # zero token
    python scripts/build_graph.py --rebuild                # wipe + rebuild
    python scripts/build_graph.py --rebuild --llm-relations  # con triple LLM
"""
import json
import re
import sys
from itertools import combinations
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import WIKI_DIR
from src.graph_store import GraphStore, _clean_body
from src.stores import EntityIndex, WikiStore


# ------------------------------------------------------------------
# Helper: caricamento body wiki
# ------------------------------------------------------------------

def _load_snippet(entity_id: str, state: str, sources: list[str], wiki: WikiStore) -> str:
    """Snippet breve (400 char) per il nodo grafo."""
    if state == "consolidated" and wiki.exists(entity_id):
        _, body = wiki.get(entity_id)
        return _clean_body(body)
    for doc_id in sources:
        source_pid = f"source_{doc_id}"
        if wiki.exists(source_pid):
            _, body = wiki.get(source_pid)
            return _extract_overview(body)[:400]
    return ""


def _load_full_body(entity_id: str, state: str, sources: list[str], wiki: WikiStore) -> str:
    """Body completo (per estrazione LLM triple). Priorità: entity page > prima source page."""
    if state == "consolidated" and wiki.exists(entity_id):
        _, body = wiki.get(entity_id)
        return body
    for doc_id in sources:
        source_pid = f"source_{doc_id}"
        if wiki.exists(source_pid):
            _, body = wiki.get(source_pid)
            return body
    return ""


def _extract_overview(body: str) -> str:
    """Testo dopo '## Overview' fino al prossimo heading, o body intero."""
    lower = body.lower()
    start = lower.find("## overview")
    if start == -1:
        return _clean_body(body)
    after = body[start + len("## overview"):]
    next_heading = after.find("\n##")
    if next_heading != -1:
        after = after[:next_heading]
    return after.strip()


# ------------------------------------------------------------------
# Estrazione triple LLM
# ------------------------------------------------------------------

_RELATION_TYPES = (
    "CONOSCE, E_UN, SI_TROVA_IN, PARTE_DI, OPPONE, ALLEATO_DI, "
    "POSSIEDE, CREA, GOVERNA, DISTRUGGE, MEMBRO_DI"
)

_TRIPLE_SYSTEM = (
    "Sei un estrattore di relazioni strutturate. "
    "Dato un testo su un'entità, estrai triple (soggetto, relazione, oggetto) "
    "usando SOLO gli entity_id dalla lista fornita per soggetto e oggetto. "
    "Non inventare entity_id non presenti in lista."
)


def _extract_triples(
    llm,
    entity_id: str,
    body: str,
    known_ids: set[str],
    tracker,
) -> list[dict]:
    """Chiama il LLM per estrarre triple tipate da un body wiki.

    Returns:
        Lista di dict {"from", "to", "type", "confidence"}, filtrata:
        - entrambi i campi from/to devono essere in known_ids
        - niente self-loop
        - type deve essere non vuoto
    """
    known_list = "\n".join(sorted(known_ids)[:80])
    body_trimmed = body[:1200]

    user = (
        f"Entita' principale: {entity_id}\n\n"
        f"entity_id noti nel corpus:\n{known_list}\n\n"
        f"Testo:\n{body_trimmed}\n\n"
        f"Tipi di relazione da usare:\n{_RELATION_TYPES}\n\n"
        "Rispondi SOLO con un JSON array (nessun altro testo):\n"
        '[{"from": "entity_id", "to": "entity_id", '
        '"type": "TIPO", "confidence": "EXTRACTED"}]\n'
        "Regola fondamentale: usa SOLO entity_id dalla lista sopra. "
        "Se l'oggetto di una relazione non e' in lista, ometti quella tripla."
    )

    with tracker.phase("build:llm_triples"):
        raw = llm.complete(system=_TRIPLE_SYSTEM, user=user, max_tokens=600)

    # Parse JSON array — tollerante: cerca il primo [...] nel testo
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        triples = json.loads(m.group(0))
    except Exception:
        return []

    valid = []
    for t in triples:
        if not isinstance(t, dict):
            continue
        frm = t.get("from", "")
        to = t.get("to", "")
        typ = t.get("type", "")
        if frm in known_ids and to in known_ids and typ and frm != to:
            valid.append({
                "from": frm,
                "to": to,
                "type": typ,
                "confidence": t.get("confidence", "EXTRACTED"),
            })
    return valid


# ------------------------------------------------------------------
# Comando principale
# ------------------------------------------------------------------

@click.command()
@click.option("--rebuild", is_flag=True, default=False,
              help="Wipe e rebuild completo anche se il grafo esiste gia'.")
@click.option("--llm-relations", "llm_relations", is_flag=True, default=False,
              help="Estrae triple tipate via LLM (costo ~18k token one-time).")
def build_graph(rebuild: bool, llm_relations: bool) -> None:
    """Costruisce il grafo da EntityIndex + WikiStore."""
    graph = GraphStore()

    n_entities = graph.count_entities()
    if n_entities > 0 and not rebuild:
        click.echo(
            f"Grafo gia' presente ({n_entities} entita'). "
            "Usa --rebuild per ricostruire da zero."
        )
        graph.close()
        return

    if rebuild:
        click.echo("Rebuild: wipe del grafo esistente...")
        graph.reset()

    entity_index = EntityIndex()
    wiki = WikiStore()
    entities = entity_index.list_all()

    # -- FASE 1: nodi --
    click.echo(f"Costruzione nodi ({len(entities)} entita')...")
    for e in entities:
        eid = e["id"]
        snippet = _load_snippet(eid, e.get("state", ""), e.get("sources", []), wiki)
        label = eid.replace("_", " ").title()
        graph.upsert_entity(
            id=eid,
            label=label,
            subtype=e.get("subtype", ""),
            domain=e.get("domain", ""),
            state=e.get("state", ""),
            wiki_snippet=snippet,
        )

    # -- FASE 2: archi co-menzione (zero token) --
    doc_to_entities: dict[str, list[str]] = {}
    for e in entities:
        for doc_id in e.get("sources", []):
            doc_to_entities.setdefault(doc_id, []).append(e["id"])

    click.echo(f"Costruzione archi co-menzione da {len(doc_to_entities)} documenti...")
    n_co = 0
    for doc_id, ent_ids in doc_to_entities.items():
        if len(ent_ids) < 2:
            continue
        for a, b in combinations(sorted(ent_ids), 2):
            graph.add_relation(a, b, rel_type="CO_MENZIONATO",
                               confidence="EXTRACTED", source_doc=doc_id)
            graph.add_relation(b, a, rel_type="CO_MENZIONATO",
                               confidence="EXTRACTED", source_doc=doc_id)
            n_co += 2

    # -- FASE 3 (opzionale): triple tipate LLM --
    n_typed = 0
    if llm_relations:
        from src.llm_client import LLMClient
        from src.token_tracker import TokenTracker

        tracker = TokenTracker(log_path=None)
        llm = LLMClient(tracker=tracker)
        known_ids = {e["id"] for e in entities}

        click.echo(f"\nEstrazione triple LLM ({len(entities)} entita')...")
        for e in entities:
            eid = e["id"]
            body = _load_full_body(eid, e.get("state", ""), e.get("sources", []), wiki)
            if not body:
                click.echo(f"  {eid}: nessun body, skip")
                continue

            try:
                triples = _extract_triples(llm, eid, body, known_ids, tracker)
            except Exception as ex:
                click.echo(f"  {eid}: ERRORE LLM ({ex}), skip")
                continue

            for t in triples:
                graph.add_relation(
                    t["from"], t["to"],
                    rel_type=t["type"],
                    confidence=t["confidence"],
                    source_doc="llm_extraction",
                )
                n_typed += 1

            click.echo(f"  {eid}: {len(triples)} triple")

        click.echo(click.style(f"\nTriple tipate aggiunte: {n_typed}", fg="green"))
        click.echo(tracker.format_session_summary())

    # -- Report finale --
    n_ent = graph.count_entities()
    n_rel = graph.count_relations()
    click.echo(f"\nGrafo costruito:")
    click.echo(f"  Nodi Entity       : {n_ent}")
    click.echo(f"  Archi CO_MENZIONATO: {n_co}")
    click.echo(f"  Archi tipati LLM  : {n_typed}")
    click.echo(f"  Archi totali      : {n_rel}")

    domains: dict[str, int] = {}
    for e in entities:
        d = e.get("domain", "unknown")
        domains[d] = domains.get(d, 0) + 1
    click.echo("\nNodi per dominio:")
    for d, cnt in sorted(domains.items(), key=lambda x: -x[1]):
        click.echo(f"  {d:<20} {cnt}")

    graph.close()


if __name__ == "__main__":
    build_graph()
