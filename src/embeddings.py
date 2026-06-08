"""
Wrapper sul modello di embedding Azure OpenAI.

Espone una API minima (`embed`, `embed_batch`) usata sia dall'ingest che
dalla query. La batch è essenziale per l'ingest: chunkare un documento da
500 parole produce 3-4 chunk, e fare una sola chiamata batch è molto più
efficiente (latenza, fatturazione, throughput) di 4 chiamate separate.

Resilienza al rate limit: il tier S0 di Azure limita text-embedding-3-* per
minuto, e il burst di embedding di un singolo doc L2 ricco può superarlo
(HTTP 429). `embed_batch` ritenta gli errori transitori (429/timeout/5xx) con
backoff esponenziale + jitter, rispettando l'header Retry-After del server.
Gli errori permanenti (auth, bad request, content filter) NON vengono
ritentati: si propagano subito al chiamante.

Tracking dei token integrato via TokenTracker opzionale.
"""

import os
import random
import sys
import time

from openai import (
    AzureOpenAI,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from .config import (
    EMBEDDING_DEPLOYMENT,
    AZURE_API_VERSION,
    EMBED_MAX_RETRIES,
    EMBED_BACKOFF_BASE,
    EMBED_BACKOFF_CAP,
)
from .token_tracker import TokenTracker

# Eccezioni transitorie su cui ha senso ritentare. Gli errori permanenti
# (AuthenticationError 401, BadRequestError 400, content filter) NON sono qui:
# ritentarli sprecherebbe tempo e maschererebbe il bug reale.
_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


class Embedder:
    """Wrapper sul client embeddings di Azure OpenAI.

    Stateless rispetto al dominio: non sa nulla di chunk, documenti, pagine
    wiki. Riceve liste di stringhe e restituisce liste di vettori.
    """

    def __init__(self, deployment: str = EMBEDDING_DEPLOYMENT, tracker: TokenTracker | None = None):
        """Inizializza il client.

        Args:
            deployment: nome della deployment di embedding su Azure
                (es. "text-embedding-3-small"). NON è il nome del modello.
            tracker: opzionale, registra il consumo token per ogni batch.

        Raises:
            RuntimeError: se le credenziali Azure non sono configurate.
        """
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not api_key or not endpoint:
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY o AZURE_OPENAI_ENDPOINT non impostati. "
                "Copia .env.example in .env e inserisci i valori."
            )
        # max_retries=0 disabilita il retry implicito dell'SDK: il backoff lo
        # gestiamo noi in _embed_with_retry (configurabile via env, con log
        # visibile e Retry-After rispettato). Lasciare entrambi attivi
        # raddoppierebbe le attese in modo opaco.
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=AZURE_API_VERSION,
            max_retries=0,
        )
        self.deployment = deployment
        self.tracker = tracker

    def embed(self, text: str) -> list[float]:
        """Embedda un singolo testo. Helper di convenienza su embed_batch."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embedda una lista di testi in una sola chiamata API.

        Args:
            texts: lista di stringhe da embeddare. Lista vuota -> ritorno
                immediato senza chiamare l'API.

        Returns:
            Lista di vettori, stesso ordine dell'input.
        """
        if not texts:
            return []
        # I newline degradano la qualità degli embedding OpenAI (problema
        # noto per text-embedding-3-*): vengono trasformati in spazi.
        # Il fallback su " " evita l'errore "input cannot be empty" se un
        # chunk si riduce a una stringa vuota dopo lo strip.
        clean = [t.replace("\n", " ").strip() or " " for t in texts]
        response = self._embed_with_retry(clean)

        # Registra il consumo. Per gli embedding non esistono "completion
        # tokens" (output non testuale), quindi totale = prompt_tokens.
        # Tracciamo anche n_items come metadato utile per capire la
        # distribuzione dei batch.
        if self.tracker is not None and getattr(response, "usage", None):
            u = response.usage
            self.tracker.record(
                operation="embedding",
                model=self.deployment,
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=0,
                extra={"n_items": len(clean)},
            )

        return [item.embedding for item in response.data]

    # ------------------------------------------------------------------
    # Resilienza al rate limit
    # ------------------------------------------------------------------

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        """Estrae l'header Retry-After (secondi) da un'eccezione SDK, se c'è.

        Azure include Retry-After nelle risposte 429: rispettarlo è più
        educato ed efficiente del backoff cieco. Gestisce solo il formato
        numerico (secondi), quello usato da Azure OpenAI; un eventuale
        formato data HTTP viene ignorato (si ricade sul backoff). Ritorna
        None se l'header è assente o non parsabile.
        """
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        raw = headers.get("retry-after")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _embed_with_retry(self, clean: list[str]):
        """Chiama l'API embeddings ritentando sugli errori transitori.

        Attesa per tentativo = max(backoff esponenziale con jitter,
        Retry-After del server), troncata a EMBED_BACKOFF_CAP. Dopo
        EMBED_MAX_RETRIES tentativi falliti rilancia l'ultima eccezione, così
        il chiamante (ingest) la gestisce per-documento e prosegue con gli
        altri invece di far crashare l'intero run.
        """
        last_exc: Exception | None = None
        for attempt in range(EMBED_MAX_RETRIES + 1):
            try:
                return self.client.embeddings.create(model=self.deployment, input=clean)
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt >= EMBED_MAX_RETRIES:
                    break
                # Backoff esponenziale (base * 2^tentativo) + jitter, con cap.
                backoff = min(EMBED_BACKOFF_BASE * (2 ** attempt), EMBED_BACKOFF_CAP)
                backoff += random.uniform(0, EMBED_BACKOFF_BASE)
                # Il server può chiedere un'attesa minima esplicita: la si usa
                # come pavimento (sempre entro il cap per non bloccare troppo).
                server_hint = self._retry_after_seconds(exc)
                wait = min(max(backoff, server_hint or 0.0), EMBED_BACKOFF_CAP)
                print(
                    f"[embeddings] {type(exc).__name__} (tentativo "
                    f"{attempt + 1}/{EMBED_MAX_RETRIES}): retry tra {wait:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
        # Tentativi esauriti: rilancia l'ultima eccezione transitoria.
        assert last_exc is not None
        raise last_exc
