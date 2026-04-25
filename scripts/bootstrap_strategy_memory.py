"""
One-shot: embed strategy blurbs into Postgres via LangChain PGVector + Ollama.

Requires:
  pip install -e ".[strategy_memory]"
  Postgres with CREATE EXTENSION vector;
  Ollama running with: ollama pull nomic-embed-text

Env (see .env.example):
  DATABASE_URL — same as rest of NXFH01 (asyncpg URLs are rewritten for psycopg2)
  OLLAMA_BASE_URL — default http://localhost:11434
  STRATEGY_MEMORY_COLLECTION — optional, default nxfh02_strategy
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv


def _sqlalchemy_psycopg2_url(database_url: str) -> str:
    """LangChain PGVector uses SQLAlchemy sync; prefer psycopg2 driver in the URL."""
    u = database_url.strip().replace("postgres://", "postgresql://", 1)
    if "+asyncpg" in u:
        return u.replace("+asyncpg", "+psycopg2", 1)
    if u.startswith("postgresql://"):
        return "postgresql+psycopg2://" + u[len("postgresql://") :]
    return u


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bootstrap strategy memory PGVector collection.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop existing LangChain tables for this collection before insert.",
    )
    args = parser.parse_args()

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    try:
        from langchain_community.embeddings import OllamaEmbeddings
        from langchain_community.vectorstores import PGVector
    except ImportError:
        print(
            "Missing deps: pip install -e \".[strategy_memory]\"",
            file=sys.stderr,
        )
        return 1

    connection_string = _sqlalchemy_psycopg2_url(raw_url)
    collection = os.getenv("STRATEGY_MEMORY_COLLECTION", "nxfh02_strategy").strip() or "nxfh02_strategy"
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

    docs = [
        "NXFH02 Momentum Strategy v1: Entry triggers on RSI > 65 with volume confirmation...",
        "Known failure mode: strategy over-trades during low-volume overnight UTC sessions...",
        "Risk controls: max 2% position size, hard stop at 1.5% drawdown...",
        "Regime classification: trending = price > 20 EMA + OI expanding...",
    ]

    embeddings = OllamaEmbeddings(model="nomic-embed-text", base_url=ollama_base)

    PGVector.from_texts(
        texts=docs,
        embedding=embeddings,
        collection_name=collection,
        connection_string=connection_string,
        pre_delete_collection=args.reset,
    )
    print(f"OK embedded {len(docs)} texts into collection={collection!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
