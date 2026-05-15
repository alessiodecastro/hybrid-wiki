# Hybrid Wiki RAG — Walking Skeleton

Versione minima funzionante del sistema descritto in `../hybrid-wiki-rag-design.md`.
Dominio: companion wiki di lettura sull'opera di Tolkien.

## Cosa fa

- **Ingest** di documenti su 3 livelli (L0/L1/L2) con classificazione manuale via CLI.
- **Doppio indice** (raw + wiki) su ChromaDB locale.
- **Query** multi-indice con orientamento dal Hot Layer e risoluzione conflitti.
- **Hot Layer** minimo (overview + index) rigenerato dopo ogni ingest L1/L2.
- **AGENTS.md v0** come contratto operativo letto in ogni chiamata LLM.

## Cosa NON fa ancora (per design)

Access control · multimodal · synthesis pages + dependency graph · lint automatica · UI · ottimizzazione costi.

## Setup

```powershell
cd hybrid-wiki
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
# Editare .env e inserire ANTHROPIC_API_KEY e OPENAI_API_KEY
```

## Uso

### Ingest dei 10 documenti seed

```powershell
python scripts/ingest_doc.py --file data/raw/incoming/frodo_intro.txt        --title "Frodo Baggins — introduzione"   --level L2 --subtype character
python scripts/ingest_doc.py --file data/raw/incoming/gandalf_intro.txt      --title "Gandalf — introduzione"          --level L2 --subtype character
python scripts/ingest_doc.py --file data/raw/incoming/aragorn_intro.txt      --title "Aragorn — introduzione"          --level L2 --subtype character
python scripts/ingest_doc.py --file data/raw/incoming/sam_intro.txt          --title "Samwise Gamgee"                  --level L1
python scripts/ingest_doc.py --file data/raw/incoming/anello_unico.txt       --title "Anello Unico"                    --level L2 --subtype artifact
python scripts/ingest_doc.py --file data/raw/incoming/contea.txt             --title "La Contea"                       --level L2 --subtype place
python scripts/ingest_doc.py --file data/raw/incoming/mordor.txt             --title "Mordor"                          --level L2 --subtype place
python scripts/ingest_doc.py --file data/raw/incoming/monte_fato.txt         --title "Monte Fato"                      --level L1
python scripts/ingest_doc.py --file data/raw/incoming/consiglio_elrond.txt   --title "Consiglio di Elrond"             --level L2 --subtype event
python scripts/ingest_doc.py --file data/raw/incoming/lettera_routine.txt    --title "Nota Mathom-house Halimath 1419" --level L0
```

### Fare una domanda

```powershell
python scripts/ask.py "Chi è il portatore dell'Anello?"
python scripts/ask.py "In che anno fu fondata la Contea?"
python scripts/ask.py "Quali oggetti porta Frodo quando lascia la Contea?"
```

### Eseguire tutto l'eval set

```powershell
python scripts/ask.py --eval tests/eval_set.yaml
```

### Health check manuale

```powershell
python scripts/lint.py
```

## Struttura

```
hybrid-wiki/
├── src/          # moduli core
├── data/
│   ├── raw/      # documenti originali (immutabili)
│   ├── wiki/     # pagine sintetizzate + HOT_LAYER.md
│   └── vectors/  # ChromaDB (creato a runtime)
├── schema/AGENTS.md
├── scripts/      # CLI (ingest_doc, ask, lint)
└── tests/eval_set.yaml
```

## Note di funzionamento

- **Modelli**: `claude-sonnet-4-6` per la generazione, `text-embedding-3-small` per gli embedding.
- **Persistenza**: ChromaDB persistente in `data/vectors/`. Per resettare il sistema basta cancellare la cartella `data/` (escluso `data/raw/incoming/`).
- **Audit trail minimo**: ogni query viene loggata in append a `data/query_log.jsonl`.
- **Contraddizione voluta** nel dataset seed: la data di fondazione della Contea è 1601 in `contea.txt` e 1604 in `consiglio_elrond.txt`. La query relativa (`eval_set.yaml#q09`) verifica che la pipeline esplichi il conflitto invece di nasconderlo.

## Roadmap successiva

Vedi sezione 10 (Roadmap di implementazione) di `../hybrid-wiki-rag-design.md`. Il prossimo passo è la fase di **Scaling**: access control, classificazione assistita, lint automatica, dependency graph.
