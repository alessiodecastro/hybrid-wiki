from __future__ import annotations
from datetime import date
from .config import HOT_LAYER_PATH, AGENTS_MD_PATH
from .stores import WikiStore
from .llm_client import LLMClient


HOT_LAYER_WARN_TOKENS = 5000


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _load_agents_md() -> str:
    if AGENTS_MD_PATH.exists():
        return AGENTS_MD_PATH.read_text(encoding="utf-8")
    return ""


class HotLayer:
    def __init__(self, wiki_store: WikiStore, llm: LLMClient | None = None):
        self.wiki_store = wiki_store
        self.llm = llm

    def load(self) -> str:
        if not HOT_LAYER_PATH.exists():
            return "# Hot Layer\n\n(vuoto: nessuna pagina wiki ancora)\n"
        return HOT_LAYER_PATH.read_text(encoding="utf-8")

    def _build_index(self) -> tuple[str, list[dict]]:
        entries = []
        for page_id in self.wiki_store.list():
            fm, body = self.wiki_store.get(page_id)
            first_line = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), "")
            descr = (first_line[:140] + "…") if len(first_line) > 140 else first_line
            entries.append({
                "id": page_id,
                "type": fm.get("type", "?"),
                "subtype": fm.get("subtype", ""),
                "tags": fm.get("tags", []) or [],
                "description": descr,
            })
        entries.sort(key=lambda e: (e["type"], e["id"]))
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
        if not entries or self.llm is None:
            return ("Il sistema contiene pagine wiki sul dominio Tolkien. "
                    "L'index sottostante elenca le entità disponibili.")
        summary_input = "\n".join(
            f"- {e['id']} [{e['type']}/{e['subtype']}]: {e['description']}" for e in entries
        )
        system = (
            "Sei l'orchestratore di un companion wiki sul mondo di Tolkien. "
            "Scrivi una overview in italiano di 2-3 paragrafi (max 200 parole) che descriva "
            "lo stato corrente della knowledge base: di cosa parla, quali entità copre, "
            "dove ci sono lacune evidenti. Tono enciclopedico, terza persona. "
            "Nessun preambolo, solo il testo."
        )
        user = f"Pagine wiki esistenti:\n{summary_input}"
        try:
            return self.llm.complete(system=system, user=user, max_tokens=600)
        except Exception as e:
            return f"(overview non generata: {e})"

    def rebuild(self) -> str:
        index_md, entries = self._build_index()
        overview = self._build_overview(entries)
        glossary = _load_agents_md()
        glossary_excerpt = ""
        if glossary:
            glossary_excerpt = "\n## Riferimento AGENTS\nVedi `schema/AGENTS.md` per regole operative e convenzioni di naming.\n"
        content = (
            f"# Hot Layer — Companion Wiki Tolkien\n\n"
            f"_Aggiornato: {date.today().isoformat()}_\n\n"
            f"## Overview\n\n{overview}\n\n"
            f"## Index ({len(entries)} pagine)\n{index_md}\n"
            f"{glossary_excerpt}"
        )
        HOT_LAYER_PATH.write_text(content, encoding="utf-8")
        if _estimate_tokens(content) > HOT_LAYER_WARN_TOKENS:
            print(f"[WARN] Hot Layer stimato in ~{_estimate_tokens(content)} token (> {HOT_LAYER_WARN_TOKENS}). Considerare un index gerarchico.")
        return content
