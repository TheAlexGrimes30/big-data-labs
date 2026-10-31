"""Idempotent raw ODS loaders for 3 DBs + Redis demonstration cache.

Each logical dataset has a raw_* table or collection with a business key,
category for a simple filter and unmodified raw payload (JSONB / string / BSON).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import clickhouse_connect
import psycopg2
import redis
from psycopg2.extras import Json, execute_values
from pymongo import MongoClient, ReplaceOne

from .config import (DATASETS, TARGETS, clickhouse_kwargs, mongodb_uri,
                     postgres_kwargs, redis_kwargs)
from .extract import extract_dataset

LOG = logging.getLogger(__name__)


def table_name(dataset: str) -> str:
    if dataset not in DATASETS:
        raise ValueError(dataset)
    return "raw_" + dataset


def redis_row_key(dataset: str, business_key: str) -> str:
    return f"rbc:{table_name(dataset)}:{hashlib.sha256(business_key.encode()).hexdigest()}"


def redis_cat_key(dataset: str, category: str) -> str:
    return f"rbc:index:{table_name(dataset)}:{hashlib.sha256(category.encode()).hexdigest()}"


def postgres_client():
    return psycopg2.connect(**postgres_kwargs())


def clickhouse_client():
    return clickhouse_connect.get_client(**clickhouse_kwargs())


def mongodb_client():
    return MongoClient(mongodb_uri(), serverSelectionTimeoutMS=15000)


def redis_client():
    return redis.Redis(**redis_kwargs())


def load_postgres(dataset: str, rows: list[dict]) -> int:

    table = table_name(dataset)
    with postgres_client() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS ods")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS ods.{table} (
                    row_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_cat ON ods.{table} (category)")
            if rows:
                execute_values(cur, f"""
                    INSERT INTO ods.{table} (row_id, category, payload)
                    VALUES %s
                    ON CONFLICT (row_id) DO UPDATE SET
                        category = EXCLUDED.category,
                        payload = EXCLUDED.payload,
                        loaded_at = now()
                """, [(r["row_id"], r["category"], Json(r["payload"])) for r in rows],
                    page_size=200)
    return len(rows)


def load_clickhouse(dataset: str, rows: list[dict]) -> int:
    table = table_name(dataset)
    ch = clickhouse_client()
    try:
        ch.command("CREATE DATABASE IF NOT EXISTS ods")
        ch.command(f"""
            CREATE TABLE IF NOT EXISTS ods.{table} (
                row_id String,
                category LowCardinality(String),
                payload String,
                version UInt64
            ) ENGINE = ReplacingMergeTree(version)
            ORDER BY row_id
        """)
        version = time.time_ns()
        if rows:
            ch.insert(f"ods.{table}", [
                [r["row_id"], r["category"], json.dumps(r["payload"], ensure_ascii=False), version]
                for r in rows
            ], column_names=["row_id", "category", "payload", "version"])
    finally:
        ch.close()
    return len(rows)


def load_mongodb(dataset: str, rows: list[dict]) -> int:

    client = mongodb_client()
    try:
        collection = client["ods"][table_name(dataset)]
        collection.create_index("category")
        actions = [
            ReplaceOne({"_id": r["row_id"]}, {
                "_id": r["row_id"], "category": r["category"], "payload": r["payload"]
            }, upsert=True)
            for r in rows
        ]
        if actions:
            collection.bulk_write(actions, ordered=False)
    finally:
        client.close()
    return len(rows)


def load_redis(dataset: str, rows: list[dict]) -> int:
    client = redis_client()
    client.ping()
    for r in rows:
        row_key = redis_row_key(dataset, r["row_id"])
        old = client.get(row_key)
        previous_category = json.loads(old)["category"] if old else None
        with client.pipeline(transaction=True) as pipe:
            if previous_category and previous_category != r["category"]:
                pipe.srem(redis_cat_key(dataset, previous_category), row_key)
            pipe.set(row_key, json.dumps(r, ensure_ascii=False))
            pipe.sadd(f"rbc:keys:{table_name(dataset)}", row_key)
            pipe.sadd(redis_cat_key(dataset, r["category"]), row_key)
            pipe.execute()
    return len(rows)


LOADERS = {
    "postgres": load_postgres,
    "clickhouse": load_clickhouse,
    "mongodb": load_mongodb,
    "redis": load_redis,
}


def load_target(dataset: str, target: str) -> dict[str, Any]:
    if target not in TARGETS:
        raise ValueError(target)
    rows = extract_dataset(dataset)
    count = LOADERS[target](dataset, rows)
    LOG.info("Ingested dataset=%s target=%s records=%d", dataset, target, count)
    return {"dataset": dataset, "target": target, "inserted_or_updated": count}


def count_target(dataset: str, target: str) -> int:
    table = table_name(dataset)
    if target == "postgres":
        with postgres_client() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM ods.{table}")
                return int(cur.fetchone()[0])
    if target == "clickhouse":
        ch = clickhouse_client()
        try:
            return int(ch.command(f"SELECT count() FROM ods.{table} FINAL"))
        finally:
            ch.close()
    if target == "mongodb":
        client = mongodb_client()
        try:
            return int(client["ods"][table].count_documents({}))
        finally:
            client.close()
    if target == "redis":
        return int(redis_client().scard(f"rbc:keys:{table}"))
    raise ValueError(target)


def verify_dataset(dataset: str) -> list[dict[str, Any]]:
    expected = len(extract_dataset(dataset))
    output = []
    for target in TARGETS:
        actual = count_target(dataset, target)
        if actual != expected:
            raise AssertionError(f"{dataset}/{target}: expected {expected}, got {actual}")
        output.append({"storage": target, "rows": actual, "ok": True})
    LOG.info("Verified %s: %s", dataset, output)
    return output
