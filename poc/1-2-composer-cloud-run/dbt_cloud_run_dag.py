from datetime import datetime

from airflow import DAG

from airflow.providers.google.cloud.operators.cloud_run import (
    CloudRunExecuteJobOperator,
)

from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCheckOperator
)


PROJECT_ID = "sturdy-practice-245323"
REGION = "europe-west2"
JOB_NAME = "dbt-bigquery-job"


with DAG(
        dag_id="dbt_cloud_run_poc",

        start_date=datetime(2026, 1, 1),

        schedule=None,

        catchup=False,

        tags=[
            "dbt",
            "bigquery",
            "cloud-run"
        ],

) as dag:

    check_source = BigQueryCheckOperator(
        task_id="check_source",

        sql="""
        SELECT COUNT(*) > 0
        FROM `sturdy-practice-245323.dbt_poc.table_a`
    """,

        use_legacy_sql=False,

        location="europe-west2",
    )

    run_dbt = CloudRunExecuteJobOperator(
        task_id="run_dbt",

        project_id=PROJECT_ID,

        region=REGION,

        job_name=JOB_NAME,
    )

    validate_target = BigQueryCheckOperator(
        task_id="validate_target",

        sql="""
        SELECT COUNT(*) > 0
        FROM `sturdy-practice-245323.dbt_poc.table_b`
    """,

        use_legacy_sql=False,

        location="europe-west2",
    )


    check_source >> run_dbt >> validate_target