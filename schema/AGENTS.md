# AGENTS.md — v0

Contratto operativo del companion wiki Tolkien (walking skeleton).

## Dominio

Companion wiki di lettura dedicato al **legendarium di J.R.R. Tolkien** (Il Signore degli Anelli, Lo Hobbit, Il Silmarillion, materiale correlato). I documenti raw sono passaggi narrativi, sintesi tematiche, schede di lettura. Lingua di redazione: **italiano**.

## Tipi di entità ammessi

Esattamente uno dei seguenti `subtype` per le pagine `type: entity`:

- `character` — personaggi (es. Frodo Baggins, Gandalf, Aragorn)
- `place`     — luoghi geografici e regioni (es. la Contea, Mordor, Monte Fato)
- `artifact`  — oggetti notevoli (es. Anello Unico, Glamdring)
- `event`     — eventi datati o discreti (es. Consiglio di Elrond, Battaglia dei Campi del Pelennor)
- `book`      — opere editoriali (es. La Compagnia dell'Anello)

Le pagine `type: source` sono sintesi di singoli documenti raw (ponte raw→wiki); il loro `subtype` è vuoto.

## Convenzioni di naming

- `entity_id`: slug in **inglese minuscolo con underscore** (`frodo_baggins`, `one_ring`, `council_of_elrond`).
- `doc_id`: slug del titolo + timestamp (`frodo_baggins_introduzione_20260515103000`).
- `source_page_id`: prefisso `source_` + `doc_id` (`source_frodo_baggins_introduzione_20260515103000`).
- I link interni usano la sintassi `[[id]]` (sia per pagine wiki che per doc_id raw).

## Frontmatter wiki obbligatorio

```yaml
id: <entity_id>
type: entity | source
subtype: character | place | artifact | event | book | ""
tags: [...]
sources: [<doc_id>, ...]
last_updated: YYYY-MM-DD
confidence: high | medium | low
stale: false
title: <titolo leggibile>
```

## Criteri L0/L1/L2

- **L0 — indicizzazione minima**: solo embedding nel raw store. Per documenti ad alto volume e basso valore strategico individuale (note operative, comunicazioni di routine). Recuperabili solo via ricerca raw.
- **L1 — sintesi singola**: L0 + pagina `type: source` nella wiki, generata dall'LLM con sezioni `## Overview / ## Dettagli / ## Citazioni notevoli`. Per documenti che meritano una sintesi autonoma ma non richiedono integrazione con il resto della wiki.
- **L2 — integrazione completa**: L1 + identificazione delle entità rilevanti e aggiornamento/creazione delle pagine `type: entity` corrispondenti, con segnalazione di contraddizioni in una sezione `## Contraddizioni note`. Per documenti strategici che modificano il quadro generale.

La classificazione è **manuale** in questa fase (livello passato come parametro CLI).

## Regole di risoluzione conflitti (query time)

| Tipo di claim                                  | Sorgente autoritativa      |
|------------------------------------------------|----------------------------|
| Numeri specifici (date, cifre, codici)         | RAW                        |
| Citazioni testuali                             | RAW                        |
| Stati attuali (cosa è vero ora)                | RAW se più recente, altrimenti WIKI |
| Sintesi e interpretazioni                      | WIKI                       |
| Relazioni e collegamenti                       | WIKI                       |

Se WIKI e RAW divergono, **non nascondere il conflitto**: esplicitare le due versioni con citazione e indicare quale prevale.

## Tono e stile

- Italiano, terza persona, tono enciclopedico.
- Niente speculazioni: solo quanto deducibile dalle sorgenti.
- Ogni claim non banale deve essere corredato dalla citazione della sorgente (`[[doc_id]]` o `[[entity_id]]`).
- Le pagine entità seguono la struttura:
  ```
  # <Titolo leggibile>

  ## Panoramica
  ## Dettagli
  ## Relazioni
  ## Domande aperte
  ## Contraddizioni note   (opzionale, solo se presenti)
  ```

## Citazioni e tracciabilità

- Ogni pagina wiki conserva nei metadati `sources` la lista dei `doc_id` che hanno contribuito al suo contenuto.
- Le pagine `source_*` hanno esattamente una sorgente: il documento che riassumono.
- L'eliminazione di una sorgente non è prevista in questa fase: in caso di errore di ingest, si reingesta come nuovo documento e si aggiorna la pagina.
