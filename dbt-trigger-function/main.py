import functions_framework

from google.cloud import run_v2

PROJECT_ID = "sturdy-practice-245323"
REGION = "europe-west2"
JOB_NAME = "dbt-bigquery-job"


@functions_framework.CloudEvent
def trigger_dbt(cloud_event):
    data = cloud_event.data

    bucket = data["bucket"]
    file_name = data["name"]

    print(f"Received file: gs://{bucket}/{file_name}")

    client = run_v2.JobsClient()

    job_name = (
        f"projects/{PROJECT_ID}/"
        f"locations/{REGION}/"
        f"jobs/{JOB_NAME}"
    )

    request = run_v2.RunJobRequest(
        name=job_name
    )

    operation = client.run_job(
        request=request
    )

    print(f"Cloud Run Job triggered: {operation.operation.name}")
