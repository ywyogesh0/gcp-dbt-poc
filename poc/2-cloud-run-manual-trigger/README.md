## Option 2 — Cloud Run Job → dbt → BigQuery

Now we'll take the **same dbt project from Option 1**, package it as a Docker container, and execute it as a **Cloud Run Job**.

This is a particularly good fit for dbt because dbt is a finite batch workload: start → transform → test → exit.

### Architecture

```text
                         Trigger
                            │
                            ▼
                    ┌────────────────┐
                    │ Cloud Run Job  │
                    │                │
                    │ dbt container  │
                    └───────┬────────┘
                            │
                         dbt build
                            │
                            ▼
                    ┌────────────────┐
                    │    BigQuery    │
                    │                │
                    │    TABLE_A     │
                    │       │        │
                    │       ▼        │
                    │ transformation │
                    │       │        │
                    │       ▼        │
                    │    TABLE_B     │
                    └────────────────┘
```

The important distinction is:

```text
Cloud Run Job = runs dbt

dbt           = compiles/manages transformation

BigQuery      = actually executes the SQL
```

---

# 1. Reuse our dbt project

From Option 1:

```text
dbt_composer_poc/
│
├── dbt_project.yml
├── profiles.yml
├── requirements.txt
│
└── models/
    ├── sources.yml
    ├── schema.yml
    └── transform_table_b.sql
```

Our model remains:

```sql
{{ config(
    materialized='table',
    alias='table_b'
) }}

SELECT
    customer_id,
    UPPER(customer_name) AS customer_name,
    transaction_amount,
    ROUND(transaction_amount * 0.20, 2) AS vat_amount,
    ROUND(transaction_amount * 1.20, 2) AS final_amount,
    transaction_timestamp,
    CURRENT_TIMESTAMP() AS dbt_processed_timestamp

FROM {{ source('source', 'table_a') }}

WHERE transaction_amount > 0
```

Nothing about the transformation needs to know that Cloud Run is executing it.

That's one of the architectural benefits.

---

# 2. Change authentication slightly

Our container will run using the **Cloud Run Job's service account**.

So the flow becomes:

```text
Cloud Run Job
      │
      │ runs as
      ▼
dbt-runner@PROJECT_ID.iam.gserviceaccount.com
      │
      │ IAM
      ▼
BigQuery
```

No service-account JSON file should be embedded in the Docker image.

Your `profiles.yml` can therefore use:

```yaml
dbt_composer_poc:

  target: prod

  outputs:

    prod:
      type: bigquery
      method: oauth
      project: YOUR_PROJECT_ID
      dataset: dbt_poc
      location: europe-west2
      threads: 4
```

The Google authentication libraries can obtain credentials from the runtime environment.

---

# 3. Create the service account

For example:

```bash
gcloud iam service-accounts create dbt-runner \
    --display-name="dbt Cloud Run Runner"
```

Which creates:

```text
dbt-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

For our POC it needs the ability to:

```text
submit BigQuery jobs
        +
read TABLE_A
        +
write TABLE_B
```

Conceptually:

```text
dbt-runner
    │
    ├── BigQuery Job User
    │
    ├── BigQuery Data Viewer
    │
    └── BigQuery Data Editor
```

For a production system, I'd grant dataset-level permissions wherever possible instead of broad project-level data access.

---

# 4. Create `requirements.txt`

Inside the dbt project:

```text
dbt-bigquery==<PINNED_VERSION>
```

Use a dbt-bigquery version you've tested rather than relying on an unpinned latest version.

Your project is now:

```text
dbt_composer_poc/
│
├── Dockerfile             ← NEW
├── requirements.txt       ← NEW
├── dbt_project.yml
├── profiles.yml
│
└── models/
```

---

# 5. Create the Dockerfile

Here's a simple POC Dockerfile:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DBT_PROFILES_DIR=/app

CMD ["dbt", "build", "--select", "transform_table_b"]
```

Why `dbt build` instead of only `dbt run`?

Because:

```text
dbt run
   │
   └── models


dbt build
   │
   ├── models
   ├── tests
   ├── seeds
   └── snapshots
```

For our simple project:

```text
Cloud Run
    │
    ▼
dbt build
    │
    ├── transform_table_b
    │
    └── tests
```

So we don't need separate `dbt run` and `dbt test` commands.

---

# 6. Test the container locally

Before Cloud Run, build it locally:

```bash
docker build -t dbt-bigquery-poc .
```

Check:

```bash
docker images
```

You should see something like:

```text
REPOSITORY          TAG
dbt-bigquery-poc    latest
```

Then you can test:

```bash
docker run --rm \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    dbt-bigquery-poc
```

The credential approach here is just for local testing.

In Cloud Run, we won't mount credentials.

---

# 7. Create Artifact Registry

Cloud Run needs somewhere to retrieve our container image.

Architecture becomes:

```text
Developer
    │
    │ docker build
    ▼
Container Image
    │
    │ push
    ▼
Artifact Registry
    │
    │ pull
    ▼
Cloud Run Job
```

Enable the APIs:

```bash
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com
```

Create a Docker repository:

```bash
gcloud artifacts repositories create dbt-poc \
    --repository-format=docker \
    --location=europe-west2 \
    --description="dbt POC containers"
```

Now we have:

```text
Artifact Registry

europe-west2
    │
    └── dbt-poc
```

---

# 8. Build the image using Cloud Build

We don't actually need Docker installed locally.

From your dbt project directory:

path: europe-west2-docker.pkg.dev/sturdy-practice-245323/dbt-poc

```bash
gcloud builds submit \
    --tag europe-west2-docker.pkg.dev/sturdy-practice-245323/dbt-poc/dbt-bigquery:v1
```

This does:

```text
Your source
    │
    ▼
Cloud Build
    │
    ├── reads Dockerfile
    │
    ├── builds container
    │
    └── pushes image
    │
    ▼
Artifact Registry
```

Notice Cloud Build appears here, but this is **not yet our Option 3 Cloud Build POC**.

We're simply using Cloud Build as a convenient image builder.

In Option 3 we'll turn it into a proper CI/CD pipeline.

---

# 9. Check Artifact Registry

Go to:

**Artifact Registry → Repositories → dbt-poc**

You should see:

```text
dbt-bigquery

Tag:
v1
```

Image:

```text
europe-west2-docker.pkg.dev/
YOUR_PROJECT_ID/
dbt-poc/
dbt-bigquery:v1
```

---

# 10. Create the Cloud Run Job

Now create the actual job:

```bash
gcloud run jobs create dbt-bigquery-job \
    --image=europe-west2-docker.pkg.dev/YOUR_PROJECT_ID/dbt-poc/dbt-bigquery:v1 \
    --region=europe-west2 \
    --service-account=dbt-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --memory=1Gi \
    --cpu=1 \
    --max-retries=0 \
    --task-timeout=30m
```

We now have:

```text
Cloud Run

dbt-bigquery-job
      │
      ├── Image
      │    └── dbt-bigquery:v1
      │
      ├── CPU: 1
      ├── RAM: 1 GiB
      │
      └── Identity
           │
           ▼
       dbt-runner
```

---

# 11. Execute the job

Run:

```bash
gcloud run jobs execute dbt-bigquery-job \
    --region=europe-west2 \
    --wait
```

Now the interesting chain begins:

```text
gcloud
   │
   ▼
Cloud Run Job
   │
   ▼
Container starts
   │
   ▼
CMD
   │
   ▼
dbt build --select transform_table_b
   │
   ▼
dbt reads project
   │
   ▼
dbt compiles SQL
   │
   ▼
BigQuery Job
   │
   ▼
TABLE_A
   │
   │ transform
   ▼
TABLE_B
   │
   ▼
dbt tests
   │
   ▼
Container exits
```

Successful container exit:

```text
exit code = 0
```

means the Cloud Run execution succeeds.

If dbt fails:

```text
exit code != 0
```

Cloud Run marks the execution as failed.

That's exactly the behaviour we want.

---

# 12. Inspect Cloud Run logs

Go to:

**Cloud Run → Jobs → dbt-bigquery-job → Executions**

Select an execution.

You should see dbt output similar to:

```text
Running with dbt=...
Registered adapter: bigquery=...

Found 1 model
Found 3 data tests

START table model transform_table_b

OK created table model transform_table_b

START test not_null...
PASS

START test unique...
PASS

Completed successfully
```

This gives us operational visibility:

```text
Cloud Run Execution
        │
        ├── Started
        ├── Running
        ├── Logs
        ├── Duration
        └── Success / Failed
```

---

# 13. Verify BigQuery

Run:

```sql
SELECT *
FROM `YOUR_PROJECT_ID.dbt_poc.table_b`
ORDER BY customer_id;
```

Expected:

| customer | amount |  VAT | final |
| -------- | -----: | ---: | ----: |
| YOGESH   |    100 |   20 |   120 |
| JOHN     |    250 |   50 |   300 |
| SARAH    |   75.5 | 15.1 |  90.6 |
| ANNA     |    500 |  100 |   600 |

So Option 2 is complete:

```text
Cloud Run
     │
     ▼
dbt container
     │
     ▼
BigQuery
     │
TABLE_A
     │
     ▼
TABLE_B
```

---

# 14. Make the container configurable

Now let's make it more realistic.

Hardcoding this:

```dockerfile
CMD ["dbt", "build", "--select", "transform_table_b"]
```

isn't ideal.

Instead:

```dockerfile
CMD ["dbt", "build"]
```

Then Cloud Run can override the arguments.

For example, one container image can run:

```text
customer_model
payment_model
settlement_model
reconciliation_model
```

instead of building four Docker images.

Architecture:

```text
                  dbt-bigquery:v1
                         │
            ┌────────────┼─────────────┐
            ▼            ▼             ▼
       Cloud Run     Cloud Run     Cloud Run
         Job A         Job B         Job C
            │            │             │
            ▼            ▼             ▼
        customer      payments     settlement
```

Same code/image, different execution arguments.

That's much more reusable.

---

# 15. Pass dbt variables

Suppose we only want today's partition.

Our dbt model could use:

```sql
WHERE DATE(transaction_timestamp)
      = DATE('{{ var("processing_date") }}')
```

Then:

```bash
dbt build \
  --select transform_table_b \
  --vars '{"processing_date": "2026-09-13"}'
```

Cloud Run can therefore become parameterised:

```text
Cloud Run Job
      │
      │ processing_date
      ▼
dbt
      │
      ▼
BigQuery

WHERE processing_date =
2026-09-13
```

This becomes particularly useful when Composer eventually invokes Cloud Run:

```text
Airflow execution date
        │
        ▼
2026-09-13
        │
        ▼
Cloud Run Job
        │
        ▼
dbt --vars
        │
        ▼
2026-09-13 partition
```

That's close to how you'd structure a production data pipeline.

---

# 16. Failure test

We should deliberately break the data.

Insert:

```sql
INSERT INTO `YOUR_PROJECT_ID.dbt_poc.table_a`
VALUES
(
    NULL,
    'Bad Customer',
    100,
    CURRENT_TIMESTAMP()
);
```

Run Cloud Run again.

The model itself may succeed:

```text
TABLE_B created ✓
```

but:

```yaml
customer_id:
    tests:
      - not_null
```

fails.

Therefore:

```text
dbt build
    │
    ├── model ✓
    │
    └── test ✗
           │
           ▼
     dbt exits non-zero
           │
           ▼
     Cloud Run Job
           │
           ▼
        FAILED
```

This is a valuable POC demonstration because it proves Cloud Run isn't merely launching a script—it can propagate dbt's pipeline status to the orchestration layer.

---

# 17. What about retries?

This deserves care.

Suppose Cloud Run retries:

```text
Attempt 1

dbt writes TABLE_B
       │
       X
container/network failure

       ↓

Attempt 2

dbt writes TABLE_B again
```

For an idempotent dbt model such as:

```sql
CREATE OR REPLACE TABLE
```

that's usually manageable.

But with incremental transformations, retries need more thought.

For our POC I deliberately used:

```text
--max-retries=0
```

Initially.

Once the transformation is demonstrably idempotent, we can consider controlled retries.

Given the sort of `insert_overwrite` partitioned dbt models you've worked with, this becomes particularly important: rerunning the same processing partition should ideally produce the same target state.

---

# 18. Cloud Run Service vs Cloud Run Job

This is an interview/design-review question worth knowing.

Don't create:

```text
Cloud Run Service
```

just to run dbt.

A Service expects request-driven application behaviour:

```text
HTTP
 │
 ▼
Cloud Run Service
 │
 ▼
response
```

dbt is:

```text
START
 │
 ▼
execute
 │
 ▼
finish
 │
 ▼
EXIT
```

Therefore:

**Cloud Run Job is the better semantic fit.**

---

# 19. Option 1 vs Option 2

We now have two implementations.

### Option 1

```text
Composer Worker
      │
      ▼
dbt CLI
      │
      ▼
BigQuery
```

### Option 2

```text
Cloud Run Job
      │
      ▼
dbt container
      │
      ▼
BigQuery
```

And eventually we'll combine them:

```text
                 Composer
                    │
                    │ orchestrates
                    ▼
               Cloud Run Job
                    │
                    │ executes
                    ▼
                   dbt
                    │
                    ▼
                 BigQuery
                    │
              ┌─────┴─────┐
              ▼           ▼
           TABLE_A      TABLE_B
```

That's the architecture I'd prefer over installing dbt directly into every Composer worker.

---

## What we have after Option 2

```text
                     ┌───────────────┐
                     │ Source Code   │
                     │ dbt project   │
                     └───────┬───────┘
                             │
                             ▼
                       Cloud Build
                             │
                             ▼
                    Artifact Registry
                             │
                       dbt image:v1
                             │
                             ▼
                      Cloud Run Job
                             │
                             ▼
                            dbt
                             │
                        BigQuery SQL
                             │
                             ▼
                 ┌───────────────────────┐
                 │       BigQuery        │
                 │                       │
                 │ TABLE_A ───► TABLE_B  │
                 └───────────────────────┘
```

**Option 3 — Cloud Build** is the natural next step because right now we're manually running `gcloud builds submit`. In Option 3, we can turn that into proper CI/CD: **Git commit → dbt validation → Docker build → Artifact Registry → update Cloud Run Job → optionally execute dbt**, including a concrete `cloudbuild.yaml` and the IAM chain.
