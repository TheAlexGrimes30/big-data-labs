from .extract import validate_dataset
from .storages import load_target, verify_dataset
from datetime import datetime, timezone
from airflow.sdk import dag, task


def create_dag(dataset: str):

    @dag(
        dag_id=f"rbc_ods_{dataset}",
        description=f"RBC ETL: {dataset}",
        start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
        schedule=None,
        catchup=False,
        max_active_runs=1,
        default_args={"retries": 1},
        tags=["lab1", "RBC", "ODS"],
    )
    def pipeline():

        @task
        def extract_and_validate() -> dict:
            return validate_dataset(dataset)

        @task
        def load_postgres() -> dict:
            return load_target(dataset, "postgres")

        @task
        def load_clickhouse() -> dict:
            return load_target(dataset, "clickhouse")

        @task
        def load_mongodb() -> dict:
            return load_target(dataset, "mongodb")

        @task
        def load_redis() -> dict:
            return load_target(dataset, "redis")

        @task
        def verify_all() -> list[dict]:
            return verify_dataset(dataset)

        extract = extract_and_validate()
        verify = verify_all()

        for load in [
            load_postgres(),
            load_clickhouse(),
            load_mongodb(),
            load_redis(),
        ]:
            extract >> load >> verify

    return pipeline()
