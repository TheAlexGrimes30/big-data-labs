"""Extract source RBC snapshot files and split into three logically distinct feeds.

This is an *offline replay* of RBC data originally collected from the public website.
To refresh it, replace data/rbc_news.json and data/rbc_news.csv with exports
from the user's RBC parser; the DAGs pick up these files at every run.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import DATASETS, csv_path, json_path

REQUIRED = frozenset({"url", "title", "category", "published_at", "source"})


def read_json(path: Path | None = None) -> list[dict[str, Any]]:
    path = Path(path) if path is not None else json_path()
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        raise ValueError(f"Expected non-empty JSON list: {path}")
    _validate_source(items)
    return items


def read_csv(path: Path | None = None) -> list[dict[str, str]]:
    path = Path(path) if path is not None else csv_path()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
            raise ValueError(f"Invalid CSV headers: {reader.fieldnames}")
        items = list(reader)
    if not items:
        raise ValueError(f"Empty CSV: {path}")
    _validate_source(items)
    return items


def _validate_source(items: list[dict]) -> None:
    for i, obj in enumerate(items):
        if not isinstance(obj, dict) or not REQUIRED.issubset(obj):
            raise ValueError(f"Malformed row {i}")
        if not isinstance(obj.get("url"), str) or not obj["url"].strip():
            raise ValueError(f"Missing URL at row {i}")
        if not isinstance(obj.get("title"), str) or not obj["title"].strip():
            raise ValueError(f"Missing title at row {i}")


def extract_dataset(dataset: str, *, json_file: Path | None = None,
                    csv_file: Path | None = None) -> list[dict[str, Any]]:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown feed: {dataset}")
    if dataset == "news_content":
        news = read_json(json_file)
        rows = [
            {"row_id": n["url"], "category": n.get("category") or "Без категории",
             "payload": {k: n.get(k) for k in ("url", "content", "content_status", "scraped_at")}}
            for n in news
        ]
    else:
        news = read_csv(csv_file)
        if dataset == "news_metadata":
            rows = [
                {"row_id": n["url"], "category": n.get("category") or "Без категории",
                 "payload": {key: value for key, value in n.items() if key != "content"}}
                for n in news
            ]
        else:
            categories = sorted({n.get("category") or "Без категории" for n in news})
            rows = [
                {"row_id": c, "category": c, "payload": {"category": c, "source": "РБК"}}
                for c in categories
            ]
    return rows


def validate_dataset(dataset: str, **paths: Any) -> dict[str, Any]:
    rows = extract_dataset(dataset, **paths)
    keys = [r["row_id"] for r in rows]
    duplicates = sum(n - 1 for n in Counter(keys).values() if n > 1)
    if duplicates:
        raise ValueError(f"{dataset}: {duplicates} duplicate business keys")
    return {
        "dataset": dataset,
        "extracted_rows": len(rows),
        "unique_keys": len(keys),
        "source_format": "JSON" if dataset == "news_content" else "CSV",
    }
