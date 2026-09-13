from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime

DBT_DIR = "/home/airflow/gcs/data/dbt_poc"

with DAG(
        dag_id="dbt_bigquery_poc",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["dbt", "bigquery", "poc"],
) as dag:
    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=f"""
        cd {DBT_DIR} &&
        dbt deps
        """
    )

    dbt_debug = BashOperator(
        task_id="dbt_debug",
        bash_command=f"""
        cd {DBT_DIR} &&
        dbt debug --profiles-dir .
        """
    )

    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=f"""
        cd {DBT_DIR} &&
        dbt run \
          --select transform_table_b \
          --profiles-dir .
        """
    )

    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"""
        cd {DBT_DIR} &&
        dbt test \
          --select transform_table_b \
          --profiles-dir .
        """
    )

    dbt_deps >> dbt_debug >> dbt_run >> dbt_test
