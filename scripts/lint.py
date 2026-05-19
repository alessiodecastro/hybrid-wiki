"""
Lint CLI della knowledge base.

Tre modalità:
- (default, nessun flag)   health check read-only (conteggi, orfani,
                           sorgenti rotte). Invariato dal walking skeleton.
- --detect-duplicates      FASE 1 lint pipeline (§6.3 / §11.1): trova
                           cluster di pagine entità candidate-duplicato e
                           scrive un report YAML da revisionare. Read-only
                           sulla wiki.
- --apply-consolidation    FASE 2: applica i SOLI cluster con
                           `approved: true` nel report. Distruttivo,
                           reversibile (git + audit con snapshot).

Le due fasi della consolidazione sono separate per design: l'output del
lint non è automatico, richiede triage umano (§6.3).
"""

import sys
import re
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stores import RawStore, WikiStore, VectorDB
from src.config import HOT_LAYER_PATH, RAW_COLLECTION, WIKI_COLLECTION, CONSOLIDATION_REPORT_PATH


# Regex per catturare i wikilink `[[id]]` usati come riferimenti incrociati.
# Solo id "puliti" (alfanumerici + underscore): esclude false positive
# come `[[esempio con spazi]]` o markup decorativo.
WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_]+)\]\]")


def _health_check():
    """Health check read-only — solo report, niente azioni."""
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


@click.command()
@click.option("--detect-duplicates", is_flag=True, default=False,
              help="FASE 1: rileva cluster duplicati/alias e scrive il report YAML (read-only).")
@click.option("--apply-consolidation", is_flag=True, default=False,
              help="FASE 2: applica i cluster con approved:true nel report (distruttivo).")
@click.option("--audit-l0", is_flag=True, default=False,
              help="Audit campionario L0 (§6.3 #7): ri-classifica i doc L0 e segnala candidati promozione (read-only).")
@click.option("--sample", default=0, type=int,
              help="Con --audit-l0: numero di doc L0 da campionare (0 = tutti).")
def main(detect_duplicates: bool, apply_consolidation: bool, audit_l0: bool, sample: int):
    """Lint della knowledge base. Senza flag: health check read-only."""
    if sum([detect_duplicates, apply_consolidation, audit_l0]) > 1:
        raise click.UsageError("Una modalità per volta.")

    if audit_l0:
        import random
        from src.stores import RawStore as _RS
        from src.classifier import LevelClassifier
        raw = _RS()
        l0_docs = []
        for did in raw.list():
            fm, _b = raw.get(did)
            if fm.get("level") == "L0":
                l0_docs.append(did)
        if sample and sample < len(l0_docs):
            l0_docs = random.sample(l0_docs, sample)
        click.echo(f"=== AUDIT L0 ({len(l0_docs)} documenti) ===")
        if not l0_docs:
            click.echo("Nessun documento L0 da auditare.")
            return
        clf = LevelClassifier()
        promote_candidates = []
        for did in l0_docs:
            fm, body = raw.get(did)
            res = clf.classify(title=fm.get("title", did), body=body,
                               domain=fm.get("domain", "?"), source_name=fm.get("source", ""))
            flag = "PROMOTE" if res["level"] != "L0" else "ok     "
            click.echo(f"[{flag}] {did}  -> {res['level']}/{res['confidence']}  {res['rationale'][:90]}")
            if res["level"] != "L0":
                promote_candidates.append((did, res["level"], res["confidence"]))
        click.echo(f"\nCandidati promozione: {len(promote_candidates)}")
        if promote_candidates:
            click.echo("Promuovere manualmente con il path dedicato (IngestPipeline.promote),")
            click.echo("dopo verifica umana — l'audit NON promuove automaticamente (§6.3).")
        click.echo(clf.tracker.format_session_summary())
        return

    if detect_duplicates:
        # Import locale: la pipeline istanzia client Azure, inutile per il
        # solo health check.
        from src.lint import LintPipeline
        click.echo("=== LINT — FASE DETECT (read-only) ===")
        res = LintPipeline().detect_duplicates()
        click.echo(f"Coppie candidate     : {res['candidates']}")
        click.echo(f"Cluster duplicati    : {res['clusters']} (in 'proposals', applicabili)")
        click.echo(f"Relazioni gerarchiche: {res.get('hierarchy_suggestions', 0)} (in 'hierarchy_suggestions', SOLO informative)")
        click.echo(f"Report scritto in    : {res['report']}")
        click.echo("\nRevisiona il report, metti 'approved: true' sui cluster corretti,")
        click.echo("poi: python scripts/lint.py --apply-consolidation")
        return

    if apply_consolidation:
        from src.lint import LintPipeline
        if not CONSOLIDATION_REPORT_PATH.exists():
            raise click.UsageError(
                f"Report assente ({CONSOLIDATION_REPORT_PATH}). Eseguire prima --detect-duplicates."
            )
        click.echo("=== LINT — FASE APPLY ===")
        pipeline = LintPipeline()
        res = pipeline.apply_consolidation()
        click.echo(f"Merge applicati: {res['applied']}")
        if res.get("note"):
            click.echo(res["note"])
        click.echo(pipeline.tracker.format_session_summary())
        return

    _health_check()


if __name__ == "__main__":
    main()
