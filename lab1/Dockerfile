FROM apache/airflow:3.1.7

USER airflow

COPY --chown=airflow:root lab1/docker-requirements.txt /tmp/lab1-requirements.txt

RUN pip install --no-cache-dir \
    "apache-airflow==3.1.7" \
    -r /tmp/lab1-requirements.txt