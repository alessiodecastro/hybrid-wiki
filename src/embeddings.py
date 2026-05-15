"""
Wrapper sul modello di embedding Azure OpenAI.

Espone una API minima (`embed`, `embed_batch`) usata sia dall'ingest che
dalla query. La batch è essenziale per l'ingest: chunkare un documento da
500 parole produce 3-4 chunk, e fare una sola chiamata batch è molto più
efficiente (latenza, fatturazione, throughput) di 4 chiamate separate.
"""

import os
from openai import AzureOpenAI
from .config import EMBEDDING_DEPLOYMENT, AZURE_API_VERSION


class Embedder:
    """Wrapper sul client embeddings di Azure OpenAI.

    Stateless rispetto al dominio: non sa nulla di chunk, documenti, pagine
    wiki. Riceve liste di stringhe e restituisce liste di vettori.
    """

    def __init__(self, deployment: str = EMBEDDING_DEPLOYMENT):
        """Inizializza il client.

        Args:
            deployment: nome della deployment di embedding su Azure
                (es. "text-embedding-3-small"). NON è il nome del modello.

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
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=AZURE_API_VERSION,
        )
        self.deployment = deployment

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
        response = self.client.embeddings.create(model=self.deployment, input=clean)
        return [item.embedding for item in response.data]
