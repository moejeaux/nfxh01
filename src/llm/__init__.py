"""Optional local LLM backends (Ollama, etc.)."""

from src.llm.explain_thesis import explain_trade_thesis
from src.llm.field_ontology import FIELD_ONTOLOGY
from src.llm.local_llm import LocalLLMClient, build_local_llm_client
from src.llm.prompt_context import build_fathom_context

__all__ = [
    "FIELD_ONTOLOGY",
    "LocalLLMClient",
    "build_fathom_context",
    "build_local_llm_client",
    "explain_trade_thesis",
]
