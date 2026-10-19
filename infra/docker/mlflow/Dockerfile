# MLflow tracking server for ECS (pv-forecasting-mlflow-service).
# Official image pinned to the MLflow version the backend uses (apps/backend/uv.lock: 3.17.0),
# plus the drivers the server needs: psycopg (v3, used by postgresql+psycopg:// store URIs) and
# psycopg2 for the Postgres backend store, and boto3 for the Supabase S3-compatible artifact root. The command and env are set by the ECS task definition.
FROM ghcr.io/mlflow/mlflow:v3.17.0

RUN pip install --no-cache-dir \
        "psycopg[binary]>=3.2,<4" \
        "psycopg2-binary==2.9.10" \
        "boto3>=1.35,<2" \
    && python -c "import mlflow, flask, psycopg, psycopg2, boto3; print('mlflow', mlflow.__version__)"

EXPOSE 5000

CMD ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000"]