FROM apache/airflow:3.1.7

COPY --chown=airflow:root lab1/docker-requirements.txt /tmp/lab1-requirements.txt
RUN pip install --no-cache-dir "apache-airflow==3.1.7" -r /tmp/lab1-requirements.txt

COPY --chown=airflow:root lab1/src/rbc_pipeline /opt/airflow/rbc_pipeline
ENV PYTHONPATH=/opt/airflow
