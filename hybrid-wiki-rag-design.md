# Knowledge Base Intelligente
## Proposta architetturale per un sistema ibrido Wiki + RAG

*Documento di riferimento per la progettazione di sistemi di gestione della conoscenza basati su LLM*

---

## Indice

1. [Executive Summary](#executive-summary)
2. [Il problema generale](#1-il-problema-generale)
3. [Gli approcci esistenti e i loro limiti](#2-gli-approcci-esistenti-e-i-loro-limiti)
4. [La proposta architetturale](#3-la-proposta-architetturale)
5. [Architettura complessiva](#4-architettura-complessiva)
6. [I componenti del sistema](#5-i-componenti-del-sistema)
7. [I processi operativi](#6-i-processi-operativi)
8. [Aspetti trasversali critici](#7-aspetti-trasversali-critici)
9. [Decisioni progettuali da concordare con il cliente](#8-decisioni-progettuali-da-concordare-con-il-cliente)
10. [Adattamento a diversi contesti d'uso](#9-adattamento-a-diversi-contesti-duso)
11. [Roadmap di implementazione](#10-roadmap-di-implementazione)
12. [Correzioni architetturali dal Walking Skeleton (pilot)](#11-correzioni-architetturali-dal-walking-skeleton-pilot)
13. [Correzioni dalla fase Scaling](#12-correzioni-dalla-fase-scaling)
14. [Glossario](#glossario)

---

## Executive Summary

Questo documento descrive un'architettura per la costruzione di sistemi di gestione della conoscenza basati su LLM (modelli linguistici come Claude o GPT). L'obiettivo è permettere a un'organizzazione di trasformare una collezione di documenti — meeting, report, articoli, specifiche, conversazioni — in una **base di conoscenza interrogabile, accurata e durevole nel tempo**.

L'architettura proposta combina due approcci esistenti, **RAG** (ricerca su documenti grezzi) e **LLM Wiki** (conoscenza pre-sintetizzata), correggendone i rispettivi limiti attraverso un sistema a **doppio indice**: i documenti originali vengono sempre conservati e indicizzati integralmente (fedeltà totale), mentre parallelamente l'LLM costruisce e mantiene una wiki strutturata (sintesi e collegamenti). I due indici lavorano in cooperazione, non in alternativa.

Il documento copre l'architettura completa, i processi operativi, e — punto critico — tutti gli aspetti trasversali che spesso vengono trascurati nelle proposte di alto livello: controllo degli accessi, valutazione della qualità, gestione delle dipendenze tra contenuti, stima dei costi reali, e manutenzione effettiva.

Il sistema è progettato per essere **adattabile a contesti molto diversi**: dalla ricerca personale alle knowledge base aziendali, dalla gestione di contenuti editoriali ai sistemi di supporto clienti. La sezione 9 fornisce linee guida per la personalizzazione.

---

## 1. Il problema generale

### 1.1 La conoscenza che esiste ma non è accessibile

Qualunque persona o organizzazione che lavori con informazioni produce continuamente documenti: appunti, registrazioni, articoli letti, decisioni prese, conversazioni significative. Con il tempo questa massa cresce a centinaia o migliaia di unità. La conoscenza esiste — ma è frammentata, dispersa, e di fatto inutilizzabile se non per chi ha contribuito a crearla.

I sintomi tipici sono:

- **Domande senza risposta veloce**: "abbiamo già affrontato questo problema in passato?" — la risposta esiste in qualche documento, ma trovarla richiederebbe ore
- **Decisioni dimenticate**: scelte ben motivate vengono riprese da capo perché nessuno ricorda perché si era deciso diversamente
- **Conoscenza che esce con le persone**: quando una persona lascia l'organizzazione (o cambia ruolo) porta via informazioni che nessuno ha mai esplicitato
- **Contraddizioni invisibili**: documenti scritti a distanza di mesi si contraddicono senza che nessuno se ne accorga
- **Lavoro duplicato**: analisi già fatte vengono rifatte perché chi le commissiona non sa che esistono

Il problema non è la mancanza di dati. È la mancanza di un sistema che li organizzi, li colleghi, li mantenga nel tempo, e li renda interrogabili in linguaggio naturale.

### 1.2 Cosa serve veramente

Un sistema utile deve offrire almeno cinque proprietà fondamentali:

```
1. RECUPERABILITÀ
   Trovare informazioni rilevanti in pochi secondi,
   anche se non si ricorda esattamente dove fossero

2. SINTESI
   Combinare informazioni da più documenti per rispondere
   a domande che nessun singolo documento copre

3. FEDELTÀ
   Garantire che nessuna informazione venga persa o
   distorta nel processo di elaborazione

4. EVOLUZIONE
   Aggiornare la conoscenza nel tempo, gestendo
   contraddizioni e cambiamenti di stato

5. CONTROLLO
   Tracciare chi vede cosa, da dove viene ogni
   affermazione, e quanto è affidabile
```

I sistemi esistenti coprono alcune di queste proprietà ma non tutte. Il sistema proposto è progettato per coprirle tutte e cinque.

---

## 2. Gli approcci esistenti e i loro limiti

### 2.1 RAG — Retrieval-Augmented Generation

Il **RAG** è oggi l'approccio più diffuso. In termini semplici: si caricano tutti i documenti in un sistema che li indicizza (li trasforma in vettori numerici che ne catturano il significato). Quando un utente fa una domanda, il sistema:

1. Trasforma la domanda nello stesso tipo di rappresentazione numerica
2. Cerca i frammenti di documento più "vicini" semanticamente
3. Passa quei frammenti a un LLM che costruisce la risposta

**Punti di forza del RAG**:
- Setup semplice e veloce
- Scala bene su grandi quantità di documenti
- Mantiene fedeltà al testo originale
- Costo per documento molto basso

**Limiti fondamentali del RAG**:
```
✗ Nessuna sintesi durevole: ogni domanda riparte da zero
✗ Nessuna comprensione delle relazioni tra documenti
✗ Nessuna gestione delle contraddizioni
✗ I risultati di una domanda complessa
  non si accumulano: la stessa domanda fatta domani
  costa esattamente lo stesso lavoro di oggi
✗ Le risposte sono limitate dalla granularità dei chunk:
  se l'informazione è "spalmata" su molti documenti
  in modo indiretto, il RAG fatica a trovarla
```

### 2.2 LLM Wiki

Un approccio più recente ribalta la prospettiva. Invece di cercare ogni volta nei documenti grezzi, si usa un LLM per **costruire e mantenere una wiki strutturata** — un insieme di pagine collegate tra loro, simile a Wikipedia ma privata e specifica per un dominio.

Quando arriva un nuovo documento, l'LLM:
1. Lo legge integralmente
2. Identifica le informazioni significative
3. Aggiorna le pagine wiki rilevanti
4. Crea nuovi collegamenti
5. Segnala contraddizioni con quanto già scritto

La conoscenza viene **compilata una volta sola** e mantenuta aggiornata nel tempo. Quando arriva una domanda, le sintesi sono già pronte.

**Punti di forza della LLM Wiki**:
- Sintesi pre-elaborate, immediatamente disponibili
- Cross-riferimenti espliciti tra concetti
- Contraddizioni emerse durante l'ingest, non al momento della query
- Migliora con il tempo invece di rimanere costante

**Limiti fondamentali della LLM Wiki**:
```
✗ Costo elevato: l'integrazione di ogni documento
  richiede molte risorse computazionali (token)
✗ Non scala linearmente: a 1000+ documenti la
  manutenzione diventa proibitiva
✗ Rischio di perdita di informazioni: l'LLM fa
  scelte editoriali durante la sintesi, omettendo
  dettagli che potrebbero servire in futuro
✗ Dipendenza totale dalla qualità del modello:
  errori di sintesi si propagano e si accumulano
```

### 2.3 Il problema della perdita di informazioni

Il limite più sottile della LLM Wiki merita attenzione perché è facilmente sottovalutato.

Quando un LLM sintetizza un documento, fa scelte editoriali implicite. Considera ciò che è "rilevante" e omette il resto. Ma "rilevante" rispetto a cosa? Rispetto al contesto del momento. Tre mesi dopo, una query potrebbe aver bisogno proprio di un dettaglio omesso.

> **Esempio illustrativo**: durante una conversazione registrata, qualcuno menziona di sfuggita un nome di un competitor. L'LLM, sintetizzando, classifica la menzione come marginale e la omette. Mesi dopo, una domanda su quel competitor restituisce risultati incompleti — e nessuno saprà mai che la pagina nei documenti originali esisteva.

Questa è una forma di **debito informativo invisibile**. Il sistema sembra funzionare, ma sta silenziosamente perdendo informazioni. Per i contesti dove l'accuratezza è critica (decisioni, audit, ricerca), è un rischio inaccettabile.

---

## 3. La proposta architetturale

### 3.1 L'idea centrale: doppio indice cooperante

La proposta combina i due approcci attraverso un'architettura a **doppio indice**, dove ogni indice ha un ruolo specifico e non sostituibile dall'altro.

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   INDICE WIKI                          INDICE RAW           │
│   ─────────────                        ────────────         │
│                                                             │
│   Pagine sintetizzate                  Documenti originali  │
│   dall'LLM, collegate                  conservati integri,  │
│   tra loro, ricche di                  indicizzati senza    │
│   contesto e relazioni                 alcuna elaborazione  │
│                                                             │
│   Risponde a:                          Risponde a:          │
│   "cosa sappiamo su X?"                "cosa dice           │
│   "qual è la situazione?"              esattamente Y?"      │
│   "come si collega A a B?"             "qual è la frase     │
│                                        precisa in Z?"       │
│                                                             │
│   QUALITÀ DELLA SINTESI                FEDELTÀ TOTALE       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

I due indici lavorano in cooperazione su ogni query: la wiki fornisce la risposta principale, il raw integra dettagli e verifiche quando servono.

### 3.2 Il principio non negoziabile

Tutto il sistema poggia su una regola fondamentale:

> **Qualunque documento che entra nel sistema viene sempre indicizzato integralmente nell'indice raw, prima e indipendentemente da qualsiasi altra elaborazione. Senza eccezioni.**

Questo elimina il rischio di perdita di informazioni. La sintesi nella wiki è qualcosa che si **aggiunge sopra**, mai qualcosa che sostituisce. Anche se la sintesi LLM dovesse fallire, omettere dettagli, o essere fatta male, il documento originale rimane completo e ricercabile.

### 3.3 Il terzo componente: Hot Layer

Oltre ai due indici, c'è un terzo elemento: lo **strato caldo** (Hot Layer). Si tratta di un piccolo insieme di pagine sempre presenti nella "memoria attiva" dell'LLM durante ogni interazione, che contengono le informazioni di orientamento essenziali sul dominio:

- Stato corrente delle cose
- Catalogo navigabile di tutto ciò che esiste nel sistema
- Riferimenti rapidi alle entità principali
- Glossario di termini specifici

È il "contesto di partenza" che permette all'LLM di sapere dove guardare senza dover esplorare a tentoni ogni volta.

### 3.4 Il quarto componente: il file di governo

Un file di configurazione (in genere chiamato `AGENTS.md`) definisce esplicitamente come il sistema deve comportarsi: convenzioni, regole di classificazione, criteri di confidenzialità, workflow standard. Questo file è il **contratto operativo** del sistema — co-evoluto tra organizzazione e LLM nel tempo.

---

## 4. Architettura complessiva

### 4.1 Vista d'insieme

```
                    ┌──────────────────────────────────────────┐
                    │              DOMANDA UTENTE              │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │      IDENTITÀ E PERMESSI UTENTE          │
                    │  (chi sta chiedendo, cosa può vedere)    │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │          HOT LAYER (memoria attiva)      │
                    │  · stato corrente del dominio            │
                    │  · indice navigabile della wiki          │
                    │  · entità principali e glossario         │
                    └────────────────────┬─────────────────────┘
                                         │ orientamento
              ┌──────────────────────────┼─────────────────────────┐
              │                          │                         │
   ricerca concettuale         espansione per             ricerca dettagli
   "cosa sappiamo su X"         collegamenti              "cosa dice esattam.
              │                          │                  il documento Y"
              ▼                          ▼                         ▼
   ┌────────────────────┐    ┌────────────────────┐    ┌─────────────────────┐
   │   INDICE WIKI      │    │  GRAFO DEI         │    │   INDICE RAW        │
   │   pagine già       │    │  COLLEGAMENTI      │    │   documenti         │
   │   sintetizzate     │    │  (relazioni        │    │   originali         │
   │                    │    │   esplicite)       │    │   integrali         │
   └─────────┬──────────┘    └─────────┬──────────┘    └──────────┬──────────┘
             │                         │                          │
             └─────────────────────────┼──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │      FILTRO PERMESSI                    │
                    │  (rimuove contenuti che l'utente        │
                    │   non ha diritto di vedere)             │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │   RISOLUZIONE CONFLITTI                 │
                    │  · raw autoritativo su fatti specifici  │
                    │  · wiki autoritativa su sintesi         │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │   RISPOSTA STRUTTURATA                  │
                    │   testo + citazioni + confidenza +      │
                    │   eventuali gap segnalati               │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │   LOG + FEEDBACK PER EVALUATION         │
                    └─────────────────────────────────────────┘
```

### 4.2 I quattro strati architetturali

Il sistema è organizzato in quattro strati, ognuno con responsabilità ben definite:

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  STRATO 4 — GOVERNO E POLICY                                    │
│  AGENTS.md, regole di classificazione, criteri di accesso,      │
│  convenzioni di scrittura, threat model                         │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  STRATO 3 — CONOSCENZA SINTETIZZATA                             │
│  Wiki (pagine entità, processi, decisioni), Hot Layer,          │
│  Synthesis (analisi derivate), grafo di collegamenti            │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  STRATO 2 — INDICIZZAZIONE                                      │
│  Indice wiki vettoriale, indice raw vettoriale,                 │
│  indici BM25 testuali, metadati strutturati                     │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  STRATO 1 — DOCUMENTI ORIGINALI                                 │
│  Sorgenti immutabili, tag di confidenzialità,                   │
│  metadati di provenienza, versioning                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

Più si scende, più i contenuti sono "puri" e fedeli; più si sale, più sono elaborati e ricchi di contesto. La direzione del flusso è dal basso verso l'alto durante l'ingest, e dall'alto verso il basso durante la query.

---

## 5. I componenti del sistema

### 5.1 Raw Layer — Lo strato dei documenti originali

#### Cosa contiene
Tutti i documenti grezzi che entrano nel sistema, **mai modificati**. Possono essere di formato e natura molto diversi: testo scritto, trascrizioni di conversazioni, articoli, report, conversazioni di chat, email, e — nei contesti più avanzati — anche immagini, tabelle, file strutturati.

#### Come è organizzato
Tipicamente per **origine** o **tipo di sorgente**, non per contenuto. L'organizzazione fisica non è importante perché il recupero avviene attraverso l'indice, non attraverso la struttura delle cartelle.

#### Cosa lo caratterizza
- **Immutabilità**: una volta inserito, un documento non viene mai modificato. Eventuali correzioni vengono fatte aggiungendo nuove versioni
- **Metadati di provenienza**: ogni documento ha tracciate origine, data di acquisizione, autore (se noto), formato
- **Tag di confidenzialità**: ogni documento porta i tag di permesso ereditati dalla sua sorgente
- **Versioning**: se un documento viene aggiornato (per esempio una specifica che evolve), le versioni precedenti vengono conservate

#### Indicizzazione

Il documento viene diviso in **frammenti** (chunk) sovrapposti, ognuno trasformato in un vettore semantico. Il vettore è una rappresentazione numerica del significato, che permette di trovare frammenti simili anche se le parole esatte non corrispondono.

```
Documento originale (10.000 parole)
        │
        ▼
[Frammento 1: parole 1-500]      ──► vettore ──► indice raw
[Frammento 2: parole 400-900]    ──► vettore ──► indice raw
[Frammento 3: parole 800-1300]   ──► vettore ──► indice raw
...
(sovrapposizione: evita di "tagliare" frasi importanti)
```

Ogni frammento mantiene un riferimento al documento originale, alla posizione, e ai tag di permesso ereditati.

### 5.2 Wiki Layer — Lo strato della conoscenza sintetizzata

#### Cosa contiene
Pagine generate dall'LLM a partire dai documenti raw. Ogni pagina ha uno scopo preciso e copre un'**entità**, un **concetto**, una **decisione**, o un **processo** rilevante per il dominio.

#### Tipi di pagine

Quattro categorie principali, adattabili al contesto:

```
ENTITÀ
   pagine dedicate a "cose" identificabili e durevoli
   esempi: persone, organizzazioni, prodotti, luoghi,
           progetti, opere, eventi specifici

CONCETTI E TOPIC
   pagine dedicate a temi, idee, processi, categorie
   esempi: metodi, teorie, procedure, classi di problemi,
           aree tematiche

SORGENTI
   pagine di sintesi che riassumono singoli documenti raw
   funzione: ponte tra raw e wiki, audit trail della sintesi

SYNTHESIS
   pagine derivate da query o analisi specifiche
   esempi: confronti, panoramiche tematiche, analisi
           comparative, risposte significative
```

#### Anatomia di una pagina wiki

Ogni pagina ha tre componenti:

```
┌─────────────────────────────────────────────────────┐
│ METADATI (frontmatter)                              │
│  · identificatore univoco                           │
│  · tipo e sottotipo                                 │
│  · tag tematici                                     │
│  · permessi (ereditati dalle sorgenti)              │
│  · indicatori di confidenza e freschezza            │
│  · contatori (numero sorgenti, link entranti)       │
│  · flag (contraddizioni note, da rivedere)          │
├─────────────────────────────────────────────────────┤
│ CONTENUTO                                           │
│  · sintesi narrativa, generalmente strutturata      │
│  · sezioni tipiche: panoramica, dettagli,           │
│    decisioni, domande aperte                        │
│  · collegamenti espliciti ad altre pagine           │
│  · citazioni alle sorgenti (sources/)               │
├─────────────────────────────────────────────────────┤
│ TRACCIABILITÀ                                       │
│  · elenco delle sorgenti che hanno contribuito      │
│  · storia delle modifiche significative             │
│  · indicazione di pagine derivate (synthesis)       │
└─────────────────────────────────────────────────────┘
```

#### Il grafo dei collegamenti

I link tra pagine non sono ornamentali — sono dati strutturati. Costituiscono un **grafo navigabile** dove i nodi sono le pagine e gli archi sono i collegamenti semantici. Questo grafo viene mantenuto esplicitamente dal sistema e usato sia durante la query (per espandere il contesto) sia durante la manutenzione (per identificare pagine orfane o nodi centrali).

```
                  ┌──────────────┐
                  │  PROGETTO A  │
                  └──┬────────┬──┘
                     │        │
              ┌──────▼──┐   ┌─▼──────────┐
              │ OWNER 1 │   │ DECISIONE X│
              └────┬────┘   └─┬──────────┘
                   │          │
              ┌────▼──────────▼─┐
              │  PROCESSO P     │
              └─────────────────┘
```

### 5.3 Hot Layer — La memoria attiva

#### Funzione
Il Hot Layer è quello che l'LLM ha "sotto gli occhi" all'inizio di ogni interazione, senza bisogno di cercare. Serve per orientarsi velocemente.

#### Composizione tipica

```
overview         ─►  panoramica dello stato attuale
                     (2-3 paragrafi, aggiornata di frequente)

index            ─►  catalogo navigabile della wiki
                     (lista pagine con descrizione di una riga)

entità chiave    ─►  riferimenti rapidi alle "cose" più
                     citate nel dominio

glossario        ─►  termini specifici, acronimi, gergo
                     del dominio
```

#### Il vincolo dimensionale

Il Hot Layer deve restare sotto una soglia di token (tipicamente 3.000-5.000) per non saturare la memoria di lavoro dell'LLM. **Questo è un vincolo architetturale serio**: oltre una certa scala di sistema, l'index lineare non basta più e va sostituito con un index gerarchico (categorie → sottocategorie → pagine), caricato dinamicamente in base alla query.

### 5.4 Indici di ricerca

I due indici (raw e wiki) sono implementati come database vettoriali, ma il principio è lo stesso:

```
Testo qualsiasi
      │
      ▼
[Modello di embedding]
      │
      ▼
Vettore numerico (es. 1536 dimensioni)
      │
      ▼
Indice vettoriale (database specializzato)
      │
      ▼
Ricerca per similarità ("trova testi simili a Q")
```

**Indice wiki**: contiene i vettori delle pagine wiki sintetizzate. Cattura il significato di alta densità — concetti, relazioni, sintesi.

**Indice raw**: contiene i vettori dei frammenti dei documenti originali. Cattura il significato di alta fedeltà — frasi precise, dettagli, citazioni testuali.

A questi può essere affiancato un **indice testuale tradizionale** (BM25) per ricerche per parola chiave esatta, complementare alla ricerca semantica.

---

## 6. I processi operativi

### 6.1 Ingest — L'ingresso di un nuovo documento

#### Vista d'insieme del flusso

```
┌─────────────────────┐
│ NUOVO DOCUMENTO     │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────┐
│ 1. CLASSIFICAZIONE PERMESSI     │
│ assegna tag di confidenzialità  │
│ e dominio                       │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│ 2. INDICIZZAZIONE RAW           │
│ ◄── SEMPRE, qualunque sia il    │
│     livello successivo          │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│ 3. CLASSIFICAZIONE LIVELLO      │
│ L0 / L1 / L2                    │
│ (LLM + validazione manuale      │
│  nelle prime settimane)         │
└──────────┬──────────────────────┘
           │
    ┌──────┴──────┬──────────────┐
    │ L0          │ L1           │ L2
    │             │              │
    │             ▼              ▼
    │   ┌─────────────────┐  ┌─────────────────┐
    │   │ Sintesi LLM     │  │ Sintesi LLM     │
    │   │ → wiki/sources/ │  │ → wiki/sources/ │
    │   │ → indice wiki   │  │ → indice wiki   │
    │   └─────────────────┘  └────────┬────────┘
    │                                  │
    │                                  ▼
    │                        ┌─────────────────────┐
    │                        │ Diff semantico      │
    │                        │ + espansione grafo  │
    │                        │ identifica pagine   │
    │                        │ wiki da aggiornare  │
    │                        └────────┬────────────┘
    │                                  │
    │                                  ▼
    │                        ┌─────────────────────┐
    │                        │ Aggiornamento       │
    │                        │ pagine wiki         │
    │                        │ + flag contraddiz.  │
    │                        └────────┬────────────┘
    │                                  │
    │                                  ▼
    │                        ┌─────────────────────┐
    │                        │ Aggiornamento       │
    │                        │ dependency graph    │
    │                        │ (synthesis stale)   │
    │                        └────────┬────────────┘
    │                                  │
    │                                  ▼
    │                        ┌─────────────────────┐
    │                        │ Aggiornamento       │
    │                        │ Hot Layer se serve  │
    │                        └────────┬────────────┘
    │                                  │
    └──────────┬───────────────────────┘
               │
               ▼
     ┌─────────────────────┐
     │ LOG + AUDIT TRAIL   │
     └─────────────────────┘
```

#### I tre livelli di elaborazione

Non tutti i documenti meritano lo stesso sforzo. Il sistema riconosce tre livelli:

```
LIVELLO 0 — Indicizzazione minima
  Cosa fa:       Solo indice raw, nessuna sintesi LLM
  Quando:        Documenti ad alto volume, basso valore
                 strategico individuale, ma utili in aggregato
  Costo:         Molto basso (solo embedding)
  Recuperabile:  Sì, ma solo via ricerca raw
  Esempi:        Comunicazioni di routine, log, notifiche,
                 contenuti ad alta frequenza

LIVELLO 1 — Sintesi singola
  Cosa fa:       Indice raw + pagina di sintesi nella wiki
  Quando:        Documenti che meritano una sintesi
                 autonoma ma non richiedono integrazione
                 con il resto della wiki
  Costo:         Moderato
  Recuperabile:  Sì, sia via wiki che via raw
  Esempi:        Articoli di riferimento, report periodici,
                 documenti tematici autonomi

LIVELLO 2 — Integrazione completa
  Cosa fa:       L1 + aggiornamento delle pagine wiki
                 correlate + gestione contraddizioni
  Quando:        Documenti strategici, decisioni importanti,
                 contenuti che cambiano il quadro generale
  Costo:         Alto
  Recuperabile:  Sì, con cross-riferimenti automatici
  Esempi:        Decisioni, contratti, specifiche di
                 riferimento, eventi significativi
```

#### Il problema della classificazione

La classificazione del livello è **uno dei punti più delicati** del sistema. Sbagliare verso il basso (un L2 classificato come L0) significa perdere quel documento per le query concettuali. Sbagliare verso l'alto è semplicemente spreco di risorse.

Per gestirlo:

1. **Classificazione assistita ma non automatica nei primi mesi**: l'LLM propone, un revisore umano conferma. Si costruisce un dataset di esempi per affinare i criteri.
2. **Regole esplicite quando possibile**: certi tipi di documenti (per natura, sorgente, autore) possono essere classificati a regola.
3. **Promozione retroattiva**: il sistema deve permettere di "promuovere" un documento da L0 a L2 quando ci si accorge che meritava più attenzione. Questo richiede di poter recuperare il documento originale (sempre possibile, grazie al raw layer) e rieseguire la sintesi.
4. **Audit periodico**: una percentuale di classificazioni L0 viene rivista periodicamente per verificare che non si stia perdendo materiale importante.

### 6.2 Query — La risposta a una domanda

#### Vista d'insieme

```
┌──────────────────────┐
│ DOMANDA UTENTE       │
└──────────┬───────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 1. IDENTITÀ E PERMESSI             │
│ determina cosa l'utente può vedere │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 2. ORIENTAMENTO (Hot Layer)        │
│ identifica pagine wiki rilevanti   │
│ per nome dall'indice navigabile    │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 3. RECUPERO MULTI-INDICE           │
│ ┌────────┐ ┌──────┐ ┌────────┐    │
│ │  wiki  │ │grafo │ │  raw   │    │
│ │vettori.│ │ hop  │ │vettor. │    │
│ └────────┘ └──────┘ └────────┘    │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 4. FILTRO PERMESSI                 │
│ rimuove contenuti non accessibili  │
│ dall'utente che ha fatto la query  │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 5. RISOLUZIONE CONFLITTI           │
│ se wiki e raw discordano,          │
│ applica regole esplicite           │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 6. SINTESI RISPOSTA                │
│ risposta + citazioni + confidence  │
│ + segnalazione gap informativi     │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ 7. LOG QUERY + FEEDBACK            │
│ alimenta il sistema di valutazione │
└────────────────────────────────────┘
```

#### Regole di risoluzione dei conflitti

Quando wiki e raw forniscono informazioni discordanti, serve una regola esplicita. Senza di essa, l'LLM tenderà a fidarsi della wiki (è in contesto, è pre-elaborata, sembra più "autoritativa") — esattamente il pattern che il dual index doveva evitare.

```
REGOLA PER TIPO DI CLAIM

  Numeri specifici (date, cifre, codici)
  ────────────────────────────────────►  RAW autoritativo
  perché la sintesi LLM può aver introdotto errori

  Citazioni testuali
  ────────────────────────────────────►  RAW autoritativo
  per definizione, solo il raw ha il testo esatto

  Stati attuali (cosa è vero ora)
  ────────────────────────────────────►  RAW se più recente
                                          WIKI se la sintesi
                                          ha aggregato più fonti
  
  Sintesi e interpretazioni
  ────────────────────────────────────►  WIKI autoritativa
  per definizione, è il suo scopo

  Relazioni e collegamenti
  ────────────────────────────────────►  WIKI autoritativa
  (il raw non rappresenta esplicitamente relazioni)
```

> **Correzione pilot (v2.1).** La tabella sopra disambigua solo i conflitti
> WIKI-vs-RAW. Il pilot ha mostrato che esiste un terzo caso non coperto —
> **due fonti RAW dello stesso dominio che divergono sullo stesso fatto** —
> in cui il modello, applicando "RAW autoritativo", sceglie arbitrariamente
> una delle due. Regola integrativa: vedi §11.3.

#### Quando si attiva l'indice raw

Il raw layer **non è un fallback di emergenza**. È un componente di prima classe che si attiva sistematicamente in determinate condizioni:

```
✓ La domanda contiene riferimenti specifici
  ("cosa ha detto X nel meeting del 3 marzo")

✓ La risposta dalla wiki ha confidence inferiore
  a una soglia (es. 0.7)

✓ La domanda riguarda dettagli che la sintesi
  potrebbe aver omesso (gergo, citazioni esatte)

✓ Si richiede verifica esplicita di un'affermazione

✓ La pagina wiki rilevante è marcata come "stale"
  (non aggiornata di recente)

✓ La query è classificata come "ad alta accuratezza"
  (compliance, audit, decisioni critiche)
```

### 6.3 Lint — Manutenzione della qualità

A intervalli regolari il sistema esegue una passata di controllo qualità, eseguita dall'LLM con supervisione umana:

```
┌────────────────────────────────────────────┐
│ LINT PIPELINE                              │
├────────────────────────────────────────────┤
│                                            │
│ 1. CONTRADDIZIONI                          │
│    cerca claim numerici/fattuali che       │
│    si contraddicono tra pagine             │
│    output: contradiction_report            │
│                                            │
│ 2. STALENESS                               │
│    identifica pagine non aggiornate da     │
│    tempo ma con sorgenti raw recenti       │
│    output: lista re-ingest                 │
│                                            │
│ 3. ORFANI                                  │
│    pagine senza link entranti              │
│    output: orphan_report                   │
│                                            │
│ 4. GAP                                     │
│    entità menzionate nei raw ma senza      │
│    pagina wiki dedicata                    │
│    output: gap_report                      │
│                                            │
│ 5. SYNTHESIS STALE                         │
│    pagine synthesis le cui sorgenti        │
│    sono state aggiornate                   │
│    output: lista da rigenerare             │
│                                            │
│ 6. HOT LAYER HEALTH                        │
│    verifica completezza e dimensione       │
│                                            │
│ 7. PROMOZIONE L0→L2                        │
│    audit di un campione di documenti L0    │
│    per verificare classificazione corretta │
│                                            │
└────────────────────────────────────────────┘
```

L'output del lint **non è automatico**: produce report che richiedono triage. Questa è una componente del **costo di manutenzione effettiva** del sistema, che va dimensionata realisticamente (vedi sezione 7.8).

### 6.4 Sincronizzazione e consistenza

In un sistema con due indici e contenuti che vengono aggiornati, la **consistenza** è un problema reale.

#### Modelli possibili

```
A) BATCH PERIODICO (es. nightly)
   I documenti accumulati vengono processati in batch
   notturni. Tutto ciò che entra durante il giorno è
   disponibile via raw immediatamente, via wiki dal giorno
   successivo.
   
   ✓ Più economico
   ✓ Più consistente (sincronizzazione atomica)
   ✗ Latenza alta nella wiki

B) NEAR REAL-TIME
   Ogni documento viene processato all'arrivo.
   ✓ Latenza bassa
   ✗ Più costoso
   ✗ Rischio di stato intermedio (raw aggiornato, wiki no)

C) IBRIDO PER LIVELLO
   L0 in real-time, L1 in batch orario, L2 in batch
   notturno con review manuale prima della pubblicazione.
   ✓ Bilancia costo e freschezza
   ✗ Complessità di implementazione maggiore
```

La scelta dipende dal contesto. È critico **dichiararla esplicitamente all'utente**: una query alle 14:00 su un documento arrivato alle 13:55 deve restituire una risposta o un avviso "in elaborazione"? Senza chiarezza, gli utenti perdono fiducia nel sistema.

---

## 7. Aspetti trasversali critici

Questa sezione tratta gli aspetti che attraversano tutta l'architettura e che spesso vengono trascurati nelle proposte di alto livello. Sono i punti che fanno la differenza tra un progetto pilota e un sistema affidabile in produzione.

### 7.1 Controllo degli accessi

#### Il problema

In quasi ogni contesto reale, non tutti gli utenti possono vedere tutto. Possono esistere documenti riservati, pagine con visibilità limitata, contenuti per ruolo specifico. Un sistema di knowledge base che ignora questo è **inutilizzabile in produzione** per qualsiasi contesto con dati sensibili.

#### La sfida specifica dei sistemi a doppio indice

Una sintesi LLM mescola informazioni provenienti da documenti con permessi diversi. Una pagina wiki "Progetto Alpha" può aggregare contenuti da fonti tecniche pubbliche e valutazioni economiche riservate. Senza accortezza, l'output svuota i permessi di partenza.

#### Il modello proposto

Il controllo permessi opera a **due livelli**, con propagazione automatica:

```
LIVELLO 1 — DOCUMENTO RAW
   Ogni documento porta tag espliciti:
   {
     visibility: [pubblico, interno, riservato, ...]
     domains: [tecnico, commerciale, hr, ...]
     custom_tags: [...]
   }
   I tag possono essere assegnati manualmente o
   dedotti dalla sorgente (es. tutti i documenti
   di "HR/" ereditano visibility:hr)

LIVELLO 2 — PAGINA WIKI
   Una pagina wiki eredita l'UNIONE dei tag di
   tutte le sorgenti che hanno contribuito al suo
   contenuto. Se almeno una sorgente è riservata,
   la pagina è riservata.
   
   Se questo crea problemi (la pagina diventa
   accessibile a troppo poche persone), si possono
   creare PAGINE PARALLELE: una "pubblica" con solo
   le sorgenti pubbliche, una "completa" con tutto.

LIVELLO 3 — QUERY TIME
   Quando un utente fa una domanda:
   1. Si determina il suo set di permessi
   2. Il retrieval esclude tutto ciò che l'utente
      non può vedere PRIMA della sintesi
   3. La risposta cita solo sorgenti accessibili
   4. Se ci sono pagine rilevanti non accessibili,
      l'utente può vedere che esistono ma non il
      contenuto (o nemmeno questo, se la mera
      esistenza è confidenziale)
```

#### Visualizzazione del flusso

```
DOCUMENTO RAW                  UTENTE Q
tags: {hr, riservato}          perms: {tecnico, pubblico}
     │                              │
     │ embedding                    │ identità verificata
     ▼                              ▼
INDICE RAW                     QUERY
(chunk con tag ereditati)           │
     │                              │
     └──────────────┬───────────────┘
                    │
                    ▼
            ┌───────────────┐
            │ RETRIEVAL     │
            └───────┬───────┘
                    │
                    ▼
            ┌───────────────┐
            │ FILTRO PERMS  │
            │ esclude chunk │  ← scarta i contenuti hr/riservato
            │ non accessib. │     perché l'utente non li può vedere
            └───────┬───────┘
                    │
                    ▼
            ┌───────────────┐
            │ LLM SYNTHESIS │
            │ usa solo ciò  │
            │ che è passato │
            └───────────────┘
```

#### Punti di attenzione

- **Audit log**: ogni query e ogni filtro vanno loggati per audit
- **Coerenza con il sistema esistente**: idealmente il sistema dei permessi si integra con quello già usato dall'organizzazione (LDAP, SSO, ecc.)
- **Granularità**: il livello di granularità (chunk, pagina, sezione) ha implicazioni di performance e complessità
- **Test sistematici**: il sistema deve essere testato regolarmente con utenti diversi per verificare che le restrizioni funzionino

### 7.2 Framework di valutazione (Evaluation)

#### Il problema

Senza misurare la qualità delle risposte, non si sa se il sistema sta funzionando. Le metriche di processo (quanti documenti, quante query) non dicono nulla su **se le risposte sono giuste**.

#### Componenti dell'eval framework

```
┌──────────────────────────────────────────────────┐
│ 1. EVAL SET                                      │
│    50-200 coppie (domanda, risposta verificata)  │
│    rappresentative dei casi d'uso reali          │
│    costruito manualmente all'inizio              │
│    arricchito nel tempo con casi reali           │
├──────────────────────────────────────────────────┤
│ 2. METRICHE                                      │
│    · accuracy: la risposta è corretta?           │
│    · completeness: copre tutti gli aspetti?      │
│    · sources: cita le sorgenti giuste?           │
│    · confidence calibration: quando dice         │
│      "sono sicuro", lo è davvero?                │
│    · permission compliance: rispetta i           │
│      permessi dell'utente di test?               │
├──────────────────────────────────────────────────┤
│ 3. SCORING                                       │
│    automatico per dati strutturati (numeri,      │
│    date, nomi); LLM-as-judge per sintesi;        │
│    human review per casi complessi               │
├──────────────────────────────────────────────────┤
│ 4. REGRESSION TESTING                            │
│    ogni modifica al sistema viene testata        │
│    contro l'eval set per evitare regressioni     │
├──────────────────────────────────────────────────┤
│ 5. ANALISI DEI FALLIMENTI                        │
│    casi in cui il sistema sbaglia vengono        │
│    analizzati per identificare cause             │
│    (pagina mancante, classificazione errata,     │
│    sintesi imprecisa, permessi sbagliati)        │
└──────────────────────────────────────────────────┘
```

#### Ciclo di valutazione

```
┌─────────────┐
│ EVAL SET    │
└──────┬──────┘
       │ run periodicamente
       ▼
┌─────────────┐         ┌──────────────┐
│  SISTEMA    │ ──────► │  RISPOSTE    │
└─────────────┘         └──────┬───────┘
                               │
                               ▼
                        ┌──────────────┐
                        │  SCORING     │
                        └──────┬───────┘
                               │
                               ▼
                        ┌──────────────┐
                        │  METRICHE    │
                        │ + ERRORI     │
                        └──────┬───────┘
                               │
                               ▼
                        ┌──────────────┐
                        │  ANALISI     │
                        │  CAUSE       │
                        └──────┬───────┘
                               │
              ┌────────────────┼───────────────────┐
              ▼                ▼                   ▼
        ┌──────────┐    ┌──────────────┐  ┌─────────────────┐
        │ Aggiorna │    │ Re-ingest    │  │ Crea pagine     │
        │ AGENTS.md│    │ documenti    │  │ wiki mancanti   │
        └──────────┘    └──────────────┘  └─────────────────┘
```

#### Regola importante

> **Costruire l'eval set PRIMA del sistema, non dopo.**
>
> Le domande devono nascere dai bisogni reali del contesto, non essere generate retroattivamente per validare ciò che il sistema già fa.

### 7.3 Cold start — Avviare un sistema da zero

#### Il problema

Un sistema vuoto è asimmetricamente più difficile di un sistema pieno. Senza pagine wiki preesistenti, la regola "il diff semantico identifica le pagine wiki da aggiornare" non funziona. Senza Hot Layer, l'LLM non sa cosa esiste nel dominio.

#### Approccio in tre fasi

```
FASE 1 — SEEDING MANUALE (settimane 1-2)
  Scelta di 20-50 documenti tra i più rappresentativi
  del dominio. Tutti processati come L2 con supervisione
  umana stretta. Si costruisce così:
  
  · Un primo set di pagine entità e topic
  · Una prima versione del Hot Layer
  · Un primo set di regole in AGENTS.md
  · Un primo eval set di 20-30 domande chiave
  
  In questa fase si scoprono molte cose: convenzioni
  da fissare, classificazioni da chiarire, edge case.
  L'output principale di questa fase è la conoscenza
  operativa, non la wiki in sé.

FASE 2 — INGEST GUIDATO (settimane 3-6)
  Si processa il backlog storico con classificazione
  automatica + revisione manuale. Si testa il sistema
  su query reali. Si itera sulle regole.
  
  In questa fase emergono:
  · Pattern di classificazione (cosa è L0 vs L2)
  · Categorie di pagine non previste
  · Problemi di consistenza
  · Lacune nell'AGENTS.md

FASE 3 — REGIME (dalla settimana 7 in poi)
  Il sistema entra in funzionamento ordinario.
  Ingest automatico con audit periodico.
  Manutenzione settimanale.
  Eval continuo.
```

#### Cosa NON fare in cold start

```
✗ Processare 1000 documenti automaticamente al primo giorno
  (le decisioni di classificazione e le convenzioni non sono
  ancora stabili — si crea debito tecnico difficile da pulire)

✗ Costruire l'eval set dopo il sistema
  (il sistema influenza le aspettative; le domande dell'eval
  set devono essere "neutre" rispetto al sistema)

✗ Saltare la fase di seeding manuale
  (è dove si impara cosa serve davvero)
```

### 7.4 Dependency graph e propagazione degli aggiornamenti

#### Il problema

Le pagine **synthesis** sono costruite a partire da altre pagine wiki. Quando una pagina di base viene aggiornata, le pagine synthesis che la usano diventano potenzialmente obsolete — ma il sistema non lo sa.

```
SCENARIO PROBLEMATICO

  Tempo T1:
    raw_doc_A → wiki_page_X
    raw_doc_B → wiki_page_Y
    
    L'utente chiede una sintesi che combina X e Y:
    synthesis_S1 viene creata e salvata
  
  Tempo T2 (un mese dopo):
    raw_doc_C → aggiorna wiki_page_X
    (informazione importante cambia)
  
  synthesis_S1 è ora basata su informazioni obsolete.
  Ma il sistema non lo sa, e la prossima query simile
  potrebbe ricevere S1 come risposta pronta.
```

#### Soluzione: dependency graph esplicito

Ogni pagina derivata mantiene un riferimento esplicito alle pagine "genitore". Quando una pagina genitore cambia, le derivate vengono marcate come "potenzialmente obsolete":

```
DEPENDENCY TRACKING

   wiki_page_X  ◄─────┐
                       │
                       │ depends_on
                       │
                synthesis_S1
                       │
                       │ depends_on
                       │
   wiki_page_Y  ◄─────┘

   QUANDO wiki_page_X viene aggiornata:
   → tutte le pagine che hanno X in depends_on
     vengono marcate stale: true
   → durante la query, le synthesis stale ricevono
     un warning o vengono ricostruite
   → il lint settimanale propone la rigenerazione
```

### 7.5 Versioning e audit trail

#### Perché serve

In contesti dove le decisioni hanno conseguenze (legali, di compliance, di reputazione), è essenziale poter rispondere a domande come:

- "Cosa diceva la nostra wiki su X il giorno Y?"
- "Quando è stata aggiornata l'ultima volta l'informazione Z?"
- "Da quale sorgente specifica viene questa affermazione?"
- "Chi (umano o sistema) ha modificato questa pagina, e quando?"

#### Implementazione

Tre meccanismi complementari:

```
1. VERSIONING DELLE PAGINE
   La wiki è in un sistema di versioning (es. git).
   Ogni modifica produce un commit datato e attribuito.
   È possibile vedere la storia di ogni pagina.

2. AUDIT TRAIL DELLE OPERAZIONI
   Un log strutturato registra:
   · ogni ingest (documento, livello, pagine toccate)
   · ogni query (domanda, sorgenti usate, risposta)
   · ogni modifica manuale
   · ogni lint pass e i suoi risultati

3. TRACCIABILITÀ DELLE AFFERMAZIONI
   Ogni claim in una pagina wiki ha un riferimento
   esplicito alla sorgente raw da cui deriva.
   In caso di dubbio, è sempre possibile risalire
   al documento originale.
```

### 7.6 Multimodal handling

#### Il problema

I documenti reali raramente sono solo testo. Contengono tabelle, immagini, schemi, grafici, file allegati. Un sistema che assume solo testo perde informazioni significative.

#### Approccio modulare

Il sistema può essere esteso con processori specializzati per contenuti non testuali:

```
DOCUMENTO COMPOSITO (es. report con tabelle e grafici)
        │
        ▼
┌───────────────────────────┐
│ ESTRATTORE MULTIMODALE    │
├───────────────────────────┤
│ · testo  → text pipeline  │
│ · tabelle → tabular ingest│
│ · immagini → vision LLM   │
│           → caption + tags│
│ · allegati → ricorsivo    │
└──────────┬────────────────┘
           │
           ▼
   contenuti normalizzati,
   ognuno con tag e metadati
```

L'approccio è incrementale: si parte con il solo testo, e si aggiungono i moduli necessari quando emergono nei casi d'uso.

### 7.7 Privacy e threat model

#### Domande da porre per ogni progetto

Prima di implementare, vanno definiti esplicitamente:

```
COSA È SENSIBILE?
  · dati personali (PII)
  · informazioni commerciali
  · proprietà intellettuale
  · informazioni regolamentate

CHI È L'AVVERSARIO?
  · accesso non autorizzato esterno
  · accesso non autorizzato interno
  · esfiltrazione tramite query LLM
  · fornitori dei modelli AI

CHE GARANZIE SI VOGLIONO?
  · i dati possono uscire dall'organizzazione?
  · i modelli AI usati possono essere addestrati
    sui nostri contenuti?
  · è accettabile il rischio di prompt injection?
```

#### Opzioni architetturali per livello di sensibilità

```
LIVELLO 1 — STANDARD
  LLM e embedding via API cloud (OpenAI, Anthropic, ...)
  ✓ Massima qualità, costi prevedibili
  ✗ Dati transitano da fornitori esterni
  Adatto: contenuti non sensibili

LIVELLO 2 — IBRIDO
  Embedding locali, LLM cloud solo per L2
  Redaction automatica prima dell'invio
  ✓ Riduce esposizione, mantiene qualità per casi critici
  ✗ Più complesso, richiede definizione PII

LIVELLO 3 — ON-PREMISE COMPLETO
  Tutti i modelli (embedding + LLM) eseguiti su
  infrastruttura controllata dall'organizzazione
  ✓ Massimo controllo
  ✗ Costo hardware significativo, qualità potenzialmente
     inferiore con modelli open source
  Adatto: contenuti altamente regolamentati
```

### 7.8 Costi reali — stime realistiche

#### Il problema delle stime ottimistiche

Le proposte iniziali tendono a sottostimare i costi computazionali. Una stima realistica include **tutti** i token usati, non solo quelli del passaggio principale.

#### Componenti di costo di un ingest L2

```
INGEST DI UN DOCUMENTO L2
─────────────────────────────────────────────────
Lettura del documento originale       20-50k token
Lettura delle pagine wiki correlate   10-20k token
Generazione sintesi                    3-8k token
Generazione pagine aggiornate         10-30k token
Aggiornamento Hot Layer                2-5k token
Validazione e linting                  2-5k token
─────────────────────────────────────────────────
TOTALE                                50-120k token
```

#### Componenti di costo di una query

```
QUERY COMPLESSA
─────────────────────────────────────────────────
Hot Layer (sempre)                     4k token
Pagine wiki recuperate                10-20k token
Frammenti raw                          2-8k token
Conversation history                   1-10k token
Risposta + reasoning                   2-5k token
─────────────────────────────────────────────────
TOTALE PER QUERY                      20-50k token
```

#### Modello di stima per cliente

Per una stima sensata bisogna combinare:

```
VOLUME ATTESO
  · documenti/mese e loro distribuzione L0/L1/L2
  · query/giorno e loro complessità media
  · lint pass (settimanali, mensili)

PARAMETRI ECONOMICI
  · prezzo per token del modello scelto
  · prezzo embedding
  · costi infrastruttura (storage, vector DB, ...)

OVERHEAD
  · primi 3-6 mesi: +30-50% per cold start, errori,
    re-ingest dovuti a regole che cambiano
  · audit e revisione manuale (vedi sezione 7.9)
```

> **Linea guida**: stimare almeno 3 scenari (basso, medio, alto) e validare con un pilot reale prima di proiettare a regime.

> **Dati pilot (v2.1).** Il pilot ha fornito i primi numeri reali e ha
> identificato il **driver di costo superlineare**: non l'ingest in sé ma la
> **proliferazione di entità duplicate**, che amplifica `entity_merge` e gli
> embedding di re-indicizzazione. Correggendola, il costo dello stesso corpus
> è sceso del ~31%. Dettaglio quantitativo e leva di ottimizzazione in §11.8.

### 7.9 Manutenzione realistica

#### La promessa pericolosa

L'approccio LLM Wiki nella sua formulazione originale promette manutenzione "quasi a costo zero". Questa è una semplificazione. La realtà è che la manutenzione effettiva richiede tempo umano strutturato.

#### Cosa richiede manutenzione umana

```
ATTIVITÀ                          FREQUENZA      TEMPO STIMATO
                                                 (per 1000 doc)

Revisione classificazioni L0/L1/L2 settimanale   2-4 ore
Triage report di contraddizioni    settimanale   1-2 ore
Triage gap report e orfani         mensile       2-3 ore
Aggiornamento AGENTS.md            ad hoc        variabile
Revisione promozioni L0→L2         mensile       2-4 ore
Refresh synthesis stale            mensile       2-4 ore
Eval review                        bisettimanale 2-4 ore
Audit accessi e permessi           mensile       1-2 ore
                                                 ─────────────
                                   TOTALE        ~30-50 h/mese
```

Questo è un costo strutturale che va incluso nella proposta al cliente. **Pretendere che il sistema sia autonomo significa creare aspettative irrealistiche e accumulare debito tecnico**.

#### Ruolo di "knowledge curator"

In progetti significativi è opportuno identificare uno o più **curatori della conoscenza**: persone (non necessariamente full time) responsabili del coordinamento del sistema. Non scrivono la wiki — l'LLM lo fa — ma:

- Definiscono le convenzioni
- Fanno triage dei report
- Approvano classificazioni dubbie
- Estendono e mantengono AGENTS.md
- Sono il punto di contatto per gli utenti

### 7.10 Backup, disaster recovery, portabilità

Spesso trascurato, ma critico:

```
BACKUP
  · documenti raw: backup giornaliero, retention lunga
  · wiki: backup continuo (è in versioning git)
  · indici vettoriali: ricostruibili dai contenuti,
    backup periodico per velocità di recovery
  · log e audit: append-only, backup giornaliero

PORTABILITÀ
  · i contenuti (raw e wiki) sono in formati aperti
    (testo, markdown) — nessun lock-in
  · gli indici vettoriali sono ricostruibili
  · l'unico componente vincolato al fornitore è
    eventualmente il modello LLM scelto

DISASTER RECOVERY
  · ricostruzione completa dai documenti raw possibile
  · tempo stimato: proporzionale al volume L2
  · va testato periodicamente (es. ogni 6 mesi)
```

---

## 8. Decisioni progettuali da concordare con il cliente

Ogni implementazione richiede decisioni esplicite su una serie di punti che variano per contesto. Questa checklist serve come base di discussione iniziale.

### 8.1 Decisioni di dominio

```
□ DEFINIZIONE DEL DOMINIO
  Cosa fa parte del sistema, cosa no?
  Confini chiari evitano scope creep.

□ TIPOLOGIE DI ENTITÀ RILEVANTI
  Quali "cose" meritano una pagina dedicata?
  Persone? Progetti? Prodotti? Clienti? Argomenti?

□ TIPOLOGIE DI SORGENTI
  Da dove vengono i documenti?
  Formato? Frequenza? Volume atteso?

□ GLOSSARIO INIZIALE
  Quali termini sono specifici del dominio e
  rischiano di essere fraintesi dall'LLM?
```

### 8.2 Decisioni di processo

```
□ CRITERI DI CLASSIFICAZIONE L0/L1/L2
  Quali documenti rientrano in quale livello?
  Si possono definire regole automatiche?

□ MODELLO DI SINCRONIZZAZIONE
  Batch notturno? Near real-time? Ibrido?

□ FREQUENZA DI LINT
  Settimanale? Mensile? On-demand?

□ WORKFLOW DI REVIEW
  Chi approva le classificazioni dubbie?
  Chi gestisce le contraddizioni?
```

### 8.3 Decisioni di accesso

```
□ MODELLO DI PERMESSI
  Per ruolo? Per dipartimento? Per progetto?
  Integrazione con sistemi esistenti (SSO, LDAP)?

□ GESTIONE DELLA SENSIBILITÀ
  Quali categorie di documenti esistono?
  Quali pagine wiki vanno duplicate
  (versione pubblica / versione completa)?

□ AUDIT REQUIREMENTS
  Quali query e accessi vanno loggati?
  Per quanto tempo conservati?
  Quali compliance specifiche si applicano?
```

### 8.4 Decisioni tecniche

```
□ LIVELLO DI PRIVACY
  Cloud, ibrido, on-premise?
  Quali modelli possono essere usati?

□ INFRASTRUTTURA
  Hosting? Storage? Vector DB?

□ INTERFACCIA UTENTE
  Solo chat? Editor wiki? Ricerca?
  Integrazione con strumenti esistenti?

□ INTEGRAZIONI
  Fonti automatiche dei documenti?
  Notifiche, alert, condivisione?
```

### 8.5 Decisioni economiche

```
□ BUDGET INIZIALE
  Setup, primo anno

□ BUDGET A REGIME
  Costo per documento, costo per query
  Stime per anno 2, 3

□ RUOLI INTERNI DEDICATI
  Knowledge curator?
  Tempo dedicato?

□ KPI DI SUCCESSO
  Come si misura se il sistema sta funzionando?
  Quali metriche di adozione?
```

---

## 9. Adattamento a diversi contesti d'uso

L'architettura proposta è generale, ma i parametri concreti variano molto in base al contesto. Ecco linee guida per alcuni scenari tipici.

### 9.1 Knowledge base aziendale

```
CARATTERISTICHE
  · 100-10.000+ documenti
  · Multi-utente con permessi complessi
  · Sensibilità medio-alta dei contenuti
  · Aggiornamento frequente
  · Necessità di audit

ENFASI
  · Access control rigoroso
  · Sincronizzazione affidabile
  · Audit trail completo
  · Integrazione con sistemi aziendali

PARAMETRI TIPICI
  · Hot Layer ricco (organigramma, progetti attivi)
  · Lint settimanale
  · Knowledge curator dedicato
  · Privacy: ibrido o on-premise
```

### 9.2 Ricerca personale o accademica

```
CARATTERISTICHE
  · 100-1.000 documenti (articoli, paper, note)
  · Singolo utente
  · Sensibilità bassa
  · Evoluzione lenta ma profonda
  · Focus sulla sintesi

ENFASI
  · Qualità della sintesi su decisioni di classificazione
  · Grafo di relazioni molto sviluppato
  · Synthesis pages frequenti
  · Esportabilità (es. citazioni)

PARAMETRI TIPICI
  · L2 generoso (la maggior parte dei doc lo merita)
  · Hot Layer focalizzato sui temi di ricerca
  · Privacy: standard (cloud accettabile)
  · Manutenzione: minima, qualche ora al mese
```

### 9.3 Gestione di contenuti editoriali

```
CARATTERISTICHE
  · Volume alto, crescita continua
  · Multi-utente con ruoli (autori, editor)
  · Sensibilità variabile per articolo
  · Necessità di tracciare versioni e diritti

ENFASI
  · Versioning rigoroso
  · Tracciabilità delle sorgenti
  · Gestione dei diritti d'autore
  · Workflow editoriali integrati

PARAMETRI TIPICI
  · Sincronizzazione near real-time
  · Synthesis come "articoli derivati"
  · Audit trail completo
  · Privacy: dipende dalla natura editoriale
```

### 9.4 Supporto clienti e knowledge per assistenza

```
CARATTERISTICHE
  · Knowledge stabile con aggiornamenti puntuali
  · Multi-utente lato organizzazione
  · Possibile esposizione a utenti esterni (clienti)
  · Massima accuratezza richiesta

ENFASI
  · Eval framework rigoroso
  · Confidence calibration
  · Risoluzione conflitti precisa
  · Tracciabilità delle risposte

PARAMETRI TIPICI
  · L2 estensivo per la knowledge canonica
  · Hot Layer come "FAQ structure"
  · Eval set ampio e curato
  · Distinzione tra "wiki pubblica" e "interna"
```

### 9.5 Reading companion (libri, contenuti lunghi)

```
CARATTERISTICHE
  · Singolo utente o piccolo gruppo
  · Sorgenti coese (un libro, una serie)
  · Necessità di catturare sfumature narrative
  · Esplorazione progressiva

ENFASI
  · Multimodalità (immagini, mappe)
  · Grafo ricco di relazioni tra entità
  · Hot Layer come "stato del racconto"
  · Esportabilità (es. note di lettura)

PARAMETRI TIPICI
  · Tutto L2 con sintesi narrative
  · Manutenzione minima
  · Privacy: standard
  · Forte componente di esplorazione interattiva
```

---

## 10. Roadmap di implementazione

Una roadmap tipica si articola in fasi crescenti di complessità e copertura.

### Fase 0 — Discovery (2-3 settimane)

```
□ Workshop con il cliente per definire il dominio
□ Compilazione della checklist delle decisioni (sez. 8)
□ Audit dei documenti esistenti (volumi, formati, sorgenti)
□ Identificazione di 50 documenti rappresentativi per il pilot
□ Definizione dell'eval set iniziale (20-30 domande)
□ Definizione delle metriche di successo
```

### Fase 1 — Pilot (4-6 settimane)

```
□ Setup dell'infrastruttura minima (indici, storage)
□ Ingest manuale dei 50 documenti seed (tutti L2)
□ Costruzione del Hot Layer iniziale
□ Scrittura di AGENTS.md v1
□ Test query con eval set iniziale
□ Iterazione su convenzioni e regole
□ Misurazione costi reali del pilot
□ Decisione GO/NO-GO sulla base dei risultati
```

### Fase 2 — Scaling (6-12 settimane)

```
□ Implementazione del controllo accessi completo
□ Pipeline di ingest automatica con classificazione
□ Ingest progressivo del backlog (L1 prioritario, L2 selettivo)
□ Pipeline di lint settimanale
□ Dashboard di metriche operative
□ Eval framework continuo
□ Formazione knowledge curator
□ Documentazione operativa
```

### Fase 3 — Regime (continuativo)

```
□ Operazioni quotidiane di ingest e query
□ Manutenzione settimanale (vedi sezione 7.9)
□ Eval continuo, regression testing
□ Affinamento periodico di AGENTS.md
□ Espansione progressiva (nuove sorgenti, nuove categorie)
□ Review trimestrale di metriche e ROI
```

### Sequenza visiva

```
  TEMPO →
  
  ──────────┬──────────┬──────────────┬──────────────────────►
            │          │              │
        DISCOVERY   PILOT         SCALING            REGIME
         2-3 sett.  4-6 sett.    6-12 sett.        continuativo
            │          │              │
            ▼          ▼              ▼
        decisioni   eval set     ingest      manutenzione
        chiare      iniziale     completo    e iterazione
                    risultati    sistema     continua
                    GO/NO-GO     in piedi    
```

---

## 11. Correzioni architetturali dal Walking Skeleton (pilot)

Questa sezione consolida le correzioni emerse durante la realizzazione del
walking skeleton (Fase 1 della roadmap, §10) che **non sono specifiche del
prototipo ma modificano la proposta architetturale generale**. Sono
validate empiricamente su un pilot reale: ~21 documenti, **due corpora
distinti** (narrativa Tolkien e Asimov), ingest L0/L1/L2 con classificazione
manuale, stack Azure OpenAI (LLM gpt-5.1 + embedding text-embedding-3-small),
vector DB locale.

Ogni sottosezione indica le sezioni del documento che amenda.

### 11.1 Entity resolution e anti-frammentazione

*Amenda: §5.2 (Wiki Layer), §6.1 (Ingest), §7.8 (Costi), §7.9 (Manutenzione).*

**Problema.** Lo step che identifica le entità da un nuovo documento, se
opera **in isolamento sul singolo documento**, produce frammentazione grave:
nel pilot 21 documenti hanno generato 72 pagine wiki, con duplicati di tre
tipi:

```
1. SINONIMO / VARIANTE
   stessa entità, id diversi
   es. mount_doom vs orodruin · shire vs the_shire
       seldon_crisis vs seldon_crises

2. CATEGORIA vs ISTANZE
   una categoria esplosa in una pagina per istanza
   es. "Crisi Seldon" → 5 pagine (una per crisi)

3. ALIAS / PERSONA
   stesso referente, identità in-world diverse
   es. sauron / annatar / the_necromancer
```

La proposta originale (§6.1) cita il "diff semantico che identifica le
pagine wiki da aggiornare" ma **non tratta la canonicalizzazione e il
riuso degli identificatori come problema di prima classe**. Senza, la wiki
non consolida: si moltiplica. Impatti a catena: costo superlineare
(§11.8), crescita del Hot Layer, diluizione del retrieval (il segnale di
un'entità è spalmato su N pagine).

**Correzione.** Lo step di identificazione entità deve ricevere
**l'inventario delle entità già esistenti** (filtrato per dominio) e
operare con tre regole esplicite:

- **Riuso tassativo**: se l'entità esiste già nell'inventario — anche con
  nome alternativo, sinonimo, altra lingua, articolo, singolare/plurale —
  riusare l'id esatto, mai coniarne una variante.
- **Categoria unica**: una categoria con più istanze è **una** pagina, non
  una per istanza, salvo che la singola istanza abbia trattazione autonoma
  sostanziale.
- **Soglia di rilevanza**: una cosa diventa pagina solo se trattata in modo
  sostanziale; le menzioni di passaggio non sono entità.

Effetto misurato: i duplicati sinonimo e l'esplosione categoria-istanza
sono stati eliminati; il caso alias/persona resta parzialmente aperto.

**Limite di scala (debito noto).** L'inventario passato nel prompt è
**lineare nel numero di entità**. È sostenibile a scala pilot, ma oltre
~500 entità il solo inventario satura il contesto. A regime va reso
**gerarchico** (per dominio → categoria → entità) o pre-filtrato
semanticamente — esattamente l'analogo del vincolo già documentato per il
Hot Layer in §5.3.

**Residuo → lint pipeline.** Il caso alias/persona (stesso essere, nomi
in-world diversi) è un problema di entity resolution sottile, da assegnare
alla **lint pipeline di consolidazione retroattiva** (§6.3), non al
percorso di ingest.

### 11.2 Hot Layer: rebuild differito a fine batch

*Amenda: §5.3 (Hot Layer), §6.4 (Sincronizzazione), §7.8 (Costi).*

**Problema.** Ricostruire il Hot Layer dopo *ogni* documento durante un
ingest batch è **O(documenti × pagine_totali)**: il costo del singolo
rebuild cresce con la wiki accumulata e lo si paga N volte.

**Correzione.** In ingest batch il rebuild del Hot Layer va **differito e
eseguito una sola volta a fine batch**. L'ingest del singolo documento
(real-time) lo ricostruisce subito, come da §6.4. Questo introduce una
distinzione operativa esplicita tra **ingest batch** e **ingest
incrementale** che il modello di sincronizzazione (§6.4) deve prevedere.

Effetto misurato: rebuild da 19 esecuzioni a 2 (una per batch),
risparmio che **cresce quadraticamente** col corpus.

### 11.3 Risoluzione conflitti: caso RAW-vs-RAW

*Amenda: §6.2 (Regole di risoluzione dei conflitti).*

**Problema.** La tabella di §6.2 disambigua solo conflitti WIKI-vs-RAW.
Quando **due fonti RAW dello stesso dominio divergono sullo stesso fatto**
(es. due documenti che riportano un numero diverso), la regola "numeri →
RAW autoritativo" non discrimina, e il modello sceglie arbitrariamente una
delle due dichiarandola autoritativa — falsa precisione.

**Correzione (regola integrativa alla tabella §6.2).**

```
Due fonti RAW divergenti, stesso dominio, nessuna più
recente o più autorevole per provenienza
────────────────────────────────────►  CONFLITTO IRRISOLTO
  · riportare ENTRAMBI i valori con citazione
  · NON sceglierne uno
  · confidence ridotta (≤ medium; low se il fatto
    è centrale per la domanda)
```

Principio generale: una gerarchia di autorità incompleta non va "chiusa"
con una scelta arbitraria; il conflitto irrisolto è un output legittimo e
va dichiarato, con la confidence che lo riflette.

### 11.4 Supporto multi-corpus (dominio)

*Amenda: §4 (Architettura), §7.1 (Controllo accessi — modello a tag), §9
(Adattamento a contesti).*

**Problema.** La proposta è implicitamente mono-dominio. In pratica un
sistema ospita più corpora (progetti, clienti, opere) che **non devono
contaminarsi**. Inoltre è emerso un fallimento non ovvio: una query
generica **senza filtro di dominio** non "mescola" i corpora — ne fa
emergere **uno solo in modo silenzioso**, con piena confidenza e nessun
segnale che gli altri esistano (più pericoloso del mixing, perché
invisibile).

**Correzione.**

- **Tag `domain` di prima classe**, propagato come i tag di
  confidenzialità di §7.1 (stesso meccanismo a due livelli: ereditarietà
  raw→wiki; pagine costruite da sorgenti multi-dominio marcate `_mixed`).
- **Retrieval filtrabile per dominio** a monte (non post-filtro).
- **Policy esplicita in assenza di filtro su corpus multi-dominio**: se il
  contesto copre più domini → risposta strutturata per dominio, niente
  fusione, confidence ridotta; se copre un solo dominio ma ne esistono
  altri → dichiarare esplicitamente la copertura parziale e ridurre la
  confidence.

Il `domain` è ortogonale ai permessi di §7.1 ma usa lo stesso pattern
architetturale: un'unica infrastruttura di tag con propagazione
raw→wiki→query serve entrambi.

### 11.5 Affidabilità delle citazioni: whitelist

*Amenda: §6.2 (Query), §7.2 (Evaluation — metrica "sources").*

**Problema.** L'LLM **fabbrica citazioni plausibili** (`[[id]]` di pagine
inesistenti, o doc_id con cifre corrotte) se non vincolato. Mina
direttamente la metrica "cita le sorgenti giuste?" di §7.2 e la fiducia
nel sistema.

**Correzione.** Il prompt di risposta deve includere la **whitelist
esplicita degli id effettivamente recuperati** (wiki + raw) con istruzione
vincolante: citare con `[[ ]]` solo id in whitelist; per entità non
recuperate, nome in chiaro senza link. Va affiancata una **validazione
post-output** che neutralizza i wikilink fuori whitelist. Da includere
nell'eval framework (§7.2) come check sistematico, non opzionale.

### 11.6 Calibrazione della confidence

*Amenda: §7.2 (confidence calibration).*

**Problema.** Il modello è **overconfident proprio quando dovrebbe
esitare**: risposte da dominanza silenziosa, sintesi ad-hoc non
verificate, conflitti irrisolti — tutte emesse con confidence alta.

**Correzione.** Regole esplicite di **cap della confidence** (non solo
"sii calibrato"): confidence ≤ medium quando (a) conflitto irrisolto
(§11.3), (b) nessun filtro dominio su corpus multi-dominio (§11.4), (c)
risposta è sintesi ad-hoc non persistita. La calibrazione va imposta da
regole verificabili, non lasciata al giudizio del modello.

### 11.7 Robustezza del contratto di output

*Amenda: §6.2 (Query — risposta strutturata).*

**Problema.** La risposta strutturata (testo + citazioni + confidence) è
veicolata da un blocco dati in coda. Se la generazione viene **troncata
al limite di token**, quel blocco non viene emesso e tutta la struttura
(sorgenti, confidence) va persa, pur essendo la risposta ricca.

**Correzione.** Due presidi: (1) dimensionare il limite di output con
margine per la chiusura del blocco strutturato, con istruzione di
preferire una risposta più breve ma completa; (2) **parser con fallback**
che, in assenza del blocco, ricostruisce le citazioni dai riferimenti
inline e degrada la confidence dichiarandolo. Un contratto di output deve
sempre prevedere il proprio fallimento parziale.

### 11.8 Osservabilità dei costi e dati empirici

*Amenda: §7.8 (Costi reali).*

**Strumentazione.** Il pilot ha reso evidente che la stima dei costi
richiede **telemetria per fase** (ingest per livello e sotto-step, query,
embedding) con log append-only, non un totale aggregato. È una componente
infrastrutturale raccomandata, non opzionale, perché senza non si
identifica il driver di costo.

**Driver superlineare individuato.** Il costo non scala col numero di
documenti in modo lineare finché esiste la proliferazione di entità
(§11.1): è `entity_merge` (più entità duplicate ⇒ più merge, ognuno che
trascina la pagina che cresce) il termine superlineare, non l'ingest in
sé. Numeri dal pilot, stesso corpus, prima/dopo le correzioni §11.1+§11.2:

```
                          pre-fix      post-fix     Δ
entity_merge          145.587 tok   42.897 tok   −70%
hot_layer_rebuild      27.474 / 19    3.512 / 2   −87%
re-embed wiki         109.162 tok   62.507 tok   −43%
TOTALE corpus         579.775 tok  399.493 tok   −31%
```

**Conseguenza per il modello di stima §7.8.** La variabile dominante non è
"token per documento" ma il **tasso di consolidamento delle entità**: un
sistema che frammenta paga un sovrapprezzo superlineare nascosto. La stima
va fatta *dopo* aver verificato la qualità dell'entity resolution, non
prima.

### 11.9 Synthesis pages: razionale raffinato

*Amenda: §5.2 (Synthesis), §7.4 (Dependency graph).*

**Osservazione.** Si presumeva che i confronti cross-entità richiedessero
synthesis pages. Il pilot mostra che le domande comparative **con entità
esplicitamente nominate** sono gestite bene *senza* synthesis (il retrieval
trova entrambe le pagine nominate). Il valore reale delle synthesis pages
è altrove:

- **aggregazione implicita** (domande che non nominano le entità ma
  richiedono di combinarne molte);
- **persistenza** (evitare di ricomputare ogni volta una sintesi costosa —
  il limite RAG di §2.1).

Implicazione: la priorità delle synthesis pages nella roadmap (§10) va
giustificata sulla persistenza/aggregazione implicita, non sui confronti
espliciti.

### 11.10 Quadro riassuntivo

```
Correzione                          Sezioni amendate     Stato
─────────────────────────────────────────────────────────────────
11.1 Entity resolution/riuso        5.2 · 6.1 · 7.8/9    applicata*
11.2 Hot Layer batch-deferred       5.3 · 6.4 · 7.8      applicata
11.3 Conflitti RAW-vs-RAW           6.2                  applicata
11.4 Multi-corpus / dominio         4 · 7.1 · 9          applicata
11.5 Whitelist citazioni            6.2 · 7.2            applicata
11.6 Cap della confidence           7.2                  applicata
11.7 Robustezza output              6.2                  applicata
11.8 Telemetria costi               7.8                  applicata
11.9 Razionale synthesis            5.2 · 7.4            recepita

* residuo alias/persona → lint pipeline §6.3 (fase Scaling)
```

Le correzioni 11.1 (inventario gerarchico oltre ~500 entità) e il residuo
alias/persona di 11.1 sono **debito noto esplicito** da affrontare nella
fase di Scaling (§10), insieme alla lint pipeline automatica di
consolidazione retroattiva (§6.3).

---

## 12. Correzioni dalla fase Scaling

Correzioni con impatto architetturale emerse implementando la fase di
Scaling (§10): inventario gerarchico, lint pipeline di consolidazione.
Validate su pilot a 2 corpora. Stesso criterio della §11: solo ciò che
modifica la proposta generale.

### 12.1 Inventario entità: da O(N) a O(1)

*Amenda: §11.1 (chiude il debito noto), §6.1, §7.8.*

Il debito dichiarato in §11.1 — l'inventario delle entità passato allo
step di identificazione cresce linearmente col corpus — è stato chiuso.
Soluzione: scheletro aggregato (domini → subtype → conteggi) + shortlist
semantica delle sole entità candidate, recuperata **riusando il vettore
della source page già calcolato** (zero embedding aggiuntivo). Misura
pilot: la fase di identificazione resta **piatta** (~3.2k token/chiamata)
indipendentemente dal numero di entità accumulate, contro la crescita
lineare precedente. Il costo si sposta da O(N) a O(1) sulla fase. Sotto
una soglia di entità configurabile resta attiva la modalità piatta
(retro-compatibile, più economica su corpora piccoli).

### 12.2 Consolidamento ≠ gerarchia (correzione principale)

*Amenda: §6.3 (lint pipeline), §5.2 (grafo dei collegamenti).*

Errore di design scoperto e corretto: la consolidazione retroattiva
trattava come equivalenti tre relazioni diverse (`same_entity`,
`alias_of`, `subset_of`) unendole tutte. Ma **`subset_of` non è una
relazione di equivalenza**: "Monte Fato dentro Mordor", "Anello Unico tra
gli Anelli del Potere" sono relazioni *gerarchiche*. Unirle tramite
chiusura transitiva collassa interi sottografi in un'unica pagina
(osservato: 9 entità distinte fuse su un singolo nodo).

Principio architetturale: **consolidamento ≠ gerarchia**.

```
same_entity / alias_of   → DUPLICATO    → merge (entità eliminata)
subset_of / part_of      → GERARCHIA    → link nel grafo (§5.2),
                                          entità CONSERVATA distinta
```

La lint pipeline deve produrre due output separati: proposte di merge
(solo equivalenza) e suggerimenti di link gerarchico (mai applicati
automaticamente, le entità restano distinte). Conflaterli distrugge la
knowledge base.

### 12.3 Similarità ≠ identità nei corpora narrativi

*Amenda: §6.3, §7.2 (metrica "sources"/qualità).*

In un corpus narrativo strettamente accoppiato la similarità coseno tra
pagine è **uniformemente alta** (personaggi/luoghi/trama condivisi):
entità palesemente distinte (es. due personaggi diversi) stanno a coseno
~0.88, quanto veri duplicati. Conseguenze di design:

- la soglia di similarità **non è un criterio di qualità**, solo un
  pre-filtro grezzo per limitare il fan-out;
- va calcolata **esplicitamente** dai vettori, non letta dalla distanza
  dell'indice (default L2, semantica e range diversi dal coseno);
- serve un **cap deterministico** sul numero di adjudication, perché la
  sola soglia non limita le coppie;
- il vero rilevatore è l'**adjudication LLM + triage umano**. Questo
  *conferma* la regola di §6.3 ("l'output del lint non è automatico"):
  non è prudenza organizzativa ma necessità tecnica — nessun segnale
  automatico è abbastanza affidabile in questo dominio.

### 12.4 Reversibilità delle operazioni distruttive

*Amenda: §7.5 (versioning e audit trail).*

Ogni operazione distruttiva di manutenzione (merge di consolidazione,
in futuro promozione/declassamento) deve: (1) eseguire il passo
distruttivo **per ultimo**, dopo aver prodotto e salvato il risultato;
(2) registrare un audit append-only con lo **snapshot integrale**
dell'entità eliminata (frontmatter + body), oltre al versioning git.
Lo snapshot rende l'operazione reversibile anche senza git e indipendente
dallo stato del repository.

### 12.5 Classificazione assistita: gate asimmetrico e pattern ricorrente

*Amenda: §6.1 (classificazione), §7.9 (manutenzione).*

La classificazione assistita L0/L1/L2 ha confermato due principi con
impatto oltre lo specifico componente.

**Gate asimmetrico (economico ↔ umano).** Non tutte le proposte vanno
trattate uguali: una decisione *economica e a basso impatto* (L0/L1 ad
alta confidence, o regola deterministica) può essere automatica; una
decisione *costosa o ad alto impatto* (L2, o qualunque confidence non
alta) deve passare per la conferma umana. L'asimmetria non è prudenza
generica ma deriva dalla §6.1: sbagliare *verso il basso* perde il
documento per le query concettuali (danno), sbagliare *verso l'alto* è
solo spreco — quindi si automatizza solo il lato sicuro e si accoda il
resto. È un pattern riusabile per ogni decisione LLM-assistita del
sistema (classificazione, promozione, futura sintesi).

**Pattern ricorrente: "menzione ≠ trattazione sostanziale".** Lo stesso
errore osservato in §11.1 (l'estrazione entità promuove cose solo
*nominate* di passaggio) si è ripresentato *identico* nella
classificazione (un documento di routine denso di nomi propri proposto
L2). Non è un bug isolato ma una **proprietà sistematica del giudizio
LLM su testo entità-denso**: il modello correla densità di nomi propri
con rilevanza. Mitigazione standard, da applicare ovunque il sistema
chieda all'LLM un giudizio di rilevanza: una **regola di confine
esplicita** ("amministrativo/di routine / menzione di passaggio →
livello basso, anche se entità-denso") valutata *prima* di ogni euristica
prudenziale, e l'asimmetria prudenziale ristretta al solo contenuto
realmente sostanziale. Va considerata una linea-guida architetturale
trasversale, non una patch locale.

**Active learning.** Ogni conferma umana alimenta un dataset di esempi
few-shot iniettato nelle classificazioni successive: il costo di triage
decresce nel tempo. È l'asset gemello dell'eval set (§7.2) e va
dimensionato/manutenuto con gli stessi criteri.

### 12.6 Quadro

```
Milestone  Componente                          Stato
─────────────────────────────────────────────────────────
M1         Inventario gerarchico (12.1)        chiuso, validato
M2         Lint consolidazione (12.2–12.4)     chiuso, validato
M3         Classificazione assistita (12.5)    chiuso, validato
```

Fase Scaling completata e validata su pilot a 2 corpora. Il workflow
§6.1 "promozione retroattiva" è ora completo end-to-end: `lint
--audit-l0` individua i candidati, `classify.py --promote <doc_id>
--level <L>` esegue la promozione human-gated riusando `promote()` (raw
immutabile non duplicato, solo step wiki del nuovo livello), e la
decisione alimenta l'active learning come una conferma da coda.

---

## Glossario

**Audit trail** — Registro cronologico e immutabile di tutte le operazioni rilevanti del sistema. Permette di ricostruire chi ha fatto cosa e quando.

**BM25** — Algoritmo di ricerca testuale tradizionale basato sulla frequenza delle parole. Veloce ed efficace per ricerche per parola chiave esatta. Complementare alla ricerca semantica.

**Chunk** — Frammento di un documento. I testi lunghi vengono divisi in chunk sovrapposti per consentire la ricerca su parti specifiche.

**Confidence calibration** — Proprietà di un sistema di assegnare probabilità "oneste" alle proprie risposte. Un sistema ben calibrato che dice "sono sicuro al 90%" sbaglia effettivamente solo nel 10% dei casi.

**Context window** — Quantità massima di testo che un LLM può "tenere in memoria" durante una conversazione. Misurata in token.

**Cold start** — Fase iniziale di un sistema, quando non ha ancora dati pregressi su cui basare le elaborazioni. Richiede strategie specifiche.

**Dependency graph** — Grafo che traccia esplicitamente le dipendenze tra contenuti (es. quali pagine wiki derivano da quali altre). Serve per propagare correttamente gli aggiornamenti.

**Embedding / Vettore semantico** — Rappresentazione numerica del significato di un testo. Due testi con significato simile hanno vettori simili. Permette la ricerca per concetto, non solo per parola chiave.

**Eval set** — Insieme di coppie (domanda, risposta verificata) usate per misurare la qualità del sistema in modo ripetibile.

**Frontmatter** — Intestazione strutturata (tipicamente in formato YAML) all'inizio di una pagina markdown. Contiene metadati leggibili sia dagli umani che dal sistema.

**Hot Layer** — Insieme di pagine sempre presenti nella memoria attiva dell'LLM, che forniscono il contesto di orientamento.

**Ingest** — Processo con cui un nuovo documento entra nel sistema e viene elaborato.

**Lint** — Processo periodico di controllo qualità della knowledge base. In analogia con il "lint" del codice sorgente.

**LLM (Large Language Model)** — Modello di intelligenza artificiale addestrato su grandi quantità di testo, capace di comprendere e generare linguaggio naturale.

**LLM-as-judge** — Tecnica di valutazione in cui un LLM viene usato per giudicare la qualità di una risposta confrontandola con un riferimento.

**Multimodal** — Capacità di gestire contenuti di natura diversa (testo, immagini, tabelle, audio).

**PII (Personally Identifiable Information)** — Informazioni personali identificabili (nomi, codici fiscali, indirizzi, ecc.) soggette a tutela.

**RAG (Retrieval-Augmented Generation)** — Tecnica che combina ricerca documentale e LLM: il sistema recupera testi rilevanti e li passa all'LLM come contesto per generare una risposta.

**Raw layer** — Strato del sistema che conserva i documenti originali integralmente, senza alcuna elaborazione.

**Stale** — Stato di una pagina che potrebbe non essere più aggiornata rispetto alle sue sorgenti.

**Synthesis (page)** — Pagina wiki generata come risposta a una query specifica e archiviata per riuso futuro.

**Threat model** — Analisi sistematica delle minacce di sicurezza e privacy a cui un sistema è esposto.

**Token** — Unità di misura del testo per gli LLM. Approssimativamente 1.000 token equivalgono a 750 parole. I costi degli LLM sono calcolati in token.

**Vector store / Indice vettoriale** — Database specializzato per memorizzare e cercare vettori semantici in modo efficiente.

**Versioning** — Pratica di conservare la storia delle modifiche a un contenuto, permettendo di tornare indietro nel tempo.

**Wiki layer** — Strato del sistema che contiene le pagine sintetizzate e collegate dall'LLM.

---

*Documento di proposta architetturale generale. Versione 2.2*
*v2.1: integrata la §11 con le correzioni architetturali validate sul pilot
(walking skeleton). La proposta originale (§1–§10) è invariata; la §11 ne
amenda i punti indicati, tracciabili per riferimento incrociato.*
*Da personalizzare in fase di Discovery con ogni cliente secondo i criteri della sezione 8.*
