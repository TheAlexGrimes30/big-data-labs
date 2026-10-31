
from __future__ import annotations

import csv
import json
import os
import statistics
import time
from pathlib import Path
from xml.sax.saxutils import escape

import clickhouse_connect
import psycopg2
from psycopg2.extras import Json, execute_values
from pymongo import MongoClient



DATA_PATH = Path("/opt/airflow/data/rbc_news.json")
REPORT_DIR = Path("/opt/airflow/reports")

SIZES = [475, 5000]
REPEATS = 5

TABLE = "benchmark_news"
DATABASES = ["PostgreSQL", "MongoDB", "ClickHouse"]
METRICS = ["insert", "filter", "aggregate"]

REPORT_DIR.mkdir(parents=True, exist_ok=True)



def prepare_data(size):
    with DATA_PATH.open(encoding="utf-8") as f:
        source = json.load(f)

    if not isinstance(source, list) or not source:
        raise ValueError("rbc_news.json must contain a non-empty list")

    records = []

    for i in range(size):
        article = source[i % len(source)]

        records.append({
            "row_id": f"benchmark_{i}",
            "category": str(article.get("category") or "Unknown"),
            "payload": article,
        })

    return records


# ---------------- CONNECTIONS ----------------

pg = psycopg2.connect(
    host=os.getenv("ETL_POSTGRES_HOST", "postgres-ods"),
    port=int(os.getenv("ETL_POSTGRES_PORT", "5432")),
    dbname=os.getenv("ETL_POSTGRES_DB", "ods"),
    user=os.getenv("ETL_POSTGRES_USER", "etl_user"),
    password=os.getenv("ETL_POSTGRES_PASSWORD", "etl_password"),
)

mongo = MongoClient(
    os.getenv(
        "ETL_MONGODB_URI",
        "mongodb://etl_user:etl_password@mongodb:27017/"
        "?authSource=admin"
    )
)
mongo_db = mongo[os.getenv("ETL_MONGODB_DB", "ods")]

ch = clickhouse_connect.get_client(
    host=os.getenv("ETL_CLICKHOUSE_HOST", "clickhouse"),
    port=int(os.getenv("ETL_CLICKHOUSE_PORT", "8123")),
    username=os.getenv("ETL_CLICKHOUSE_USER", "etl_user"),
    password=os.getenv("ETL_CLICKHOUSE_PASSWORD", "etl_password"),
    database=os.getenv("ETL_CLICKHOUSE_DB", "ods"),
)



def reset_postgres():
    with pg.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS ods.{TABLE}")
        cur.execute(f"""
            CREATE TABLE ods.{TABLE} (
                row_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                payload JSONB NOT NULL
            )
        """)
    pg.commit()


def reset_mongodb():
    mongo_db.drop_collection(TABLE)


def reset_clickhouse():
    ch.command(f"DROP TABLE IF EXISTS ods.{TABLE}")
    ch.command(f"""
        CREATE TABLE ods.{TABLE} (
            row_id String,
            category String,
            payload String
        )
        ENGINE = MergeTree()
        ORDER BY row_id
    """)


RESET = {
    "PostgreSQL": reset_postgres,
    "MongoDB": reset_mongodb,
    "ClickHouse": reset_clickhouse,
}



def insert_postgres(records):
    values = [
        (r["row_id"], r["category"], Json(r["payload"]))
        for r in records
    ]

    with pg.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO ods.{TABLE}
                (row_id, category, payload)
            VALUES %s
            """,
            values,
            page_size=1000,
        )

    pg.commit()


def insert_mongodb(records):
    documents = [
        {
            "_id": r["row_id"],
            "category": r["category"],
            "payload": r["payload"],
        }
        for r in records
    ]

    mongo_db[TABLE].insert_many(
        documents,
        ordered=False,
    )


def insert_clickhouse(records):
    rows = [
        [
            r["row_id"],
            r["category"],
            json.dumps(r["payload"], ensure_ascii=False),
        ]
        for r in records
    ]

    ch.insert(
        f"ods.{TABLE}",
        rows,
        column_names=["row_id", "category", "payload"],
    )


INSERT = {
    "PostgreSQL": insert_postgres,
    "MongoDB": insert_mongodb,
    "ClickHouse": insert_clickhouse,
}



def postgres_filter():
    with pg.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM ods.{TABLE} WHERE category = %s",
            ("Политика",),
        )
        return cur.fetchone()[0]


def mongodb_filter():
    return mongo_db[TABLE].count_documents({
        "category": "Политика"
    })


def clickhouse_filter():
    return ch.query(
        f"""
        SELECT count()
        FROM ods.{TABLE}
        WHERE category = 'Политика'
        """
    ).result_rows[0][0]


FILTER = {
    "PostgreSQL": postgres_filter,
    "MongoDB": mongodb_filter,
    "ClickHouse": clickhouse_filter,
}


def postgres_aggregate():
    with pg.cursor() as cur:
        cur.execute(f"""
            SELECT category, COUNT(*)
            FROM ods.{TABLE}
            GROUP BY category
            ORDER BY COUNT(*) DESC
        """)
        return cur.fetchall()


def mongodb_aggregate():
    return list(mongo_db[TABLE].aggregate([
        {
            "$group": {
                "_id": "$category",
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
    ]))


def clickhouse_aggregate():
    return ch.query(f"""
        SELECT category, count() AS cnt
        FROM ods.{TABLE}
        GROUP BY category
        ORDER BY cnt DESC
    """).result_rows


AGGREGATE = {
    "PostgreSQL": postgres_aggregate,
    "MongoDB": mongodb_aggregate,
    "ClickHouse": clickhouse_aggregate,
}



results = []


def measure(database, metric, size, func, repeats=REPEATS):
    times = []

    for _ in range(repeats):
        start = time.perf_counter()
        func()
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)

    result = {
        "database": database,
        "metric": metric,
        "size": size,
        "repeats": repeats,
        "mean_ms": round(statistics.mean(times), 4),
        "median_ms": round(statistics.median(times), 4),
        "min_ms": round(min(times), 4),
        "max_ms": round(max(times), 4),
    }

    results.append(result)

    print(
        f"{database:12} | {metric:10} | "
        f"{size:6} rows | "
        f"median={result['median_ms']:.3f} ms"
    )



def run_benchmark():
    for size in SIZES:
        print(f"\n{'=' * 55}")
        print(f"BENCHMARK: {size} rows")
        print(f"{'=' * 55}")

        records = prepare_data(size)

        for database in DATABASES:

            # Repeat insertion into freshly created tables.
            # Table creation is not included in insertion time.
            def write_once():
                RESET[database]()
                INSERT[database](records)

            measure(
                database,
                "insert",
                size,
                write_once,
            )

            # A fresh table with exactly 'size' records
            # is now available for read tests.

            # Warm up the read paths.
            FILTER[database]()
            AGGREGATE[database]()

            measure(
                database,
                "filter",
                size,
                FILTER[database],
            )

            measure(
                database,
                "aggregate",
                size,
                AGGREGATE[database],
            )


# ---------------- CSV ----------------

def save_csv():
    path = REPORT_DIR / "benchmark_results.csv"

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "database",
                "metric",
                "size",
                "repeats",
                "mean_ms",
                "median_ms",
                "min_ms",
                "max_ms",
            ],
        )

        writer.writeheader()
        writer.writerows(results)

    print(f"\nCSV saved: {path}")



def save_chart(metric, size):
    selected = [
        r for r in results
        if r["metric"] == metric and r["size"] == size
    ]

    selected.sort(key=lambda r: DATABASES.index(r["database"]))

    if not selected:
        return

    colors = {
        "PostgreSQL": "#2563eb",
        "MongoDB": "#16a34a",
        "ClickHouse": "#f59e0b",
    }

    max_value = max(r["median_ms"] for r in selected) or 1
    width = 760
    height = 320

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="30" y="38" font-size="21" '
        f'font-family="Arial" font-weight="bold">'
        f'{escape(metric.title())} — {size} rows</text>',
        '<text x="30" y="63" font-size="13" '
        'font-family="Arial" fill="#555">'
        'Median client-side time (ms), lower is better</text>',
    ]

    for i, row in enumerate(selected):
        y = 95 + i * 70
        value = row["median_ms"]
        bar_width = max(1, 440 * value / max_value)
        name = row["database"]

        elements.extend([
            f'<text x="30" y="{y + 23}" '
            f'font-family="Arial" font-size="15">'
            f'{escape(name)}</text>',

            f'<rect x="175" y="{y}" '
            f'width="{bar_width:.2f}" height="32" '
            f'rx="4" fill="{colors[name]}"/>',

            f'<text x="{185 + bar_width:.2f}" y="{y + 22}" '
            f'font-family="Arial" font-size="14">'
            f'{value:.3f} ms</text>',
        ])

    elements.append("</svg>")

    path = REPORT_DIR / f"{metric}_{size}.svg"
    path.write_text("\n".join(elements), encoding="utf-8")

    print(f"Chart saved: {path}")


def save_charts():
    for size in SIZES:
        for metric in METRICS:
            save_chart(metric, size)



if __name__ == "__main__":
    try:
        run_benchmark()
        save_csv()
        save_charts()

        print("\nBENCHMARK COMPLETED SUCCESSFULLY")

    finally:

        try:
            reset_postgres()
            reset_mongodb()
            reset_clickhouse()
        finally:
            pg.close()
            mongo.close()
            ch.close()
