"""
Wrapper sottile sul client Azure OpenAI per le chat completions.

Lo scopo è isolare il resto del codice dai dettagli SDK: in futuro si potrà
sostituire backend (Anthropic, Bedrock, modello on-prem) toccando solo
questo file, mantenendo la firma `complete(system, user)` stabile.

Walking skeleton: niente retry, streaming, function calling, prompt caching.
Saranno aggiunti nelle fasi successive quando emergeranno i pattern d'uso.
"""

import os
from openai import AzureOpenAI
from .config import LLM_DEPLOYMENT, AZURE_API_VERSION


class LLMClient:
    """Client sincrono per le chat completions su Azure OpenAI.

    Una sola responsabilità: dato un system prompt e un messaggio utente,
    restituire la stringa di testo prodotta dal modello. Tutto il resto
    (parsing JSON, gestione conflitti, retrieval) sta nei moduli chiamanti.
    """

    def __init__(self, deployment: str = LLM_DEPLOYMENT):
        """Inizializza il client.

        Args:
            deployment: nome della deployment Azure (NON il nome del modello
                base). Default letto da .env via config.LLM_DEPLOYMENT.

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

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """Esegue una completion chat con un singolo turno user.

        Args:
            system: prompt di sistema (istruzioni, regole, contesto).
            user: messaggio utente.
            max_tokens: limite di token di OUTPUT (non include il prompt).

        Returns:
            Testo della risposta, già strippato. Stringa vuota se il modello
            non ha prodotto contenuto testuale.
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
        return (response.choices[0].message.content or "").strip()
