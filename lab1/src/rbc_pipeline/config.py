"""Central configuration for three source datasets and storage adapters."""
from __future__ import annotations

import os
from pathlib import Path

DATASETS = ("news_metadata", "news_content", "categories")
TARGETS = ("postgres", "clickhouse", "mongodb", "redis")


def json_path() -> Path:
    return Path(os.getenv("RBC_JSON_PATH", "/opt/airflow/data/rbc_news.json"))


def csv_path() -> Path:
    return Path(os.getenv("RBC_CSV_PATH", "/opt/airflow/data/rbc_news.csv"))


def postgres_kwargs() -> dict:
    return {
        "host": os.getenv("ETL_POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("ETL_POSTGRES_PORT", "5433")),
        "dbname": os.getenv("ETL_POSTGRES_DB", "ods"),
        "user": os.getenv("ETL_POSTGRES_USER", "etl_user"),
        "password": os.getenv("ETL_POSTGRES_PASSWORD", "etl_password"),
        "connect_timeout": 10,
    }


def clickhouse_kwargs() -> dict:
    return {
        "host": os.getenv("ETL_CLICKHOUSE_HOST", "localhost"),
        "port": int(os.getenv("ETL_CLICKHOUSE_PORT", "8123")),
        "database": os.getenv("ETL_CLICKHOUSE_DB", "ods"),
        "username": os.getenv("ETL_CLICKHOUSE_USER", "etl_user"),
        "password": os.getenv("ETL_CLICKHOUSE_PASSWORD", "etl_password"),
    }


def mongodb_uri() -> str:
    return os.getenv(
        "ETL_MONGODB_URI",
        "mongodb://etl_user:etl_password@localhost:27017/?authSource=admin",
    )


def redis_kwargs() -> dict:
    return {
        "host": os.getenv("ETL_REDIS_HOST", "localhost"),
        "port": int(os.getenv("ETL_REDIS_PORT", "6380")),
        "decode_responses": True,
        "socket_connect_timeout": 10,
    }
