"""
Wrapper sottile sul client Azure OpenAI per le chat completions.

Lo scopo è isolare il resto del codice dai dettagli SDK: in futuro si potrà
sostituire backend (Anthropic, Bedrock, modello on-prem) toccando solo
questo file, mantenendo la firma `complete(system, user)` stabile.

Walking skeleton: niente retry, streaming, function calling, prompt caching.
Saranno aggiunti nelle fasi successive quando emergeranno i pattern d'uso.
Tracking dei token integrato via TokenTracker opzionale.
"""

import os
from openai import AzureOpenAI
from .config import LLM_DEPLOYMENT, AZURE_API_VERSION
from .token_tracker import TokenTracker


class LLMClient:
    """Client sincrono per le chat completions su Azure OpenAI.

    Una sola responsabilità: dato un system prompt e un messaggio utente,
    restituire la stringa di testo prodotta dal modello. Tutto il resto
    (parsing JSON, gestione conflitti, retrieval) sta nei moduli chiamanti.

    Se viene passato un TokenTracker, ogni chiamata emette un record di
    consumo con la fase corrente (impostata via `tracker.phase(...)`).
    """

    def __init__(self, deployment: str = LLM_DEPLOYMENT, tracker: TokenTracker | None = None):
        """Inizializza il client.

        Args:
            deployment: nome della deployment Azure (NON il nome del modello
                base). Default letto da .env via config.LLM_DEPLOYMENT.
            tracker: opzionale, se presente registra il consumo token di
                ogni chiamata. Tipicamente injectato dalla pipeline.

        Raises:
            RuntimeError: se le credenziali Azure non sono configurate.
        """
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not api_key or not endpoint:
            # Fallisce fast e con un messaggio actionable invece di lasciar
            # arrivare un 401 oscuro dal layer SDK.
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
        self.tracker = tracker

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """Esegue una completion chat con un singolo turno user.

        Args:
            system: prompt di sistema (istruzioni, regole, contesto).
            user: messaggio utente.
            max_tokens: limite di token di OUTPUT (non include il prompt).

        Returns:
            Testo della risposta, già strippato. Stringa vuota se il modello
            non ha prodotto contenuto testuale.

        Side effect:
            Se è configurato un tracker, registra un record con i token
            consumati (input/output/cached) e la fase corrente.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # I modelli serie GPT-5 richiedono `max_completion_tokens` al posto
        # del vecchio `max_tokens` e non accettano temperature custom.
        # Il try/except garantisce retro-compatibilità con SDK più vecchi
        # che non riconoscono ancora il parametro nuovo.
        try:
            response = self.client.chat.completions.create(
                model=self.deployment,
                max_completion_tokens=max_tokens,
                messages=messages,
            )
        except TypeError:
            response = self.client.chat.completions.create(
                model=self.deployment,
                max_tokens=max_tokens,
                messages=messages,
            )

        # Emette un record di telemetria se la API ha restituito il blocco
        # usage (Azure OpenAI lo include sempre per le chat). Lo facciamo
        # PRIMA di restituire così anche eventuali eccezioni a valle non
        # impediscono di tracciare il costo già sostenuto.
        if self.tracker is not None and getattr(response, "usage", None):
            u = response.usage
            cached = 0
            # Sui modelli più nuovi può comparire `prompt_tokens_details.cached_tokens`
            # quando entra in gioco il prompt caching: lo isoliamo come metrica
            # separata per stimare lo sconto applicato (variabile per modello).
            details = getattr(u, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
            self.tracker.record(
                operation="chat",
                model=self.deployment,
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                cached_tokens=cached,
            )

        return (response.choices[0].message.content or "").strip()
