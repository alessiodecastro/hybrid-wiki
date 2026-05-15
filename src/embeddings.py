import os
from openai import OpenAI
from .config import EMBEDDING_MODEL


class Embedder:
    def __init__(self, model: str = EMBEDDING_MODEL):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY non impostata. Copia .env.example in .env e inserisci la chiave.")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        clean = [t.replace("\n", " ").strip() or " " for t in texts]
        response = self.client.embeddings.create(model=self.model, input=clean)
        return [item.embedding for item in response.data]
