"""
Scarica pagine da un wiki MediaWiki/Fandom via *export API* → un singolo dump XML.

Costo token: ZERO. Questo script NON chiama l'LLM. Usa l'endpoint standard
`api.php?action=query&export=1` di MediaWiki per ottenere il wikitext delle
pagine nel formato dump `<mediawiki><page>...`, identico a quello che
`scripts/prepare_fandom.py` già sa leggere. Evita di scaricare il dump
completo `.7z` (centinaia di MB) quando per uno scaling test ne bastano poche
centinaia di pagine.

Due fonti di pagine, combinate in un unico dump:
  1. --seed-titles : lista esplicita di titoli (separati da `|`) esportati per
     primi. Serve a garantire che le pagine rilevanti per l'eval (es. "Harry
     Potter", "Lord Voldemort", "Horcrux") siano nel corpus, anche se l'ordine
     alfabetico di allpages non le raggiungerebbe.
  2. generator=allpages : riempie fino a --pages totali scorrendo gli articoli
     (namespace 0) in ordine alfabetico, con paginazione via gapcontinue.

I redirect NON vengono filtrati qui (escono come pagine piccole con
`#REDIRECT [[...]]`): ci pensa prepare_fandom.py a scartarli, insieme a
disambigue e stub sotto soglia parole.

Flusso completo:

    # 1. Scarica le pagine (questo script, zero token):
    python scripts/fetch_fandom_api.py --wiki harrypotter.fandom.com \\
        --domain rowling --pages 400 \\
        --seed-titles "Harry Potter|Lord Voldemort|Horcrux|Albus Dumbledore|Hogwarts"

    # 2. Prepara il corpus (zero token):
    python scripts/prepare_fandom.py --dump data/raw/dumps/rowling.xml \\
        --domain rowling --level L2 --limit 100 --min-words 60

    # 3. Ingest vero (costo LLM) e resto della pipeline:
    python scripts/ingest_folder.py --manifest data/raw/incoming/rowling/manifest.yaml
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR  # noqa: E402

DUMPS_DIR = DATA_DIR / "raw" / "dumps"

_UA = "hybrid-wiki-scaling-test/1.0 (research; contact decastroalessio@gmail.com)"
_XMLNS = "http://www.mediawiki.org/xml/export-0.11/"


# ------------------------------------------------------------------
# Chiamate API
# ------------------------------------------------------------------

def _api_get(api_url: str, params: dict) -> dict:
    """GET su api.php con JSON in risposta. Solleva su errore HTTP."""
    url = api_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _export_titles(api_url: str, titles: list[str]) -> str:
    """Esporta una lista esplicita di titoli; ritorna l'XML del dump (stringa)."""
    data = _api_get(api_url, {
        "action": "query",
        "titles": "|".join(titles),
        "redirects": "1",   # risolve es. "Lord Voldemort" -> pagina reale "Tom Riddle"
        "export": "1",
        "format": "json",
    })
    return data.get("query", {}).get("export", {}).get("*", "")


def _export_allpages(api_url: str, batch: int, gapfrom: str | None) -> tuple[str, str | None]:
    """Esporta un batch di articoli (ns 0) via generator=allpages.

    Ritorna (xml_dump, gapcontinue|None). Il gapcontinue guida la paginazione.
    """
    params = {
        "action": "query",
        "generator": "allpages",
        "gapnamespace": "0",
        "gaplimit": str(batch),
        "export": "1",
        "format": "json",
    }
    if gapfrom:
        params["gapfrom"] = gapfrom
    data = _api_get(api_url, params)
    xml = data.get("query", {}).get("export", {}).get("*", "")
    cont = data.get("continue", {}).get("gapcontinue")
    return xml, cont


# ------------------------------------------------------------------
# Merge dei <page> in un unico dump valido
# ------------------------------------------------------------------

def _extract_page_blocks(batch_xml: str) -> str:
    """Estrae la regione `<page>...</page>` (tutte le pagine) da un dump-batch.

    Ogni risposta export è un `<mediawiki>` completo con `<siteinfo>` + N
    `<page>`. Tenendo solo dal primo `<page>` all'ultimo `</page>` si scartano
    header e siteinfo, lasciando i soli elementi pagina (fratelli) da
    concatenare sotto un unico root.
    """
    start = batch_xml.find("<page>")
    if start == -1:
        return ""
    end = batch_xml.rfind("</page>")
    if end == -1:
        return ""
    return batch_xml[start:end + len("</page>")]


def _count_pages(blob: str) -> int:
    """Conta le pagine accumulate (il wikitext è XML-escaped, niente falsi positivi)."""
    return blob.count("<page>")


# ------------------------------------------------------------------
# Comando principale
# ------------------------------------------------------------------

@click.command()
@click.option("--wiki", default="harrypotter.fandom.com", show_default=True,
              help="Host del wiki Fandom/MediaWiki (senza https://).")
@click.option("--domain", required=True,
              help="Etichetta dominio: determina il nome del file dump in output.")
@click.option("--pages", default=400, type=int, show_default=True,
              help="Pagine grezze totali da scaricare (redirect inclusi). "
                   "Sovrastima: prepare_fandom.py filtra redirect/stub/disambig.")
@click.option("--seed-titles", default="",
              help="Titoli espliciti separati da '|', esportati per primi "
                   "(garantisce le pagine rilevanti per l'eval nel corpus).")
@click.option("--batch", default=50, type=int, show_default=True,
              help="Pagine per chiamata allpages (cap anonimo MediaWiki ~50).")
@click.option("--delay", default=0.3, type=float, show_default=True,
              help="Pausa (s) tra le chiamate API: cortesia verso il server.")
@click.option("--out", default=None, type=click.Path(dir_okay=False),
              help="Path del dump in output (default: data/raw/dumps/<domain>.xml).")
def main(wiki: str, domain: str, pages: int, seed_titles: str,
         batch: int, delay: float, out: str | None) -> None:
    """Wiki MediaWiki/Fandom → singolo dump XML via export API (zero token)."""
    api_url = f"https://{wiki}/api.php"
    out_path = Path(out).resolve() if out else (DUMPS_DIR / f"{domain}.xml")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    click.echo(f"Wiki        : {wiki}")
    click.echo(f"API         : {api_url}")
    click.echo(f"Dominio     : {domain}")
    click.echo(f"Target pagine: {pages} (grezze, redirect inclusi)")
    click.echo(f"Output      : {out_path}")
    click.echo()

    parts: list[str] = []

    # 1. Seed: titoli espliciti rilevanti per l'eval.
    seeds = [t.strip() for t in seed_titles.split("|") if t.strip()]
    if seeds:
        click.echo(f"Seed: esporto {len(seeds)} titoli espliciti ...")
        try:
            xml = _export_titles(api_url, seeds)
            block = _extract_page_blocks(xml)
            if block:
                parts.append(block)
                click.echo(f"  ok: {_count_pages(block)} pagine seed.")
            else:
                click.echo(click.style("  attenzione: nessuna pagina seed trovata.", fg="yellow"))
        except Exception as exc:  # noqa: BLE001
            click.echo(click.style(f"  errore seed export: {exc}", fg="yellow"))
        time.sleep(delay)

    # 2. Fill: generator=allpages con paginazione gapcontinue.
    gapfrom: str | None = None
    n_calls = 0
    while _count_pages("".join(parts)) < pages:
        try:
            xml, gapfrom = _export_allpages(api_url, batch, gapfrom)
        except Exception as exc:  # noqa: BLE001
            click.echo(click.style(f"  errore allpages (stop): {exc}", fg="yellow"))
            break
        block = _extract_page_blocks(xml)
        if block:
            parts.append(block)
        n_calls += 1
        total = _count_pages("".join(parts))
        click.echo(f"  batch {n_calls}: +{_count_pages(block)} pagine  (totale {total})")
        if not gapfrom:
            click.echo("  fine allpages (nessun continue).")
            break
        time.sleep(delay)

    body = "".join(parts)
    total = _count_pages(body)
    if total == 0:
        click.echo(click.style("Nessuna pagina scaricata: controlla --wiki o la rete.", fg="red"))
        sys.exit(1)

    # Dump combinato valido: un solo root con il namespace di default, così
    # prepare_fandom.py (iterparse + _local) lo legge senza modifiche.
    dump = f'<mediawiki xmlns="{_XMLNS}">\n{body}\n</mediawiki>\n'
    out_path.write_text(dump, encoding="utf-8")

    click.echo()
    click.echo("=== DOWNLOAD COMPLETATO (zero token) ===")
    click.echo(click.style(f"Pagine grezze scaricate : {total}", fg="green"))
    click.echo(f"Dump                    : {out_path}")
    click.echo(f"Dimensione              : {out_path.stat().st_size/1024:.0f} KiB")
    click.echo()
    click.echo("Prossimo passo:")
    click.echo(f"  python scripts/prepare_fandom.py --dump {out_path} "
               f"--domain {domain} --level L2 --limit 100 --min-words 60")


if __name__ == "__main__":
    main()
