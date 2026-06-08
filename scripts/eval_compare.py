"""
Harness di confronto Arch A vs Arch B.

Per ogni domanda nell'eval set:
  1. Esegue con Arch A (QueryPipeline: raw + wiki ChromaDB)
  2. Esegue con Arch B (QueryPipelineGraph: raw + Kuzu graph)
  3. Confronta token consumati e dimensione contesto iniettato
  4. (Opzionale --judge) Chiede all'LLM di valutare le due risposte su
     accuracy, completeness e citation_quality (scala 0-5).

Output:
  - Tabella comparativa a console (per ogni domanda e totale)
  - File JSON in tests/results/eval_compare_TIMESTAMP.json

Uso base (solo token, nessun LLM judge):
    python scripts/eval_compare.py

Con judge LLM (costo aggiuntivo):
    python scripts/eval_compare.py --judge

Su un eval set specifico:
    python scripts/eval_compare.py --eval-set tests/eval_set_arch_compare.yaml

Solo le prime N domande (utile per test rapidi):
    python scripts/eval_compare.py --limit 3
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import click
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import EVAL_RESULTS_DIR, TOKEN_LOG_PATH
from src.embeddings import Embedder
from src.graph_store import GraphStore
from src.llm_client import LLMClient
from src.query import QueryPipeline
from src.query_graph import QueryPipelineGraph
from src.token_tracker import TokenTracker


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _fresh_tracker() -> TokenTracker:
    """TokenTracker in-memory senza persistenza su disco."""
    return TokenTracker(log_path=None)


def _token_summary(tracker: TokenTracker) -> dict:
    """Estrae totali token dalla sessione corrente del tracker."""
    s = tracker.session_summary()
    t = s["totals"]
    by_phase = {row["phase"]: row["total_tokens"] for row in s["by_phase"]}
    return {
        "total": t["total_tokens"],
        "prompt": t["prompt_tokens"],
        "completion": t["completion_tokens"],
        "calls": t["calls"],
        "by_phase": by_phase,
    }


def _context_char_count(pipeline_result: dict, user_msg: str) -> int:
    """Stima la dimensione del contesto iniettato nel prompt (in caratteri)."""
    return len(user_msg)


def _judge_prompt(question: str, answer_a: str, answer_b: str) -> str:
    return (
        "Valuta queste due risposte alla stessa domanda. Usa scala 0-5 per ogni criterio.\n\n"
        f"## Domanda\n{question}\n\n"
        f"## Risposta A (wiki ChromaDB)\n{answer_a}\n\n"
        f"## Risposta B (graph + raw)\n{answer_b}\n\n"
        "Criteri:\n"
        "- accuracy (0-5): la risposta è fatualmente corretta?\n"
        "- completeness (0-5): copre gli aspetti rilevanti della domanda?\n"
        "- citation_quality (0-5): le citazioni sono presenti, pertinenti, non inventate?\n\n"
        "Rispondi SOLO con questo JSON (nessun altro testo):\n"
        '{"A": {"accuracy": N, "completeness": N, "citation_quality": N}, '
        '"B": {"accuracy": N, "completeness": N, "citation_quality": N}, '
        '"winner": "A"|"B"|"tie", '
        '"rationale": "una frase"}'
    )


def _run_judge(llm: LLMClient, question: str, answer_a: str, answer_b: str) -> dict:
    """Chiama l'LLM come giudice. Ritorna dict con scores o errore."""
    prompt = _judge_prompt(question, answer_a, answer_b)
    try:
        raw = llm.complete(system="Sei un valutatore imparziale di sistemi RAG.", user=prompt, max_tokens=400)
        m = __import__("re").search(r"\{.*\}", raw, __import__("re").DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        return {"error": str(e)}
    return {"error": "no JSON in response"}


def _print_row(qid: str, tok_a: int, tok_b: int, judge: dict | None) -> None:
    delta = tok_b - tok_a
    delta_pct = f"{delta / tok_a * 100:+.1f}%" if tok_a else "N/A"
    winner = ""
    if judge and "winner" in judge:
        winner = f" | winner={judge['winner']}"
    click.echo(
        f"  {qid:<6} A={tok_a:>5} tok  B={tok_b:>5} tok  d={delta:>+5} ({delta_pct}){winner}"
    )


# ------------------------------------------------------------------
# Main command
# ------------------------------------------------------------------

@click.command()
@click.option("--eval-set", "eval_set_path",
              default="tests/eval_set_arch_compare.yaml",
              show_default=True,
              help="Path al file YAML con le domande di eval.")
@click.option("--limit", default=0, type=int,
              help="Limita il numero di domande (0 = tutte).")
@click.option("--judge", is_flag=True, default=False,
              help="Attiva LLM-as-judge per valutazione qualitativa (costo extra).")
@click.option("--domain", default=None,
              help="Forza un filtro dominio su tutte le domande (override del campo domain).")
def eval_compare(eval_set_path: str, limit: int, judge: bool, domain: str | None) -> None:
    """Confronta Arch A (wiki ChromaDB) vs Arch B (graph) su token e qualità."""

    # Verifica grafo presente (filesystem check, nessun DB open)
    from src.config import GRAPH_DIR
    kuzu_db_path = GRAPH_DIR / "kuzu_db"
    if not kuzu_db_path.exists():
        click.echo(
            "ERRORE: il grafo non esiste. Esegui prima:\n"
            "  python scripts/build_graph.py",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Grafo: {kuzu_db_path} trovato.")

    # Carica eval set
    eval_path = Path(eval_set_path)
    if not eval_path.is_absolute():
        eval_path = Path(__file__).resolve().parent.parent / eval_set_path
    data = yaml.safe_load(eval_path.read_text(encoding="utf-8"))
    questions = data["questions"]
    if limit > 0:
        questions = questions[:limit]
    click.echo(f"Domande: {len(questions)} da {eval_path.name}\n")

    # Inizializza client condivisi (un solo Embedder, due tracker separati)
    # Il LLM e l'Embedder sono condivisi per non pagare doppio l'init.
    # I tracker sono indipendenti per misurare token per architettura.
    shared_llm = LLMClient(tracker=_fresh_tracker())
    shared_embedder = Embedder(tracker=_fresh_tracker())

    # Inizializza pipeline una sola volta fuori dal loop.
    # QueryPipelineGraph tiene un file lock Kuzu che non può essere
    # ricreato per ogni domanda senza rilasciare e riacquisire il lock.
    tracker_a = _fresh_tracker()
    tracker_b = _fresh_tracker()
    pipe_a = QueryPipeline(
        llm=LLMClient(tracker=tracker_a),
        embedder=Embedder(tracker=tracker_a),
        tracker=tracker_a,
    )
    pipe_b = QueryPipelineGraph(
        llm=LLMClient(tracker=tracker_b),
        embedder=Embedder(tracker=tracker_b),
        tracker=tracker_b,
    )

    results = []
    total_tok_a = 0
    total_tok_b = 0

    click.echo(f"{'ID':<6}  {'Arch A':>8}  {'Arch B':>8}  {'d_tokens':>10}  {'d%':>7}", nl=False)
    if judge:
        click.echo(f"  {'winner':<8}  {'rationale'}", nl=False)
    click.echo()
    click.echo("-" * (60 + (30 if judge else 0)))

    for q in questions:
        qid = q["id"]
        text = q["text"]
        q_domain = domain or q.get("domain")

        # Snapshot indici records prima di ogni domanda per isolare i token
        idx_a_before = len(tracker_a.records)
        idx_b_before = len(tracker_b.records)

        result_a = pipe_a.ask(text, domain=q_domain)
        result_b = pipe_b.ask(text, domain=q_domain)

        # Token per questa domanda = delta records dalla snapshot
        def _sum_records(records):
            t = sum(r["total_tokens"] for r in records)
            p = sum(r["prompt_tokens"] for r in records)
            c = sum(r["completion_tokens"] for r in records)
            phases = {}
            for r in records:
                phases[r["phase"]] = phases.get(r["phase"], 0) + r["total_tokens"]
            return {"total": t, "prompt": p, "completion": c,
                    "calls": len(records), "by_phase": phases}

        tok_a = _sum_records(tracker_a.records[idx_a_before:])
        tok_b = _sum_records(tracker_b.records[idx_b_before:])

        total_tok_a += tok_a["total"]
        total_tok_b += tok_b["total"]

        # --- Judge ---
        judge_result = None
        if judge:
            judge_llm = LLMClient(tracker=_fresh_tracker())
            judge_result = _run_judge(
                judge_llm, text,
                result_a.get("answer", ""),
                result_b.get("answer", ""),
            )

        # Stampa riga
        delta = tok_b["total"] - tok_a["total"]
        delta_pct = f"{delta / tok_a['total'] * 100:+.1f}%" if tok_a["total"] else "N/A"
        row = f"  {qid:<6}  {tok_a['total']:>8}  {tok_b['total']:>8}  {delta:>+10}  {delta_pct:>7}"
        if judge and judge_result:
            w = judge_result.get("winner", "?")
            rat = judge_result.get("rationale", "")[:60]
            row += f"  {w:<8}  {rat}"
        click.echo(row)

        # Accumula risultato
        record = {
            "id": qid,
            "text": text,
            "domain": q_domain,
            "type": q.get("type"),
            "arch_a": {
                "answer": result_a.get("answer", ""),
                "confidence": result_a.get("confidence"),
                "wiki_sources": result_a.get("wiki_sources", []),
                "raw_sources": result_a.get("raw_sources", []),
                "tokens": tok_a,
            },
            "arch_b": {
                "answer": result_b.get("answer", ""),
                "confidence": result_b.get("confidence"),
                "wiki_sources": result_b.get("wiki_sources", []),
                "raw_sources": result_b.get("raw_sources", []),
                "graph_entities": result_b.get("graph_entities", 0),
                "graph_relations": result_b.get("graph_relations", 0),
                "tokens": tok_b,
            },
            "delta_tokens": tok_b["total"] - tok_a["total"],
            "delta_pct": delta_pct,
            "judge": judge_result,
        }
        results.append(record)

    # Chiude il lock Kuzu
    pipe_b.close()

    # Totali
    click.echo("-" * (60 + (30 if judge else 0)))
    total_delta = total_tok_b - total_tok_a
    total_pct = f"{total_delta / total_tok_a * 100:+.1f}%" if total_tok_a else "N/A"
    click.echo(
        f"  {'TOTAL':<6}  {total_tok_a:>8}  {total_tok_b:>8}  {total_delta:>+10}  {total_pct:>7}"
    )

    # Statistiche aggregate
    click.echo("\n=== SOMMARIO ===")
    click.echo(f"Domande eseguite : {len(results)}")
    click.echo(f"Token totali A   : {total_tok_a}")
    click.echo(f"Token totali B   : {total_tok_b}")
    click.echo(f"Saving  (B-A)    : {total_delta:+d} token ({total_pct})")

    if judge:
        wins = {"A": 0, "B": 0, "tie": 0}
        for r in results:
            j = r.get("judge") or {}
            w = j.get("winner")
            if w in wins:
                wins[w] += 1
        click.echo(f"Qualità (judge)  : A vince {wins['A']}x  |  B vince {wins['B']}x  |  tie {wins['tie']}x")

    # Salvataggio risultati
    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = EVAL_RESULTS_DIR / f"eval_compare_{ts}.json"
    out_path.write_text(
        json.dumps({
            "run_at": datetime.now().isoformat(timespec="seconds"),
            "eval_set": str(eval_path.name),
            "n_questions": len(results),
            "judge_enabled": judge,
            "totals": {
                "arch_a_tokens": total_tok_a,
                "arch_b_tokens": total_tok_b,
                "delta_tokens": total_delta,
                "delta_pct": total_pct,
            },
            "questions": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    click.echo(f"\nRisultati salvati in: {out_path}")


if __name__ == "__main__":
    eval_compare()
