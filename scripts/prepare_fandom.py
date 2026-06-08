"""
Prepara un dump Fandom/Wikia (o Wikipedia) per l'ingest nel corpus.

Costo token: ZERO. Questo script NON chiama l'LLM: si limita a leggere il
dump XML MediaWiki, ripulire il wikitext a testo piano (src/wikitext.py) e
scrivere un file .txt per pagina + un manifest YAML compatibile con
`scripts/ingest_folder.py`. L'ingest vero (con i costi LLM di L1/L2) avviene
in un secondo passo, riusando l'infrastruttura esistente.

Flusso completo per uno scaling test:

    # 1. Scarica il dump: su un wiki Fandom, fondo di Special:Statistics →
    #    "database download" → file .7z (current pages). Decomprimi a .xml
    #    con 7-Zip (oppure passa il .7z se hai py7zr installato).

    # 2. Prepara il corpus (questo script, zero token):
    python scripts/prepare_fandom.py --dump dump.xml --domain rowling \\
        --level L2 --limit 200 --min-words 60

    # 3. Ingest vero (costo LLM, riusa il tool esistente):
    python scripts/ingest_folder.py --manifest data/raw/incoming/rowling/manifest.yaml

    # 4. (Ri)costruisci il grafo e misura lo scaling A vs B:
    python scripts/build_graph.py --rebuild          # + --llm-relations se vuoi le triple
    python scripts/eval_compare.py --judge

Filtri applicati in fase di preparazione:
- solo pagine dell'articolo principale (namespace 0): niente Talk/User/Template/Category;
- niente redirect (nessun contenuto proprio);
- niente pagine di disambiguazione;
- pagine sotto --min-words parole (stub) scartate;
- pagine sopra --max-words troncate (0 = nessun cap).

Compressione del dump supportata: .xml, .xml.bz2/.bz2, .gz nativamente;
.7z se è installato `py7zr` (altrimenti estrai l'XML a mano).
"""

import bz2
import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import click
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import INCOMING_DIR, VALID_LEVELS
from src.ingest import slugify
from src.wikitext import strip_wikitext, is_redirect, is_disambiguation


# ------------------------------------------------------------------
# Apertura del dump (gestione compressione)
# ------------------------------------------------------------------

def _open_dump(path: Path):
    """Ritorna un file object binario sul contenuto XML del dump.

    Supporta .xml, .bz2, .gz nativamente. Per .7z prova py7zr (opzionale):
    estrae il primo membro .xml in una temp dir e lo apre.
    """
    suffix = path.suffix.lower()
    if suffix == ".bz2":
        return bz2.open(path, "rb")
    if suffix == ".gz":
        return gzip.open(path, "rb")
    if suffix == ".7z":
        try:
            import py7zr
        except ImportError:
            raise SystemExit(
                "Dump .7z: installa py7zr (pip install py7zr) oppure estrai "
                "l'XML con 7-Zip e ripassa il file .xml a --dump."
            )
        import tempfile
        with py7zr.SevenZipFile(path, "r") as z:
            xml_members = [n for n in z.getnames() if n.lower().endswith(".xml")]
            if not xml_members:
                raise SystemExit(f"Nessun .xml dentro l'archivio {path.name}.")
            target = xml_members[0]
            tmpdir = Path(tempfile.mkdtemp(prefix="fandom_dump_"))
            click.echo(f"Estrazione {target} da {path.name} -> {tmpdir} ...")
            z.extract(path=str(tmpdir), targets=[target])
            return open(tmpdir / target, "rb")
    return open(path, "rb")


# ------------------------------------------------------------------
# Parsing streaming del dump MediaWiki
# ------------------------------------------------------------------

def _local(tag: str) -> str:
    """Local-name di un tag XML namespaced ({ns}page → page)."""
    return tag.rsplit("}", 1)[-1]


def _iter_pages(fileobj):
    """Generatore streaming: yield (title, ns, is_redirect_elem, text) per pagina.

    Usa iterparse per non caricare l'intero dump in memoria (i dump grandi
    arrivano a GB). Dopo ogni <page> chiama elem.clear() per liberare RAM.
    """
    for _event, elem in ET.iterparse(fileobj, events=("end",)):
        if _local(elem.tag) != "page":
            continue
        title = ns = None
        redirect_elem = False
        text = ""
        for child in elem:
            ctag = _local(child.tag)
            if ctag == "title":
                title = child.text or ""
            elif ctag == "ns":
                ns = child.text
            elif ctag == "redirect":
                redirect_elem = True
            elif ctag == "revision":
                for rc in child:
                    if _local(rc.tag) == "text":
                        # itertext() concatena testo + tail di eventuali
                        # sotto-elementi: nei dump normali il wikitext è
                        # XML-escaped (nessun figlio) e questo equivale a
                        # rc.text; ma è robusto anche se il <text> contiene
                        # markup non escaped, recuperando il body completo.
                        text = "".join(rc.itertext())
        yield title, ns, redirect_elem, text
        elem.clear()


def _unique_slug(title: str, seen: dict[str, int]) -> str:
    """Slug filesystem-safe univoco; su collisione aggiunge un suffisso _N."""
    base = slugify(title)
    if base not in seen:
        seen[base] = 1
        return base
    seen[base] += 1
    return f"{base}_{seen[base]}"


# ------------------------------------------------------------------
# Comando principale
# ------------------------------------------------------------------

@click.command()
@click.option("--dump", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Path del dump MediaWiki (.xml/.bz2/.gz/.7z).")
@click.option("--domain", required=True,
              help="Etichetta dominio/corpus per le pagine (es. rowling, tolkien).")
@click.option("--level", default="L2", show_default=True,
              type=click.Choice(sorted(VALID_LEVELS)),
              help="Livello di ingest nel manifest. L2 popola le entità → grafo "
                   "(necessario per testare Arch B), ma costa LLM per pagina.")
@click.option("--limit", default=0, type=int,
              help="Max pagine da scrivere (0 = tutte). Usalo per i primi test: "
                   "L2 chiama l'LLM per ogni pagina.")
@click.option("--min-words", default=40, type=int, show_default=True,
              help="Scarta pagine con meno parole (stub/disambigua residue).")
@click.option("--max-words", default=0, type=int,
              help="Tronca pagine più lunghe (0 = nessun cap). Utile per limitare "
                   "il costo embedding su pagine enormi.")
@click.option("--out-dir", default=None, type=click.Path(file_okay=False),
              help="Dir di output (default: data/raw/incoming/<domain>).")
def main(dump: str, domain: str, level: str, limit: int,
         min_words: int, max_words: int, out_dir: str | None) -> None:
    """Dump MediaWiki → cartella di .txt + manifest.yaml (zero token)."""
    dump_path = Path(dump).resolve()
    out = Path(out_dir).resolve() if out_dir else (INCOMING_DIR / domain)
    out.mkdir(parents=True, exist_ok=True)

    click.echo(f"Dump      : {dump_path}")
    click.echo(f"Dominio   : {domain}")
    click.echo(f"Output    : {out}")
    click.echo(f"Livello   : {level}  (manifest defaults)")
    click.echo(f"Filtri    : min_words={min_words}  max_words={max_words or 'nessun cap'}  "
               f"limit={limit or 'tutte'}")
    click.echo()

    seen_slugs: dict[str, int] = {}
    docs: list[dict] = []
    n_pages = n_written = 0
    n_skip_ns = n_skip_redirect = n_skip_disambig = n_skip_short = n_skip_empty = 0

    fileobj = _open_dump(dump_path)
    try:
        for title, ns, redirect_elem, raw in _iter_pages(fileobj):
            n_pages += 1
            if n_pages % 2000 == 0:
                click.echo(f"  ...{n_pages} pagine lette, {n_written} scritte")

            # Solo articoli (ns 0): scarta Talk/User/Template/Category/File.
            if ns is not None and ns != "0":
                n_skip_ns += 1
                continue
            if not title:
                n_skip_empty += 1
                continue
            if redirect_elem or is_redirect(raw):
                n_skip_redirect += 1
                continue
            if is_disambiguation(raw):
                n_skip_disambig += 1
                continue

            plain = strip_wikitext(raw)
            words = plain.split()
            if len(words) < min_words:
                n_skip_short += 1
                continue
            if max_words and len(words) > max_words:
                plain = " ".join(words[:max_words])

            slug = _unique_slug(title, seen_slugs)
            (out / f"{slug}.txt").write_text(plain, encoding="utf-8")
            docs.append({"file": f"{slug}.txt", "title": title})
            n_written += 1
            if limit and n_written >= limit:
                click.echo(f"  raggiunto --limit {limit}, stop.")
                break
    finally:
        fileobj.close()

    if not docs:
        click.echo(click.style("Nessuna pagina scritta: controlla i filtri o il dump.", fg="red"))
        sys.exit(1)

    # Manifest compatibile con ingest_folder.py. base_dir omesso → risolve
    # contro la dir del manifest (dove stanno i .txt).
    manifest = {
        "defaults": {"domain": domain, "level": level},
        "documents": docs,
    }
    manifest_path = out / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )

    click.echo()
    click.echo("=== PREPARAZIONE COMPLETATA (zero token) ===")
    click.echo(f"Pagine lette        : {n_pages}")
    click.echo(f"  scartate ns!=0     : {n_skip_ns}")
    click.echo(f"  scartate redirect : {n_skip_redirect}")
    click.echo(f"  scartate disambig : {n_skip_disambig}")
    click.echo(f"  scartate corte    : {n_skip_short}  (< {min_words} parole)")
    click.echo(f"  titoli vuoti      : {n_skip_empty}")
    click.echo(click.style(f"Pagine scritte      : {n_written}", fg="green"))
    click.echo(f"Manifest            : {manifest_path}")
    click.echo()
    click.echo("Prossimi passi:")
    if level == "L2":
        est = n_written
        click.echo(click.style(
            f"  ATTENZIONE: level=L2 -> ~{est} doc faranno chiamate LLM all'ingest "
            f"(source page + estrazione entità, eventuale consolidamento).",
            fg="yellow",
        ))
    click.echo(f"  python scripts/ingest_folder.py --manifest {manifest_path}")
    click.echo(f"  python scripts/build_graph.py --rebuild")
    click.echo(f"  python scripts/eval_compare.py --judge")


if __name__ == "__main__":
    main()
