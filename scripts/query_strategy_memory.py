"""
Similarity search against the PGVector collection seeded by bootstrap_strategy_memory.py.

  python3 scripts/query_strategy_memory.py "what causes false signals"
  python3 scripts/query_strategy_memory.py "risk controls" -k 5
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv


def _sqlalchemy_psycopg2_url(database_url: str) -> str:
    u = database_url.strip().replace("postgres://", "postgresql://", 1)
    if "+asyncpg" in u:
        return u.replace("+asyncpg", "+psycopg2", 1)
    if u.startswith("postgresql://"):
        return "postgresql+psycopg2://" + u[len("postgresql://") :]
    return u


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Query strategy memory (PGVector + Ollama).")
    parser.add_argument("query", help="Natural language query")
    parser.add_argument("-k", type=int, default=3, help="Number of hits")
    args = parser.parse_args()

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    try:
        from langchain_community.embeddings import OllamaEmbeddings
        from langchain_community.vectorstores import PGVector
    except ImportError:
        print('Missing deps: pip install -e ".[strategy_memory]"', file=sys.stderr)
        return 1

    connection_string = _sqlalchemy_psycopg2_url(raw_url)
    collection = os.getenv("STRATEGY_MEMORY_COLLECTION", "nxfh02_strategy").strip() or "nxfh02_strategy"
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

    embeddings = OllamaEmbeddings(model="nomic-embed-text", base_url=ollama_base)
    vectorstore = PGVector(
        connection_string=connection_string,
        embedding_function=embeddings,
        collection_name=collection,
    )

    results = vectorstore.similarity_search(args.query, k=args.k)
    for i, doc in enumerate(results):
        print(f"\n--- Result {i + 1} ---")
        print(doc.page_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
