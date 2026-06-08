"""
Stripper MediaWiki *wikitext* → testo piano.

I dump Fandom/Wikia (e quelli di Wikipedia) contengono il markup MediaWiki
grezzo: template `{{...}}`, tabelle `{|...|}`, wikilink `[[...]]`, tag `<ref>`,
heading `== ... ==`, ecc. La pipeline di ingest (`src/ingest.py`) lavora su
testo piano: questo modulo fa da adattatore, ripulendo il markup e
preservando la prosa narrativa.

Non è un parser completo di MediaWiki (impresa enorme): è uno stripper
pragmatico calibrato sul testo narrativo dei wiki fandom, dove i costrutti
più ostili (tabelle profondamente annidate, parser function esotiche) sono
rari. Per corpora dove serve fedeltà massima si può sostituire con
`mwparserfromhell`, ma qui la dipendenza zero e la velocità contano di più.

Ordine delle trasformazioni (l'ordine è significativo):
  1. commenti HTML  2. <ref>...</ref>  3. tabelle {|...|}
  4. template {{...}}  5. link a media/categorie  6. wikilink testuali
  7. link esterni  8. tag HTML residui  9. grassetto/corsivo
 10. heading  11. marcatori di lista  12. magic words
 13. unescape entità HTML  14. normalizzazione spazi
"""

from __future__ import annotations

import html
import re

# Namespace di link da RIMUOVERE integralmente (media, categorie, meta).
# Coperti sia gli alias inglesi sia quelli italiani usati dai fandom IT.
_DROP_LINK_NS = (
    "File", "Image", "Media", "Immagine",
    "Category", "Categoria",
    "Template", "Template",
    "Help", "Aiuto",
    "Wikipedia", "Portale", "Portal",
)

_RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_REF_PAIR = re.compile(r"<ref\b[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_RE_REF_SELF = re.compile(r"<ref\b[^>]*/\s*>", re.IGNORECASE)
# Blocchi <gallery>/<timeline>/<imagemap> contengono "pseudo-markup" che
# diventa rumore se ridotto a testo: si rimuovono con tutto il contenuto.
_RE_BLOCK_TAGS = re.compile(
    r"<(gallery|timeline|imagemap|score|syntaxhighlight|source|math)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_TEMPLATE_INNER = re.compile(r"\{\{[^{}]*?\}\}", re.DOTALL)
_RE_TABLE_INNER = re.compile(r"\{\|(?:[^{}]|\{(?!\|)|\}(?!\}))*?\|\}", re.DOTALL)
_RE_DROP_LINK = re.compile(
    r"\[\[\s*(?:" + "|".join(_DROP_LINK_NS) + r")\s*:[^\[\]]*\]\]",
    re.IGNORECASE,
)
_RE_INTERWIKI = re.compile(r"\[\[[a-z]{2,3}\s*:[^\[\]]*\]\]")
_RE_WIKILINK = re.compile(r"\[\[([^\[\]]+?)\]\]")
_RE_EXT_LINK = re.compile(r"\[(?:https?:|ftp:|//)[^\s\]]+(?:\s+([^\]]+))?\]")
_RE_BOLD_ITALIC = re.compile(r"'''''(.+?)'''''", re.DOTALL)
_RE_BOLD = re.compile(r"'''(.+?)'''", re.DOTALL)
_RE_ITALIC = re.compile(r"''(.+?)''", re.DOTALL)
_RE_HEADING = re.compile(r"^[ \t]*(={2,6})[ \t]*(.+?)[ \t]*\1[ \t]*$", re.MULTILINE)
_RE_LIST_MARK = re.compile(r"^[ \t]*[\*#:;]+[ \t]*", re.MULTILINE)
_RE_MAGIC = re.compile(r"__[A-Z]+__")
_RE_MULTINL = re.compile(r"\n{3,}")
_RE_TRAIL_WS = re.compile(r"[ \t]+$", re.MULTILINE)

# Prefissi di redirect (EN + IT). Le pagine redirect non hanno contenuto
# proprio: vanno saltate a monte, non ripulite.
_REDIRECT_PREFIXES = ("#redirect", "#rinvia", "#rinviare")

# Marcatori di pagina disambigua: testo senza valore enciclopedico autonomo.
_RE_DISAMBIG = re.compile(
    r"\{\{\s*(disambig\w*|disambigua\w*|hndis|dab|disamb)\b", re.IGNORECASE
)


def is_redirect(wikitext: str) -> bool:
    """True se il wikitext è una pagina di redirect (nessun contenuto proprio)."""
    if not wikitext:
        return False
    return wikitext.lstrip().lower().startswith(_REDIRECT_PREFIXES)


def is_disambiguation(wikitext: str) -> bool:
    """True se la pagina è marcata come disambigua (testo non enciclopedico)."""
    if not wikitext:
        return False
    return bool(_RE_DISAMBIG.search(wikitext))


def _strip_balanced(text: str, pattern: re.Pattern, max_iter: int = 40) -> str:
    """Rimuove iterativamente le occorrenze più interne di un costrutto annidato.

    `pattern` deve matchare l'occorrenza SENZA annidamenti interni (la più
    profonda). Ripetendo la sostituzione, si "pelano" i livelli dall'interno
    verso l'esterno. `max_iter` evita loop patologici su markup malformato.
    """
    for _ in range(max_iter):
        new = pattern.sub("", text)
        if new == text:
            break
        text = new
    return text


def _resolve_wikilink(match: re.Match) -> str:
    """Risolve `[[target|display]]` → display, `[[target]]` → target.

    Con più pipe (`[[a|b|c]]`, raro) tiene l'ultimo segmento, che nei
    wikilink MediaWiki è il testo mostrato. Scarta eventuali suffissi di
    sezione (`target#sezione`) tenendo solo l'etichetta leggibile.
    """
    inner = match.group(1)
    if "|" in inner:
        return inner.rsplit("|", 1)[-1].strip()
    # Niente pipe: il target è anche il display. Rimuove l'ancora #sezione.
    return inner.split("#", 1)[0].strip()


def strip_wikitext(wikitext: str) -> str:
    """Converte wikitext MediaWiki in testo piano leggibile.

    Idempotente su testo già piano (nessun markup → nessuna modifica).
    Ritorna stringa vuota per input vuoto o redirect.
    """
    if not wikitext:
        return ""

    text = wikitext

    # 1-2. Commenti e citazioni <ref>.
    text = _RE_COMMENT.sub("", text)
    text = _RE_REF_PAIR.sub("", text)
    text = _RE_REF_SELF.sub("", text)
    text = _RE_BLOCK_TAGS.sub("", text)

    # 3-4. Tabelle e template (annidati: rimozione iterativa dall'interno).
    #     Prima i template, perché le tabelle spesso ne contengono; poi le
    #     tabelle, poi di nuovo i template per quelli che le tabelle
    #     "nascondevano".
    text = _strip_balanced(text, _RE_TEMPLATE_INNER)
    text = _strip_balanced(text, _RE_TABLE_INNER)
    text = _strip_balanced(text, _RE_TEMPLATE_INNER)

    # 5. Link a media/categorie/meta e interwiki: via integralmente.
    #    Due passate per gestire didascalie con link annidati.
    for _ in range(2):
        text = _RE_DROP_LINK.sub("", text)
    text = _RE_INTERWIKI.sub("", text)

    # 6. Wikilink testuali → etichetta leggibile (più passate per annidati).
    for _ in range(3):
        new = _RE_WIKILINK.sub(_resolve_wikilink, text)
        if new == text:
            break
        text = new

    # 7. Link esterni → testo dell'etichetta (o nulla se assente).
    text = _RE_EXT_LINK.sub(lambda m: m.group(1) or "", text)

    # 8. Tag HTML residui (<br>, <small>, <sup>, <div>...): rimuovi il tag,
    #    tieni il contenuto inline.
    text = _RE_HTML_TAG.sub("", text)

    # 9. Grassetto/corsivo wiki.
    text = _RE_BOLD_ITALIC.sub(r"\1", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)

    # 10. Heading `== X ==` → `X` su riga propria (mantiene la struttura
    #     a sezioni come segnale per la sintesi LLM).
    text = _RE_HEADING.sub(r"\2", text)

    # 11. Marcatori di lista a inizio riga.
    text = _RE_LIST_MARK.sub("", text)

    # 12. Magic words `__TOC__`, `__NOTOC__`, ...
    text = _RE_MAGIC.sub("", text)

    # 13. Entità HTML (&nbsp;, &amp;, &mdash;, ...).
    text = html.unescape(text)

    # 14. Normalizzazione: niente spazi a fine riga, max una riga vuota.
    text = _RE_TRAIL_WS.sub("", text)
    text = _RE_MULTINL.sub("\n\n", text)
    return text.strip()
