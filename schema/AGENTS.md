# AGENTS.md — v0.3

Contratto operativo del companion wiki di lettura.
Sistema **dominio-agnostico**: stesso motore, più corpora indipendenti.

## Scopo

Companion wiki per la lettura di opere lunghe (romanzi, saghe, saggistica).
I documenti raw sono passaggi narrativi, sintesi tematiche, schede di lettura.
Lingua di redazione: **italiano**.

## Domini (corpora)

Ogni documento appartiene a un **dominio** (campo `domain`), una stringa
libera che identifica il corpus di provenienza. Domini attualmente attivi
(estendibili senza modifiche al codice, aggiungendo un manifest):

- `tolkien` — legendarium di J.R.R. Tolkien (LotR, Hobbit, Silmarillion).
- `asimov`  — Ciclo della Fondazione di Isaac Asimov.
- `rowling` — Ciclo di Harry Potter di J.K. Rowling.

**Regola di isolamento**: una pagina wiki appartiene al dominio delle sue
sorgenti. Se un'entità riceve contributi da sorgenti di domini diversi, la
pagina diventa `_mixed`. Non mescolare fatti di domini diversi nella stessa
affermazione: un personaggio di `tolkien` e uno di `asimov` non vanno mai
confusi né accomunati, salvo che la domanda sia esplicitamente comparativa.

## Tipi di entità ammessi

Le pagine `type: entity` usano uno dei seguenti `subtype`:

- `character` — personaggi (Frodo Baggins, Hari Seldon, …)
- `place`     — luoghi, regioni, pianeti (Mordor, Trantor, …)
- `artifact`  — oggetti notevoli (Anello Unico, …)
- `event`     — eventi datati o discreti (Consiglio di Elrond, Crisi Seldon, …)
- `book`      — opere editoriali interne alla finzione (Enciclopedia Galattica, …)

**Limite noto della tassonomia**: questi cinque subtype sono narrative-centrici.
Contenuti come *concetti*, *discipline*, *organizzazioni* (es. "psicostoria",
"la Fondazione") non vi rientrano. Politica per questi casi nel walking
skeleton: creare comunque la pagina `type: entity` con `subtype` vuoto
(`""`) anziché forzare un subtype errato. L'ampliamento della tassonomia
(es. aggiunta di `concept`, `organization`) è un task della fase di Scaling.

## Convenzioni di naming

- `entity_id`: slug **inglese minuscolo con underscore** (`frodo_baggins`,
  `one_ring`, `hari_seldon`, `galactic_empire`). Indipendente dalla lingua
  del contenuto (che resta italiano).
- `doc_id`: slug del titolo + timestamp.
- `source_page_id`: prefisso `source_` + `doc_id`.
- Link interni: sintassi `[[id]]` (sia pagine wiki che doc_id raw).
- Citare **solo** id realmente esistenti tra i risultati di retrieval. Per
  riferirsi a entità non disponibili, usare il nome in chiaro senza `[[ ]]`.

### Riuso e unicità delle entità (anti-frammentazione)

Una entità del mondo = **una sola pagina**. Prima di coniare un nuovo
`entity_id`, verificare l'inventario delle entità esistenti del dominio:
se l'entità esiste già, riusare il suo id **esatto**, anche se nel testo
compare con nome diverso. Casi di duplicazione vietati:

- variante con/senza articolo: `shire` vs `the_shire`
- singolare/plurale: `seldon_crisis` vs `seldon_crises`
- sinonimo o nome in altra lingua: `orodruin` vs `mount_doom` (stessa entità)
- iper-frammentazione: una categoria (es. "Crisi Seldon") va su **una sola
  pagina-categoria**, NON una pagina per istanza, salvo che la singola
  istanza abbia ≥3 frasi di trattazione autonoma.
- unificazione intra-documento: se nello stesso documento un'entità compare
  con nomi diversi, una sola pagina.

L'`entity_id` è **sempre in inglese** (snake_case) anche quando il contenuto
della pagina è in italiano: `primary_radiant`, non `radiante_primario`.

Soglia di entità: una cosa diventa pagina solo se **trattata in modo
sostanziale** (≥2-3 frasi specifiche). Le menzioni di passaggio non sono
entità.

**Inventario parziale a grande scala.** Oltre una soglia di entità
l'inventario mostrato non è l'elenco completo ma uno *scheletro* aggregato
per tipo + una *shortlist* delle entità più affini al documento. Se
un'entità non compare in shortlist ma il suo tipo è presente nello
scheletro, è probabile che esista già: usare un `entity_id` **canonico e
prevedibile** (nome proprio inglese, snake_case, senza articolo né
plurale), mai una variante stilistica. Eventuali duplicati residui sono
gestiti dalla pipeline di consolidazione (lint, §6.3).

## Frontmatter wiki obbligatorio

```yaml
id: <entity_id>
type: entity | source
subtype: character | place | artifact | event | book | ""
domain: <nome_dominio> | _mixed
tags: [...]
sources: [<doc_id>, ...]
last_updated: YYYY-MM-DD
confidence: high | medium | low
stale: false
title: <titolo leggibile>
```

## Criteri L0/L1/L2

- **L0 — indicizzazione minima**: solo embedding nel raw store. Documenti ad
  alto volume e basso valore strategico individuale (avvisi, note di servizio,
  log, comunicazioni operative). Recuperabili solo via ricerca raw.
- **L1 — sintesi singola**: L0 + pagina `type: source` con sezioni
  `## Overview / ## Dettagli / ## Citazioni notevoli`. La source è 1:1 col
  doc, **append-only**, mai mergeable.
- **L2 — integrazione completa**: L1 + identificazione entità e
  creazione/merge di pagine `type: entity`, con esplicitazione delle
  contraddizioni in `## Contraddizioni note`. L'entity aggrega contributi
  da N raw e **viene aggiornata** ad ogni nuovo doc rilevante.

La classificazione è **assistita** (l'LLM propone, l'umano conferma) con un
gate di confidenza asimmetrico: regole deterministiche o L0/L1 ad alta
confidence → ingest automatico; L2 o confidence non alta → coda di review
umana. Il livello può essere comunque dichiarato esplicitamente nel manifest
o via CLI per bypassare il classificatore.

**Confine L0 (decisivo, da valutare per primo nella classificazione)**: un
documento è L0 se di natura amministrativa, logistica, di routine o di
servizio **anche se denso di nomi propri**. Una circolare che cita venti
entità resta L0 — citare ≠ trattare. Si promuove a L1/L2 solo se il
documento *tratta sostanzialmente* (biografia, caratterizzazione, decisione,
evento analizzato) almeno un'entità o un tema.

## Regole di risoluzione conflitti (query time)

| Tipo di claim                                  | Sorgente autoritativa               |
|------------------------------------------------|-------------------------------------|
| Numeri specifici (date, cifre, codici)         | RAW                                 |
| Citazioni testuali                             | RAW                                 |
| Stati attuali (cosa è vero ora)                | RAW se più recente, altrimenti WIKI |
| Sintesi e interpretazioni                      | WIKI                                |
| Relazioni e collegamenti                       | WIKI                                |

Se WIKI e RAW divergono, **non nascondere il conflitto**: esplicitare le due
versioni con citazione e indicare quale prevale. Lo stesso vale per
contraddizioni interne allo stesso dominio (es. due documenti che danno
valori diversi per la stessa quantità).

## Tono e stile

- Italiano, terza persona, tono enciclopedico.
- Niente speculazioni: solo quanto deducibile dalle sorgenti.
- Ogni claim non banale corredato dalla citazione (`[[doc_id]]` o
  `[[entity_id]]`).
- Struttura pagine entità:
  ```
  # <Titolo leggibile>

  ## Panoramica
  ## Dettagli
  ## Relazioni
  ## Domande aperte
  ## Contraddizioni note   (opzionale, solo se presenti)
  ```

### Regola "menzione ≠ trattazione" (lato query)

Il principio §11.1 ("menzione ≠ trattazione sostanziale") vale anche
**lato query**, non solo lato ingest:

- Se un'entità è solo **menzionata di passaggio** in un raw (es. compare
  in una sola frase, senza biografia, caratterizzazione o trattazione
  diretta), NON colmare il vuoto attingendo a conoscenza esterna al
  corpus. Limitati a riportare la menzione effettiva e dichiara il gap.
- Esempio: "Severus Piton ricevette il Marchio Oscuro" è una **menzione**
  del Marchio Oscuro, non una sua trattazione. Una domanda "Cos'è il
  Marchio Oscuro?" deve produrre: (a) la sola menzione presente nel
  corpus con citazione, e (b) un gap esplicito sul resto — NON una
  descrizione tratta dal canone esterno.
- La presenza di una singola occorrenza NON autorizza a parlare
  ampiamente dell'entità: la conoscenza pre-training resta fuori dal
  perimetro fattuale della risposta. Eventuali integrazioni "dal canone"
  vanno esplicitamente segregate (es. "fuori corpus, dal canone:...")
  e non concorrono alle citazioni con `[[id]]`.

Stessa logica per i **gap totali** (entità mai menzionata nel corpus):
rispondere "non determinabile dalle fonti attuali" con confidence=low e
nessuna ricostruzione da conoscenza esterna.

## Citazioni e tracciabilità

- `sources` nei metadati = lista cumulativa dei `doc_id` che hanno
  contribuito alla pagina.
- Le pagine `source_*` hanno esattamente una sorgente.
- In questa fase non è prevista la cancellazione di una sorgente: per
  correggere un errore di ingest si reingesta come nuovo documento.
