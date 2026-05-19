"""
Classificazione L0/L1/L2 assistita (design §6.1).

Principio del design: nei primi mesi la classificazione è **assistita ma
non automatica** — l'LLM propone, un revisore umano conferma. Si costruisce
nel tempo un dataset di esempi che affina i criteri.

Architettura a 3 livelli:

  1. REGOLE DETERMINISTICHE (opzionali, da rules.yaml)
     certi documenti, per sorgente/titolo/dominio, hanno livello fissato
     a regola → nessuna chiamata LLM, confidence alta, rule_applied=True.
  2. PROPOSTA LLM
     criteri da AGENTS.md + few-shot dal dataset esempi (che cresce a
     ogni conferma umana) → {level, confidence, rationale}.
  3. GATE DI CONFIDENZA (applicato dal chiamante, slice 2)
     rule-applied o L0/L1 ad alta confidence → ingest diretto;
     L2 o confidence bassa → coda di review umana. L'asimmetria è
     voluta: sbagliare verso il basso (L2→L0) perde il documento per le
     query concettuali (§6.1), sbagliare verso l'alto è solo spreco.

Questo modulo implementa SOLO la proposta (1+2). Il gate e il workflow di
conferma/ingest sono nello script CLI e nell'integrazione ingest.
"""

from __future__ import annotations
import json
import re
import fnmatch

import yaml
from datetime import datetime

from .config import (
    AGENTS_MD_PATH, VALID_LEVELS,
    CLASSIFICATION_EXAMPLES_PATH, CLASSIFICATION_RULES_PATH,
    CLASSIFICATION_QUEUE_PATH, CLASSIFIER_FEWSHOT_MAX, TOKEN_LOG_PATH,
)
from .llm_client import LLMClient
from .token_tracker import TokenTracker


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Nessun JSON nella risposta:\n{text}")
    return json.loads(m.group(0))


def _clip(text: str, limit: int) -> str:
    """Tronca al confine di parola (no tagli a metà parola come [:300])."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + "…"


class LevelClassifier:
    """Propone un livello L0/L1/L2 per un documento.

    Stateless rispetto al corpus: non legge la wiki né il vector DB.
    Riceve testo + metadati di sorgente e restituisce una proposta
    motivata. La decisione finale resta umana (slice 2).
    """

    def __init__(self, llm: LLMClient | None = None, tracker: TokenTracker | None = None):
        self.tracker = tracker or TokenTracker(log_path=TOKEN_LOG_PATH)
        self.llm = llm or LLMClient(tracker=self.tracker)
        self.agents_md = AGENTS_MD_PATH.read_text(encoding="utf-8") if AGENTS_MD_PATH.exists() else ""
        self.rules = self._load_rules()

    # ------------------------------------------------------------------
    # Livello 1 — regole deterministiche
    # ------------------------------------------------------------------
    def _load_rules(self) -> list[dict]:
        """Carica le regole da rules.yaml se presente.

        Formato atteso:
            rules:
              - match: {source_glob: "*.log", domain: "ops"}
                level: L0
                note: "log di routine"
              - match: {title_regex: "(?i)decisione|contratto"}
                level: L2
        Una regola scatta se TUTTI i criteri in `match` sono soddisfatti.
        """
        if not CLASSIFICATION_RULES_PATH.exists():
            return []
        data = yaml.safe_load(CLASSIFICATION_RULES_PATH.read_text(encoding="utf-8")) or {}
        return data.get("rules") or []

    def _match_rule(self, title: str, source_name: str, domain: str) -> dict | None:
        """Ritorna la prima regola che combacia, o None."""
        for rule in self.rules:
            m = rule.get("match") or {}
            ok = True
            if "source_glob" in m and not fnmatch.fnmatch(source_name or "", m["source_glob"]):
                ok = False
            if ok and "title_regex" in m and not re.search(m["title_regex"], title or ""):
                ok = False
            if ok and "domain" in m and (domain or "") != m["domain"]:
                ok = False
            if ok and rule.get("level") in VALID_LEVELS:
                return rule
        return None

    # ------------------------------------------------------------------
    # Livello 2 — proposta LLM con few-shot
    # ------------------------------------------------------------------
    def _load_examples(self) -> list[dict]:
        """Carica gli esempi few-shot confermati dall'umano (active learning).

        Ogni riga JSONL: {title, level, rationale, domain}. Si iniettano
        al massimo CLASSIFIER_FEWSHOT_MAX esempi (i più recenti).
        """
        if not CLASSIFICATION_EXAMPLES_PATH.exists():
            return []
        out = []
        for line in CLASSIFICATION_EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[-CLASSIFIER_FEWSHOT_MAX:]

    def _llm_propose(self, title: str, body: str, domain: str) -> dict:
        examples = self._load_examples()
        fewshot = ""
        if examples:
            lines = [
                f'- "{e.get("title","")}" [{e.get("domain","")}] -> {e.get("level")}'
                f' ({e.get("rationale","")[:120]})'
                for e in examples
            ]
            fewshot = "\nESEMPI CONFERMATI DA UMANI:\n" + "\n".join(lines) + "\n"
        system = (
            "Sei il classificatore di livello di un companion wiki. Assegna a "
            "un documento uno tra L0, L1, L2 secondo i criteri di AGENTS.md.\n"
            "- L0: alto volume, basso valore individuale; solo indice raw.\n"
            "- L1: merita una sintesi autonoma (pagina source), niente "
            "integrazione con la wiki.\n"
            "- L2: strategico; integra/aggiorna le pagine entità.\n"
            "CONFINE L0 (decisivo, valutalo PER PRIMO): un documento è L0 se "
            "è di natura amministrativa, logistica, di routine o di servizio "
            "(avvisi, note interne, comunicazioni operative, log) ANCHE SE "
            "denso di nomi propri. Citare o nominare di passaggio molte "
            "entità NON rende un documento strategico: conta se il documento "
            "le TRATTA in modo sostanziale (biografia, caratterizzazione, "
            "decisione, evento analizzato). Un elenco di nomi non è L2.\n"
            "REGOLA DI PRUDENZA: sbagliare verso il basso (un L2 marcato L0) "
            "perde il documento per le query concettuali ed è il rischio "
            "grave; nel dubbio tra due livelli, proponi il PIÙ ALTO e usa "
            "confidence 'medium' o 'low'. ATTENZIONE: questa regola si "
            "applica SOLO quando il documento tratta davvero contenuto "
            "sostanziale; NON usarla per promuovere documenti di routine che "
            "si limitano a nominare entità (quelli restano L0).\n"
            "Rispondi SOLO con JSON: "
            '{"level": "L0|L1|L2", "confidence": "high|medium|low", '
            '"rationale": "<1-2 frasi>"}\n'
            f"{fewshot}\n"
            f"AGENTS.md:\n{self.agents_md}"
        )
        # Corpo troncato: per classificare bastano incipit + struttura, non
        # serve l'intero documento (contiene il costo della fase).
        user = f"Documento (titolo: {title}, dominio: {domain}):\n\n{body[:4000]}"
        with self.tracker.phase("classify"):
            raw = self.llm.complete(system=system, user=user, max_tokens=400)
        d = _extract_json(raw)
        level = d.get("level")
        if level not in VALID_LEVELS:
            # Fallback prudente: in caso di output sporco, non degradare a
            # L0. L1 è il compromesso sicuro (almeno una sintesi esiste).
            return {"level": "L1", "confidence": "low",
                    "rationale": f"output classificatore non valido ({level!r}); fallback prudenziale L1"}
        return {
            "level": level,
            "confidence": d.get("confidence", "low"),
            "rationale": _clip((d.get("rationale") or "").strip(), 360),
        }

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------
    def classify(self, title: str, body: str, domain: str, source_name: str = "") -> dict:
        """Propone un livello per il documento.

        Returns:
            {level, confidence, rationale, rule_applied: bool, source}
            dove source ∈ {"rule", "llm"}.
        """
        rule = self._match_rule(title, source_name, domain)
        if rule is not None:
            return {
                "level": rule["level"],
                "confidence": "high",
                "rationale": f"regola deterministica: {rule.get('note', rule.get('match'))}",
                "rule_applied": True,
                "source": "rule",
            }
        prop = self._llm_propose(title, body, domain)
        prop["rule_applied"] = False
        prop["source"] = "llm"
        return prop


# ----------------------------------------------------------------------------
# Helper condivisi tra CLI (classify.py, ingest_folder.py): un solo formato
# di coda ed esempi, per evitare divergenze tra produttori/consumatori.
# ----------------------------------------------------------------------------
def enqueue_review(entry: dict) -> None:
    """Append idempotente alla coda di review (YAML con lista `queue`).

    Dedup su (file, domain): una nuova proposta sostituisce la precedente
    per lo stesso documento (re-classificazione → ultima proposta vince).
    """
    data = {}
    if CLASSIFICATION_QUEUE_PATH.exists():
        data = yaml.safe_load(CLASSIFICATION_QUEUE_PATH.read_text(encoding="utf-8")) or {}
    queue = [
        q for q in (data.get("queue") or [])
        if not (q.get("file") == entry.get("file") and q.get("domain") == entry.get("domain"))
    ]
    queue.append(entry)
    CLASSIFICATION_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFICATION_QUEUE_PATH.write_text(
        yaml.safe_dump({"queue": queue}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_queue() -> list[dict]:
    """Ritorna la lista di entry in coda (vuota se assente)."""
    if not CLASSIFICATION_QUEUE_PATH.exists():
        return []
    data = yaml.safe_load(CLASSIFICATION_QUEUE_PATH.read_text(encoding="utf-8")) or {}
    return data.get("queue") or []


def save_queue(queue: list[dict]) -> None:
    """Riscrive la coda (usato dal --confirm per rimuovere le entry evase)."""
    CLASSIFICATION_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFICATION_QUEUE_PATH.write_text(
        yaml.safe_dump({"queue": queue}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def record_example(title: str, level: str, rationale: str, domain: str) -> None:
    """Appende un esempio confermato dall'umano al dataset few-shot.

    È l'anello di active learning (§6.1 / §7.2): ogni decisione umana
    raffina i criteri delle classificazioni successive.
    """
    CLASSIFICATION_EXAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CLASSIFICATION_EXAMPLES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "title": title, "level": level, "rationale": rationale,
            "domain": domain, "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n")
