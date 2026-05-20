"""
Hot Layer: la "memoria attiva" iniettata in ogni chiamata di query.

Funzione architetturale (sezione 5.3 del design):
fornire all'LLM un orientamento immediato sul dominio senza obbligarlo a
"esplorare a tentoni" il vector DB. Nel walking skeleton il Hot Layer è
composto da:

- Overview: 2-3 paragrafi generati dall'LLM che descrivono lo stato
  corrente della knowledge base.
- Index: lista deterministica (NON generata da LLM) di tutte le pagine
  wiki, raggruppate per type. Necessariamente esaustiva e ordinata.

L'index è deterministico (build a partire dai frontmatter) perché deve
essere completo e veritiero: un index generato da LLM rischierebbe di
inventare pagine o ometterne. L'overview è invece libera: serve a dare
"colore" e contesto, non a fungere da indice navigabile.

Vincolo dimensionale: il Hot Layer deve restare sotto ~5k token. Oltre
questa soglia satura la memoria di lavoro dell'LLM (vedi design 5.3) e
va sostituito con un index gerarchico caricato dinamicamente.
"""

from __future__ import annotations
from datetime import date
from .config import HOT_LAYER_PATH, AGENTS_MD_PATH
from .stores import WikiStore, EntityIndex
from .llm_client import LLMClient


# Soglia oltre la quale stampiamo un warning. Approssimazione conservativa:
# l'index lineare comincia a degradare la qualità del prompt quando occupa
# più del 10-15% di una context window da ~30-40k token utili.
HOT_LAYER_WARN_TOKENS = 5000


def _estimate_tokens(text: str) -> int:
    """Stima euristica del numero di token in un testo.

    Approssimazione 1 token ≈ 4 caratteri, valida per l'inglese e
    ragionevolmente per l'italiano. Sufficiente per il warning.
    """
    return max(1, len(text) // 4)


def _load_agents_md() -> str:
    """Carica il contratto operativo. Stringa vuota se non esiste ancora."""
    if AGENTS_MD_PATH.exists():
        return AGENTS_MD_PATH.read_text(encoding="utf-8")
    return ""


class HotLayer:
    """Gestisce il file HOT_LAYER.md: ricostruzione e caricamento.

    Il file vive in data/wiki/HOT_LAYER.md ed è versionabile via git.
    Viene rigenerato a ogni ingest L1/L2 (operazione che modifica la wiki)
    e letto a ogni query.
    """

    def __init__(self, wiki_store: WikiStore, llm: LLMClient | None = None,
                 entity_index: EntityIndex | None = None):
        """Inizializza il Hot Layer.

        Args:
            wiki_store: sorgente delle pagine wiki da indicizzare.
            llm: opzionale. Se None, l'overview viene sostituita da un
                testo placeholder (utile in test / offline).
            entity_index: opzionale. Se fornito, il Hot Layer riporta il
                conteggio delle entità in stato `aliased` (§13) — non
                materializzate ma comunque parte del corpus. Se assente,
                viene istanziato di default.
        """
        self.wiki_store = wiki_store
        self.llm = llm
        # Lazy: l'EntityIndex viene letto solo al rebuild, non subito.
        self.entity_index = entity_index or EntityIndex()

    def load(self) -> str:
        """Carica il Hot Layer corrente. Fallback su placeholder se non esiste."""
        if not HOT_LAYER_PATH.exists():
            return "# Hot Layer\n\n(vuoto: nessuna pagina wiki ancora)\n"
        return HOT_LAYER_PATH.read_text(encoding="utf-8")

    def _build_index(self) -> tuple[str, list[dict]]:
        """Costruisce l'index deterministico a partire dai frontmatter.

        Returns:
            (markdown_index, entries) dove entries è la lista dei dict usati
            anche per generare l'overview.
        """
        entries = []
        for page_id in self.wiki_store.list():
            fm, body = self.wiki_store.get(page_id)
            # Estrae la prima riga di testo non-header come descrizione breve.
            # Heuristic semplice ma robusta sul nostro formato di pagina.
            first_line = next(
                (l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")),
                "",
            )
            descr = (first_line[:140] + "…") if len(first_line) > 140 else first_line
            entries.append({
                "id": page_id,
                "type": fm.get("type", "?"),
                "subtype": fm.get("subtype", ""),
                "tags": fm.get("tags", []) or [],
                "description": descr,
            })
        # Ordinamento stabile per type poi id: rende l'index leggibile e il
        # diff git informativo (nessuno scrambling tra rebuild successivi).
        entries.sort(key=lambda e: (e["type"], e["id"]))

        # Render markdown: header per tipo, una bullet per pagina con link
        # wikilink-style [[id]] che il modello impara a riconoscere come
        # riferimento navigabile.
        lines = []
        current_type = None
        for e in entries:
            if e["type"] != current_type:
                current_type = e["type"]
                lines.append(f"\n### {current_type}")
            sub = f" ({e['subtype']})" if e["subtype"] else ""
            lines.append(f"- [[{e['id']}]]{sub} — {e['description']}")
        return "\n".join(lines).strip() or "(nessuna pagina)", entries

    def _build_overview(self, entries: list[dict]) -> str:
        """Genera l'overview narrativa via LLM.

        Riceve solo metadata sintetici (no body completo) per contenere il
        costo: bastano id/type/descrizione per scrivere 200 parole di
        contesto generale.
        """
        if not entries or self.llm is None:
            # Fallback deterministico: usato in cold start (wiki vuota) o in
            # test senza LLM disponibile.
            return ("Il sistema contiene pagine wiki di uno o più corpora di lettura. "
                    "L'index sottostante elenca le entità disponibili.")
        summary_input = "\n".join(
            f"- {e['id']} [{e['type']}/{e['subtype']}]: {e['description']}" for e in entries
        )
        system = (
            "Sei l'orchestratore di un companion wiki di lettura multi-corpus. "
            "Scrivi una overview in italiano di 2-3 paragrafi (max 200 parole) che descriva "
            "lo stato corrente della knowledge base: quali corpora/domini sono presenti, "
            "quali entità copre, dove ci sono lacune evidenti. Se sono presenti più "
            "domini, tienili distinti nella descrizione. Tono enciclopedico, terza "
            "persona. Nessun preambolo, solo il testo."
        )
        user = f"Pagine wiki esistenti:\n{summary_input}"
        try:
            return self.llm.complete(system=system, user=user, max_tokens=600)
        except Exception as e:
            # Errore non bloccante: il sistema deve poter funzionare anche
            # se la generazione dell'overview fallisce (es. rate limit).
            # L'index resta corretto e questo è ciò che conta.
            return f"(overview non generata: {e})"

    def rebuild(self) -> str:
        """Ricostruisce il file HOT_LAYER.md da zero.

        Con lazy materialization (§13) l'index include solo le entity
        consolidated (filtro implicito: wiki_store.list() legge dal
        filesystem, le aliased non hanno file). Le aliased vengono
        riportate solo come conteggio aggregato per dare consapevolezza
        dello spazio entità completo all'LLM in query.

        Side effect: scrive su disco. Ritorna anche il contenuto per
        eventuale ispezione/test.
        """
        index_md, entries = self._build_index()
        # Aliased counter (§13): rispecchia entità note ma non
        # materializzate. Il modello in query deve sapere che il corpus
        # è più ampio dell'index visibile.
        aliased_entries = self.entity_index.list_by_state("aliased")
        n_aliased = len(aliased_entries)
        # Breakdown per dominio: utile per orientamento multi-corpus.
        aliased_by_domain: dict[str, int] = {}
        for e in aliased_entries:
            d = e.get("domain") or "_unknown"
            aliased_by_domain[d] = aliased_by_domain.get(d, 0) + 1

        overview = self._build_overview(entries)
        glossary = _load_agents_md()
        # Il glossary completo (AGENTS.md) NON viene incluso nel Hot Layer
        # perché aumenterebbe troppo la dimensione del prompt di ogni query.
        # AGENTS.md viene già passato separatamente come parte del system
        # prompt dalla query pipeline. Qui mettiamo solo un puntatore.
        glossary_excerpt = ""
        if glossary:
            glossary_excerpt = (
                "\n## Riferimento AGENTS\n"
                "Vedi `schema/AGENTS.md` per regole operative e convenzioni di naming.\n"
            )
        # Sezione "Spazio aliased": invisibile nell'index navigabile ma
        # contabilizzata. Sopra una soglia minima è informazione utile.
        aliased_section = ""
        if n_aliased > 0:
            breakdown = ", ".join(
                f"{d}: {c}" for d, c in sorted(aliased_by_domain.items())
            )
            aliased_section = (
                f"\n\n## Entità aliased ({n_aliased} non materializzate)\n"
                f"Ci sono {n_aliased} entità note al corpus ma non ancora "
                f"materializzate come pagina (n_sources < soglia di consolidamento). "
                f"Sono comunque citabili con `[[entity_id]]` e recuperabili via "
                f"retrieval delle loro source page. Distribuzione per dominio: "
                f"{breakdown}.\n"
            )
        content = (
            f"# Hot Layer — Companion Wiki di Lettura\n\n"
            f"_Aggiornato: {date.today().isoformat()}_\n\n"
            f"## Overview\n\n{overview}\n\n"
            f"## Index ({len(entries)} pagine materializzate)\n{index_md}"
            f"{aliased_section}\n"
            f"{glossary_excerpt}"
        )
        HOT_LAYER_PATH.write_text(content, encoding="utf-8")
        # Warning informativo: non blocca, ma segnala che è ora di passare
        # a un index gerarchico.
        if _estimate_tokens(content) > HOT_LAYER_WARN_TOKENS:
            print(
                f"[WARN] Hot Layer stimato in ~{_estimate_tokens(content)} token "
                f"(> {HOT_LAYER_WARN_TOKENS}). Considerare un index gerarchico."
            )
        return content
