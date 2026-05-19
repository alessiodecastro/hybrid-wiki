"""
CLI di classificazione L0/L1/L2 assistita (design §6.1).

Workflow completo (l'LLM propone, l'umano conferma):

  1. PROPOSTA (read-only)
     python scripts/classify.py --file <doc> --title "<t>" --domain <d> [--enqueue]
     Stampa livello/confidence/motivazione. Con --enqueue accoda in
     review_queue.yaml (approved_level: null = in attesa di triage).

  2. REVIEW (read-only)
     python scripts/classify.py --review
     Mostra la coda: per ogni entry, proposta + se già approvata.

  3. CONFERMA + INGEST
     L'umano edita review_queue.yaml impostando `approved_level` su
     ogni entry (L0/L1/L2, oppure 'reject' per scartare; null = lascia
     in coda). Poi:
     python scripts/classify.py --confirm
     Ogni entry approvata viene ingestata al livello scelto, registrata
     come esempio few-shot (active learning), e rimossa dalla coda.

Le proposte non vengono MAI ingestate senza approvazione esplicita.
"""

import sys
from pathlib import Path
from datetime import datetime

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classifier import (
    LevelClassifier, enqueue_review, load_queue, save_queue, record_example,
)
from src.config import DEFAULT_DOMAIN, VALID_LEVELS
from src.ingest import IngestPipeline


def _do_propose(file_path: str, title: str, domain: str, enqueue: bool) -> None:
    path = Path(file_path)
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        raise click.UsageError(f"File vuoto: {path}")

    classifier = LevelClassifier()
    res = classifier.classify(title=title, body=body, domain=domain, source_name=path.name)

    click.echo("=" * 64)
    click.echo(f"File        : {path.name}")
    click.echo(f"Titolo      : {title}   (dominio: {domain})")
    click.echo("-" * 64)
    click.echo(f"Livello     : {res['level']}")
    click.echo(f"Confidence  : {res['confidence']}")
    click.echo(f"Origine     : {'REGOLA' if res['rule_applied'] else 'LLM'}")
    click.echo(f"Motivazione : {res['rationale']}")
    click.echo("=" * 64)

    if enqueue:
        enqueue_review({
            "file": str(path),
            "title": title,
            "domain": domain,
            "proposed_level": res["level"],
            "confidence": res["confidence"],
            "rationale": res["rationale"],
            "source": res["source"],
            "proposed_at": datetime.now().isoformat(timespec="seconds"),
            "approved_level": None,  # l'umano lo imposta prima di --confirm
        })
        click.echo("Accodato per review (imposta approved_level, poi --confirm)")

    click.echo(classifier.tracker.format_session_summary())


def _do_review() -> None:
    queue = load_queue()
    if not queue:
        click.echo("Coda di review vuota.")
        return
    click.echo(f"=== REVIEW QUEUE ({len(queue)} entry) ===")
    for i, q in enumerate(queue):
        state = q.get("approved_level")
        flag = "PENDING" if state in (None, "", "null") else f"-> {state}"
        click.echo(
            f"[{i}] {flag:<10} {q.get('proposed_level')}/{q.get('confidence'):<6} "
            f"{q.get('domain')}  {Path(q.get('file','')).name}\n"
            f"     titolo: {q.get('title')}\n"
            f"     {q.get('rationale','')}"
        )
    click.echo("\nImposta 'approved_level' nelle entry (L0/L1/L2 o 'reject'), poi --confirm.")


def _do_confirm() -> None:
    queue = load_queue()
    if not queue:
        click.echo("Coda vuota: niente da confermare.")
        return

    pipeline = IngestPipeline()
    remaining: list[dict] = []
    ingested = rejected = skipped = 0

    for q in queue:
        approved = q.get("approved_level")
        # Non deciso: resta in coda.
        if approved in (None, "", "null"):
            remaining.append(q)
            skipped += 1
            continue
        # Scartato esplicitamente: esce dalla coda, nessun ingest.
        if str(approved).lower() == "reject":
            click.echo(f"REJECT  {Path(q['file']).name}")
            rejected += 1
            continue
        if approved not in VALID_LEVELS:
            click.echo(f"[WARN] approved_level non valido ({approved!r}) per "
                       f"{Path(q['file']).name}: lasciato in coda.", err=True)
            remaining.append(q)
            skipped += 1
            continue
        # Ingest al livello approvato dall'umano.
        fp = Path(q["file"])
        if not fp.exists():
            click.echo(f"[WARN] file assente: {fp} — rimosso dalla coda.", err=True)
            rejected += 1
            continue
        res = pipeline.ingest(
            str(fp), title=q["title"], level=approved,
            subtype=None, domain=q.get("domain", DEFAULT_DOMAIN),
        )
        # Active learning: la decisione umana diventa esempio few-shot.
        record_example(
            title=q["title"], level=approved,
            rationale=q.get("rationale", "confermato da umano"),
            domain=q.get("domain", DEFAULT_DOMAIN),
        )
        click.echo(f"OK      {fp.name} -> {approved}  doc_id={res['doc_id']}")
        ingested += 1

    save_queue(remaining)
    click.echo(f"\n=== CONFIRM SUMMARY ===")
    click.echo(f"Ingestati : {ingested}")
    click.echo(f"Rifiutati : {rejected}")
    click.echo(f"In attesa : {skipped}" + (" (restano in coda)" if skipped else ""))
    click.echo(pipeline.tracker.format_session_summary())


def _do_promote(doc_id: str, level: str) -> None:
    """Promozione retroattiva human-gated (§6.1).

    L'umano, visti i candidati di `lint --audit-l0`, esegue qui la
    promozione su un doc_id specifico e a un livello esplicito: il
    gate è l'invocazione stessa (nessun automatismo). Riusa
    IngestPipeline.promote: il raw immutabile non viene duplicato,
    si eseguono solo gli step wiki del nuovo livello.
    """
    if level not in VALID_LEVELS:
        raise click.UsageError(f"--level invalido: {level}. Ammessi: {sorted(VALID_LEVELS)}")
    pipeline = IngestPipeline()
    if not pipeline.raw.exists(doc_id):
        raise click.UsageError(f"doc_id non presente nel raw store: {doc_id}")
    fm, _ = pipeline.raw.get(doc_id)
    res = pipeline.promote(doc_id, new_level=level)
    click.echo("=" * 64)
    click.echo(f"Promosso : {doc_id}")
    click.echo(f"Livello  : {res['promoted_from']} -> {res['level']}")
    wp = ", ".join(res["wiki_pages"]) if res["wiki_pages"] else "(nessuna)"
    click.echo(f"Wiki     : {wp}")
    click.echo("=" * 64)
    # La promozione è una decisione di classificazione umana: alimenta
    # l'active learning come una conferma da coda.
    record_example(
        title=fm.get("title", doc_id), level=level,
        rationale=f"promozione retroattiva {res['promoted_from']}->{level}",
        domain=fm.get("domain", DEFAULT_DOMAIN),
    )
    click.echo(pipeline.tracker.format_session_summary())


@click.command()
@click.option("--file", "file_path", default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="Documento da classificare (modalità proposta).")
@click.option("--title", default=None, help="Titolo leggibile (con --file).")
@click.option("--domain", default=DEFAULT_DOMAIN, help=f"Dominio (default: {DEFAULT_DOMAIN}).")
@click.option("--enqueue", is_flag=True, default=False,
              help="Accoda la proposta per conferma umana.")
@click.option("--review", "review", is_flag=True, default=False,
              help="Mostra la coda di review (read-only).")
@click.option("--confirm", "confirm", is_flag=True, default=False,
              help="Ingesta le entry con approved_level impostato; le registra come esempi.")
@click.option("--promote", "promote_doc", default=None,
              help="Promozione retroattiva: doc_id da promuovere (richiede --level).")
@click.option("--level", "promote_level", default=None,
              help="Livello target della promozione (con --promote): L0/L1/L2.")
def main(file_path, title, domain, enqueue, review, confirm, promote_doc, promote_level):
    """Classificazione assistita L0/L1/L2."""
    modes = sum([bool(file_path), review, confirm, bool(promote_doc)])
    if modes != 1:
        raise click.UsageError(
            "Usare esattamente una modalità: --file <doc> | --review | --confirm | --promote <doc_id>."
        )

    if review:
        _do_review()
        return
    if confirm:
        _do_confirm()
        return
    if promote_doc:
        if not promote_level:
            raise click.UsageError("--promote richiede anche --level (L0/L1/L2).")
        _do_promote(promote_doc, promote_level)
        return
    if not title:
        raise click.UsageError("--title è obbligatorio con --file.")
    _do_propose(file_path, title, domain, enqueue)


if __name__ == "__main__":
    main()
