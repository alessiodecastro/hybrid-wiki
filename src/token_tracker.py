"""
Telemetria centralizzata del consumo token.

Obiettivo: rendere visibile QUANTO e DOVE viene speso ogni token, sia per
debug (perché questa query è costata 30k token?) sia per stima dei costi
a regime (vedi design 7.8).

Architettura:
- TokenTracker intercetta tutte le chiamate API (LLM chat + embeddings)
  via wrapper sui client. Ogni chiamata produce un record strutturato.
- Il tag della "fase" (es. "ingest:l2:entity_merge", "query:llm") viene
  propagato via contextvars: i wrapper non devono accettare un parametro
  esplicito, e i call site dichiarano la fase con `with tracker.phase(...)`.
- Persistenza: append a data/token_log.jsonl (parallelo al query_log).
- Visualizzazione: in-memory per la sessione corrente (riepilogo a fine
  CLI) + aggregati cumulativi via scripts/tokens.py.

Non incluso nello skeleton:
- Tariffazione (price book per modello/regione). Va aggiunta quando si
  passa a produzione e si stabilisce il contratto con Azure.
- Rate limiting e budget cap. Per ora la responsabilità è del chiamante.
"""

from __future__ import annotations
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Iterator


# Variabile di contesto che memorizza la "fase corrente" per le chiamate
# fatte all'interno di un blocco `with tracker.phase(...)`. ContextVar è
# safe rispetto ad async e thread (a differenza di una variabile globale
# normale), e si annida correttamente: se entriamo in "query" e poi in
# "query:llm", l'uscita dal blocco interno ripristina "query".
_CURRENT_PHASE: ContextVar[str] = ContextVar("current_phase", default="uncategorized")


class TokenTracker:
    """Registro centralizzato di tutte le chiamate API che consumano token.

    Una sola istanza per processo (tipicamente creata dall'IngestPipeline o
    QueryPipeline e passata ai client). I record vengono sia accumulati in
    memoria per il summary di fine sessione, sia appesi al file JSONL.
    """

    def __init__(self, log_path: Path | None = None):
        """Inizializza il tracker.

        Args:
            log_path: file JSONL su cui appendere i record. Se None, il
                tracker resta utilizzabile in-memory ma non persiste nulla
                (utile in test).
        """
        self.log_path = log_path
        # Buffer in-memory: contiene solo i record della sessione corrente
        # (cioè da quando il tracker è stato istanziato). Il log su disco
        # è invece cumulativo tra invocazioni CLI.
        self.records: list[dict] = []

    # ------------------------------------------------------------------
    # API per i client API (LLMClient, Embedder)
    # ------------------------------------------------------------------
    def record(
        self,
        operation: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        extra: dict | None = None,
    ) -> None:
        """Registra una chiamata API.

        Args:
            operation: "chat" oppure "embedding". Stringa libera, ma queste
                due sono le convenzioni usate dai client del progetto.
            model: nome del deployment o modello.
            prompt_tokens: token di input fatturati.
            completion_tokens: token di output fatturati (0 per embeddings).
            cached_tokens: token serviti da cache (Azure può scontarli, e
                comunque utile come metrica di efficienza del prompt).
            extra: dict opzionale di metadati aggiuntivi (es. n_items per
                una embed_batch, char_count, ecc.).
        """
        rec = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "phase": _CURRENT_PHASE.get(),
            "operation": operation,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "cached_tokens": int(cached_tokens or 0),
            "total_tokens": int((prompt_tokens or 0) + (completion_tokens or 0)),
        }
        if extra:
            rec["extra"] = extra
        self.records.append(rec)
        # Persistenza opzionale. Append-only JSONL: una riga per evento,
        # facilmente leggibile da jq/pandas e ricostruibile per analisi.
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # API per i call site (pipeline, scripts)
    # ------------------------------------------------------------------
    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Tag tutte le chiamate emesse dentro il blocco con `name`.

        Convenzione di naming (a colon-separated path):
            ingest                 -> radice
            ingest:l2              -> livello
            ingest:l2:source_page  -> step specifico
            query                  -> radice
            query:embedding        -> embedding della domanda
            query:llm              -> chiamata finale di sintesi

        I report aggregati possono raggruppare per prefisso (es. "ingest:*").
        Usabile annidato: il `with` interno sovrascrive temporaneamente la
        fase, l'uscita ripristina la precedente.
        """
        token = _CURRENT_PHASE.set(name)
        try:
            yield
        finally:
            _CURRENT_PHASE.reset(token)

    def session_summary(self) -> dict:
        """Aggregato dei record della sessione corrente (in-memory).

        Returns:
            Dict con totali globali e breakdown per (phase, operation).
            Pronto per essere stampato in fondo a una CLI.
        """
        by_key: dict[tuple[str, str], dict] = {}
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        for r in self.records:
            key = (r["phase"], r["operation"])
            agg = by_key.setdefault(key, {
                "phase": r["phase"],
                "operation": r["operation"],
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            })
            agg["calls"] += 1
            agg["prompt_tokens"] += r["prompt_tokens"]
            agg["completion_tokens"] += r["completion_tokens"]
            agg["total_tokens"] += r["total_tokens"]
            totals["calls"] += 1
            totals["prompt_tokens"] += r["prompt_tokens"]
            totals["completion_tokens"] += r["completion_tokens"]
            totals["total_tokens"] += r["total_tokens"]
        # Ordinamento per total_tokens DESC: in cima le fasi più costose.
        breakdown = sorted(by_key.values(), key=lambda x: -x["total_tokens"])
        return {"totals": totals, "by_phase": breakdown}

    def format_session_summary(self) -> str:
        """Render testuale del summary, pronto per `click.echo()`."""
        s = self.session_summary()
        if s["totals"]["calls"] == 0:
            return "Token usage: (nessuna chiamata API in questa sessione)"
        lines = ["", "=== TOKEN USAGE (sessione corrente) ==="]
        # Tabella allineata. Colonne: phase, op, calls, prompt, completion, total.
        lines.append(f"{'phase':<35} {'op':<10} {'calls':>5} {'prompt':>8} {'compl.':>8} {'total':>8}")
        lines.append("-" * 80)
        for row in s["by_phase"]:
            lines.append(
                f"{row['phase']:<35} {row['operation']:<10} {row['calls']:>5} "
                f"{row['prompt_tokens']:>8} {row['completion_tokens']:>8} {row['total_tokens']:>8}"
            )
        t = s["totals"]
        lines.append("-" * 80)
        lines.append(
            f"{'TOTAL':<35} {'':<10} {t['calls']:>5} "
            f"{t['prompt_tokens']:>8} {t['completion_tokens']:>8} {t['total_tokens']:>8}"
        )
        return "\n".join(lines)
