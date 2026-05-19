"""
Ingest in batch di una cartella di documenti, descritta da un manifest YAML.

Pensato per la fase di scaling test: ingestare 30-300 documenti a mano via
ingest_doc.py è inviabile e non riproducibile. Il manifest fa da
"infrastructure as code" del corpus: descrive cosa va dove, con quale
livello, in quale dominio.

Formato del manifest (esempio):

    base_dir: data/raw/incoming/asimov   # opzionale, default = dir del manifest
    defaults:
      domain: asimov
      level: L1
    documents:
      - file: foundation_intro.txt
        title: "Foundation — introduzione"
        level: L2
        subtype: book
      - file: hari_seldon.txt
        title: "Hari Seldon"
        level: L2
        subtype: character
        domain: asimov           # opzionale, eredita da defaults
      - file: trantor.txt
        title: "Trantor"
        level: L2
        subtype: place

Comportamento:
- I path dei file sono risolti contro base_dir (oppure la dir del manifest).
- Defaults applicati a ogni entry priva del campo corrispondente.
- Idempotenza: di default skippa i documenti già ingestati (matching su
  filename sorgente + dominio nel raw store). Forzare con --force.
- Tolleranza errori: se un documento fallisce, lo segnala e prosegue.
- A fine corsa stampa il riepilogo token cumulativo (utile per stimare il
  costo del corpus in toto).
"""

import sys
import traceback
from pathlib import Path

import click
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import IngestPipeline
from src.stores import RawStore
from src.classifier import LevelClassifier, enqueue_review
from src.config import (
    VALID_LEVELS, VALID_SUBTYPES, DEFAULT_DOMAIN, CLASSIFIER_AUTO_CONFIDENCE,
)


def _auto_ingest_ok(res: dict) -> bool:
    """Gate (§6.1): decide se una proposta di livello può essere ingestata
    automaticamente o deve passare per la review umana.

    Asimmetria voluta: un L2 (costoso, alto impatto) o qualunque proposta
    sotto la confidence-soglia va SEMPRE in coda. Solo le regole
    deterministiche e gli L0/L1 ad alta confidence sono auto-ingestati —
    sbagliare verso il basso è il rischio grave, sbagliare verso l'alto
    qui significherebbe solo spreco, che evitiamo accodando.
    """
    if res.get("rule_applied"):
        return True
    return (
        res.get("level") in ("L0", "L1")
        and res.get("confidence") == CLASSIFIER_AUTO_CONFIDENCE
    )


def _existing_sources(raw_store: RawStore) -> set[tuple[str, str]]:
    """Costruisce l'insieme (source_filename, domain) dei doc già nello store.

    Usato per il check di idempotenza: stesso file ingerito due volte
    nello stesso dominio = skip. Permette invece il re-ingest dello stesso
    file in un dominio diverso (caso d'uso lecito, anche se raro).
    """
    out: set[tuple[str, str]] = set()
    for doc_id in raw_store.list():
        fm, _ = raw_store.get(doc_id)
        source = fm.get("source")
        domain = fm.get("domain") or DEFAULT_DOMAIN
        if source:
            out.add((source, domain))
    return out


def _resolve_entry(entry: dict, defaults: dict, base_dir: Path) -> dict:
    """Applica defaults a un entry del manifest e valida i campi obbligatori.

    Ritorna un dict normalizzato pronto per pipeline.ingest().
    """
    merged = {**defaults, **{k: v for k, v in entry.items() if v is not None}}
    # `level` ora OPZIONALE: se assente verrà classificato (assistito).
    required = ("file", "title")
    missing = [k for k in required if not merged.get(k)]
    if missing:
        raise ValueError(f"Manifest entry incompleto: mancano {missing}. Entry={entry!r}")

    level = merged.get("level")  # può essere None → classificazione
    if level is not None and level not in VALID_LEVELS:
        raise ValueError(f"Livello invalido '{level}'. Ammessi: {VALID_LEVELS} (oppure ometterlo per classificare)")

    subtype = merged.get("subtype")
    if subtype and subtype not in VALID_SUBTYPES:
        raise ValueError(f"Subtype invalido '{subtype}'. Ammessi: {VALID_SUBTYPES}")

    file_path = (base_dir / merged["file"]).resolve()
    return {
        "file_path": file_path,
        "title": merged["title"],
        "level": level,  # None = da classificare
        "subtype": subtype,
        "domain": merged.get("domain", DEFAULT_DOMAIN),
        "source_name": Path(merged["file"]).name,
    }


@click.command()
@click.option(
    "--manifest", required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path del file YAML con la lista dei documenti da ingestare.",
)
@click.option(
    "--force/--skip-existing", default=False,
    help="--force re-ingesta anche documenti già presenti. Default: skip.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Stampa il piano di ingest senza chiamare l'API.",
)
def main(manifest: str, force: bool, dry_run: bool):
    """Ingest in batch di un manifest YAML."""
    manifest_path = Path(manifest).resolve()
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}

    defaults = data.get("defaults") or {}
    documents = data.get("documents") or []
    # base_dir relativo al manifest se non assoluto; fallback alla dir
    # del manifest stesso. Convenzione: i path dei file sono relativi
    # a base_dir, NON alla cwd di chi lancia lo script.
    base_dir_raw = data.get("base_dir")
    base_dir = (manifest_path.parent / base_dir_raw).resolve() if base_dir_raw else manifest_path.parent

    click.echo(f"Manifest    : {manifest_path}")
    click.echo(f"Base dir    : {base_dir}")
    click.echo(f"Documenti   : {len(documents)}")
    click.echo(f"Defaults    : {defaults}")
    click.echo(f"Mode        : {'DRY-RUN' if dry_run else ('FORCE' if force else 'SKIP-EXISTING')}")
    click.echo()

    # Risoluzione + validazione di TUTTI gli entry prima di iniziare:
    # meglio fallire subito su un manifest malformato che a metà ingest.
    resolved: list[dict] = []
    for i, entry in enumerate(documents):
        try:
            resolved.append(_resolve_entry(entry, defaults, base_dir))
        except Exception as e:
            click.echo(f"[ERROR] entry #{i}: {e}", err=True)
            sys.exit(1)

    # Idempotenza: leggiamo lo stato del raw store una sola volta.
    pipeline: IngestPipeline | None = None
    existing = set()
    if not dry_run:
        pipeline = IngestPipeline()
        existing = _existing_sources(pipeline.raw)

    ok = skipped = failed = queued = 0
    # Classificatore lazy: creato solo se almeno un entry ha level assente.
    classifier: LevelClassifier | None = None
    # Traccia se almeno un ingest ha rimandato il rebuild dell'Hot Layer:
    # in tal caso va eseguito UNA volta sola alla fine del batch. Eseguirlo
    # per ogni documento è O(pagine_totali) × N → O(N^2) sul corpus.
    needs_hot_layer_rebuild = False
    for entry in resolved:
        lvl_disp = entry["level"] if entry["level"] else "??"
        marker = f"[{lvl_disp:<2}] {entry['domain']:<15} {entry['source_name']}"

        # Check idempotenza.
        key = (entry["source_name"], entry["domain"])
        if not force and key in existing:
            click.echo(f"SKIP  {marker}  (già ingestato)")
            skipped += 1
            continue

        if dry_run:
            plan = entry["level"] or "CLASSIFY"
            click.echo(f"PLAN  [{plan}] {entry['domain']} {entry['source_name']}  title={entry['title']!r}")
            ok += 1
            continue

        # Verifica file esistente (potrebbe essere stato rimosso tra
        # la lettura del manifest e l'ingest).
        if not entry["file_path"].exists():
            click.echo(f"MISS  {marker}  (file non trovato: {entry['file_path']})", err=True)
            failed += 1
            continue

        # Classificazione assistita: level assente → l'LLM propone, poi
        # il gate decide se auto-ingestare o accodare per review umana.
        if entry["level"] is None:
            if classifier is None:
                classifier = LevelClassifier(tracker=pipeline.tracker)
            body = entry["file_path"].read_text(encoding="utf-8").strip()
            res = classifier.classify(
                title=entry["title"], body=body,
                domain=entry["domain"], source_name=entry["source_name"],
            )
            origin = "REGOLA" if res["rule_applied"] else "LLM"
            if _auto_ingest_ok(res):
                entry["level"] = res["level"]
                marker = f"[{res['level']:<2}] {entry['domain']:<15} {entry['source_name']}"
                click.echo(f"CLASS {marker}  -> {res['level']}/{res['confidence']} ({origin}) auto-ingest")
            else:
                enqueue_review({
                    "file": str(entry["file_path"]),
                    "title": entry["title"],
                    "domain": entry["domain"],
                    "proposed_level": res["level"],
                    "confidence": res["confidence"],
                    "rationale": res["rationale"],
                    "source": res["source"],
                    "approved_level": None,
                })
                click.echo(f"QUEUE {marker}  proposto {res['level']}/{res['confidence']} ({origin}) "
                           f"→ review umana (classify.py --review)")
                queued += 1
                continue

        try:
            result = pipeline.ingest(
                str(entry["file_path"]),
                title=entry["title"],
                level=entry["level"],
                subtype=entry["subtype"],
                domain=entry["domain"],
                # Deferral: il rebuild dell'Hot Layer è rimandato a fine
                # batch. Risparmio che cresce quadraticamente col corpus.
                defer_hot_layer=True,
            )
            if result.get("hot_layer_deferred"):
                needs_hot_layer_rebuild = True
            wp = ", ".join(result["wiki_pages"]) if result["wiki_pages"] else "(nessuna)"
            click.echo(f"OK    {marker}  doc_id={result['doc_id']}  wiki=[{wp}]")
            ok += 1
        except Exception as e:
            # Tolleranza errori per non perdere l'intero batch su un
            # singolo file rotto (es. encoding, prompt rejection, ecc.).
            click.echo(f"FAIL  {marker}  -> {e}", err=True)
            if click.get_current_context().obj and click.get_current_context().obj.get("verbose"):
                traceback.print_exc()
            failed += 1

    # Un solo rebuild dell'Hot Layer per l'intero batch, se necessario.
    if pipeline is not None and not dry_run and needs_hot_layer_rebuild:
        click.echo()
        click.echo("Rebuild Hot Layer (una volta, fine batch)...")
        pipeline.rebuild_hot_layer()

    click.echo()
    click.echo(f"=== BATCH SUMMARY ===")
    click.echo(f"OK      : {ok}")
    click.echo(f"SKIPPED : {skipped}")
    click.echo(f"QUEUED  : {queued} (in review_queue.yaml — classify.py --review/--confirm)")
    click.echo(f"FAILED  : {failed}")
    if pipeline is not None and not dry_run:
        click.echo(pipeline.tracker.format_session_summary())


if __name__ == "__main__":
    main()
