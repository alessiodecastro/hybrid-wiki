import os
from anthropic import Anthropic
from .config import LLM_MODEL


class LLMClient:
    def __init__(self, model: str = LLM_MODEL):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY non impostata. Copia .env.example in .env e inserisci la chiave.")
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return "".join(parts).strip()
