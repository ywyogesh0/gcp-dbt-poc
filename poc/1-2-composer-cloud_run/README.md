Yes. This is the right enhancement because it gives each component a clean responsibility:

```text
Composer = orchestration
Cloud Run Job = dbt runtime
dbt = transformation definition
BigQuery = SQL compute/storage
```

## Enhanced Option 2

Instead of Option 1:

```text
Composer Worker
      │
      ▼
   dbt CLI
      │
      ▼
   BigQuery
```

we'll implement:

```text
                         CLOUD COMPOSER
                               │
                         Airflow DAG
                               │
              ┌────────────────┼─────────────────┐
              ▼                ▼                 ▼
        Check source      Trigger Run       Validate
                             Job
                               │
                               ▼
                       CLOUD RUN JOB
                               │
                         dbt container
                               │
                         dbt build
                               │
                               ▼
                           BIGQUERY
                               │
                       TABLE_A → TABLE_B
```

This is much closer to the architecture I'd use for a real platform.

---

# 1. What we already have

From Option 2:

```text
Artifact Registry
      │
      │ dbt-bigquery:v1
      ▼
Cloud Run Job
      │
      │ dbt-bigquery-job
      ▼
dbt build
      │
      ▼
BigQuery
TABLE_A → TABLE_B
```

And we have a service account:

```text
dbt-runner@PROJECT_ID.iam.gserviceaccount.com
```

The important change is that **Composer will no longer execute dbt**.

Composer only tells Cloud Run:

> Execute `dbt-bigquery-job`.

---

# 2. Two identities

Now we need to distinguish two service accounts.

```text
                    Composer
                       │
                       │ runs as
                       ▼
              composer-runner SA
                       │
                       │ execute job
                       ▼
                 Cloud Run Job
                       │
                       │ runs as
                       ▼
                 dbt-runner SA
                       │
                       ▼
                    BigQuery
```

This is an important security boundary.

### Composer service account

Something like:

```text
composer-runner@PROJECT_ID.iam.gserviceaccount.com
```

It needs permission to invoke the Cloud Run Job.

### dbt service account

```text
dbt-runner@PROJECT_ID.iam.gserviceaccount.com
```

It needs BigQuery permissions.

Therefore we don't give Composer unnecessary BigQuery write access merely because dbt needs it.

Conceptually:

```text
Composer SA
    │
    └── Run Jobs Executor
              │
              ▼
         Cloud Run Job


Cloud Run dbt SA
    │
    ├── BigQuery Job User
    ├── source read
    └── target write
```

That's a much cleaner least-privilege design.

---

# 3. DAG design

Let's make our Composer DAG more realistic.

```text
START
  │
  ▼
CHECK TABLE_A
  │
  ▼
TRIGGER DBT CLOUD RUN JOB
  │
  ▼
WAIT FOR COMPLETION
  │
  ▼
CHECK TABLE_B
  │
  ▼
SUCCESS
```

But the Cloud Run Airflow operator can handle the execution/waiting behaviour for us, so logically:

```text
check_source
      │
      ▼
run_dbt_cloud_run
      │
      ▼
validate_target
```

Later we'll evolve this to:

```text
                       check_source
                            │
                            ▼
                      run_dbt_job
                            │
                ┌───────────┴───────────┐
                │                       │
             success                  failure
                │                       │
                ▼                       ▼
         validate_target          failure_handler
                │                       │
                ▼                       ▼
         quality_checks              alert
                │
                ▼
             success
```

---

# 4. Composer provider

Composer needs the Google Cloud provider containing the Cloud Run operators.

In modern Composer environments, Google provider packages are normally already part of the environment, but check your environment/package version rather than blindly installing another version.

The operator we want conceptually is:

```python
CloudRunExecuteJobOperator
```

This is much better than doing:

```python
BashOperator(
    bash_command="gcloud run jobs execute ..."
)
```

because we're using an Airflow operator designed to interact with the Google service rather than shelling out to `gcloud`.

---

# 5. Create the first DAG

Create:

```text
dags/
└── dbt_cloud_run_dag.py
```

Start with:

```python
from datetime import datetime

from airflow import DAG

from airflow.providers.google.cloud.operators.cloud_run import (
    CloudRunExecuteJobOperator,
)


PROJECT_ID = "YOUR_PROJECT_ID"
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

    run_dbt = CloudRunExecuteJobOperator(
        task_id="run_dbt",

        project_id=PROJECT_ID,

        region=REGION,

        job_name=JOB_NAME,
    )


    run_dbt
```

That's enough for our first test.

---

# 6. What happens when you trigger the DAG?

In Airflow:

```text
Trigger DAG
    │
    ▼
run_dbt
```

The operator calls the Cloud Run API:

```text
Composer
    │
    │ Cloud Run API
    ▼
Cloud Run
    │
    ▼
dbt-bigquery-job
    │
    ▼
container
    │
    ▼
dbt build
    │
    ▼
BigQuery
```

Composer does **not** need to:

```text
pip install dbt-bigquery
```

anymore.

That's one of the biggest improvements.

---

# 7. Composer waits for Cloud Run

Suppose dbt takes 8 minutes.

Conceptually:

```text
13:00

Composer
run_dbt
   │
   ├──── Cloud Run started
   │
   │
   │          dbt running...
   │
   │
   │
   │
   │
   └──── Cloud Run completed
                       │
                       ▼
                     SUCCESS

13:08
```

If dbt returns a non-zero exit status:

```text
Cloud Run
   │
   ▼
FAILED
   │
   ▼
Composer task
   │
   ▼
FAILED
```

Therefore failure propagates cleanly:

```text
dbt failure
     ↓
container failure
     ↓
Cloud Run execution failure
     ↓
Airflow task failure
     ↓
DAG failure
```

That's exactly what we want operationally.

---

# 8. Add source validation

Now let's make the DAG useful.

Before running dbt, verify `table_a` contains data.

Use a BigQuery operator.

Conceptually:

```python
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCheckOperator
)
```

Then:

```python
check_source = BigQueryCheckOperator(
    task_id="check_source",

    sql="""
        SELECT COUNT(*) > 0
        FROM `YOUR_PROJECT_ID.dbt_poc.table_a`
    """,

    use_legacy_sql=False,

    location="europe-west2",
)
```

Dependency:

```python
check_source >> run_dbt
```

Now:

```text
TABLE_A empty
     │
     ▼
check_source ✗
     │
     X
Cloud Run never starts
```

This prevents unnecessary dbt execution.

---

# 9. Add target validation

After Cloud Run succeeds:

```python
validate_target = BigQueryCheckOperator(
    task_id="validate_target",

    sql="""
        SELECT COUNT(*) > 0
        FROM `YOUR_PROJECT_ID.dbt_poc.table_b`
    """,

    use_legacy_sql=False,

    location="europe-west2",
)
```

Dependency:

```python
check_source >> run_dbt >> validate_target
```

Our DAG becomes:

```text
┌─────────────────┐
│  check_source   │
│                 │
│ TABLE_A exists  │
│ and has data    │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│      run_dbt            │
│                         │
│ Trigger Cloud Run Job   │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│    validate_target      │
│                         │
│ Verify TABLE_B          │
└─────────────────────────┘
```

---

# 10. But there's an architectural question

You might reasonably ask:

> Why have Composer validate `table_b` if dbt already has tests?

Exactly.

We should separate two kinds of validation.

### dbt tests

Inside the dbt container:

```text
dbt build

model
  +
tests
```

Validate things such as:

```text
customer_id NOT NULL
customer_id UNIQUE
final_amount NOT NULL
```

### Composer checks

Validate **pipeline-level conditions**, such as:

```text
Source partition arrived?
        ↓
Run dbt
        ↓
Target partition produced?
```

Therefore:

```text
Composer
    =
workflow validation

dbt
    =
data/model validation
```

That's the better separation.

---

# 11. Make the pipeline date-driven

This is where Composer becomes genuinely valuable.

Imagine this DAG runs daily:

```text
2026-09-13
```

We want to process:

```text
processing_date = 2026-09-13
```

Composer already knows its logical execution date.

So:

```text
Airflow
 logical date
     │
     ▼
2026-09-13
     │
     ▼
Cloud Run
     │
     ▼
dbt
     │
     ▼
BigQuery partition
2026-09-13
```

Instead of hardcoding the date.

---

# 12. Parameterise dbt

Change the model:

```sql
{{ config(
    materialized='table',
    alias='table_b'
) }}

SELECT

    customer_id,

    UPPER(customer_name) AS customer_name,

    transaction_amount,

    ROUND(transaction_amount * 0.20, 2)
        AS vat_amount,

    ROUND(transaction_amount * 1.20, 2)
        AS final_amount,

    transaction_timestamp,

    CURRENT_TIMESTAMP()
        AS dbt_processed_timestamp

FROM {{ source('source', 'table_a') }}

WHERE
    transaction_amount > 0

AND DATE(transaction_timestamp)
    = DATE('{{ var("processing_date") }}')
```

Now dbt accepts:

```bash
dbt build \
    --select transform_table_b \
    --vars '{"processing_date":"2026-09-13"}'
```

---

# 13. Composer passes the date to Cloud Run

This is the interesting bit.

Instead of Cloud Run always running the container's default command, Composer can override execution arguments.

Conceptually:

```python
run_dbt = CloudRunExecuteJobOperator(

    task_id="run_dbt",

    project_id=PROJECT_ID,

    region=REGION,

    job_name=JOB_NAME,

    overrides={
        "container_overrides": [
            {
                "args": [
                    "build",
                    "--select",
                    "transform_table_b",
                    "--vars",
                    '{"processing_date":"{{ ds }}"}'
                ]
            }
        ]
    },
)
```

Airflow templates:

```text
{{ ds }}
```

into the DAG's logical date.

For a run representing 13 September:

```text
{{ ds }}

    ↓

2026-09-13
```

and the Cloud Run execution effectively becomes:

```bash
dbt build \
    --select transform_table_b \
    --vars '{"processing_date":"2026-09-13"}'
```

One implementation detail: the exact override structure supported by `CloudRunExecuteJobOperator` depends on the Google provider version installed in your Composer environment, so verify that version's operator signature before copying the override block unchanged.

---

# 14. Change Docker ENTRYPOINT

For parameterisation, I'd modify our Dockerfile.

Instead of:

```dockerfile
CMD ["dbt", "build"]
```

use:

```dockerfile
ENTRYPOINT ["dbt"]
CMD ["build"]
```

Now:

```text
ENTRYPOINT
dbt
```

and Cloud Run provides:

```text
build
--select
transform_table_b
--vars
...
```

Result:

```bash
dbt build \
 --select transform_table_b \
 --vars ...
```

Much cleaner.

---

# 15. Daily schedule

Once manual execution works, change:

```python
schedule=None
```

to something like:

```python
schedule="0 2 * * *"
```

Meaning daily at 02:00 according to the DAG's configured timezone.

Then:

```text
                   Every day
                     02:00
                       │
                       ▼
                  Composer
                       │
                check source
                       │
                       ▼
                  Cloud Run
                       │
                       ▼
                      dbt
                       │
                       ▼
                    BigQuery
```

For production, I'd explicitly configure the DAG timezone rather than relying on assumptions around UTC/local time.

---

# 16. Source partition check

Let's improve our earlier source check.

Don't just ask:

```sql
COUNT(*) > 0
```

across the whole table.

Check the processing date:

```python
check_source = BigQueryCheckOperator(

    task_id="check_source",

    sql="""
        SELECT COUNT(*) > 0

        FROM
          `YOUR_PROJECT_ID.dbt_poc.table_a`

        WHERE DATE(transaction_timestamp)
              = DATE('{{ ds }}')
    """,

    use_legacy_sql=False,

    location="europe-west2",
)
```

Now:

```text
Composer logical date
        │
        ▼
    2026-09-13
        │
        ▼
Does TABLE_A contain
2026-09-13 data?
        │
    ┌───┴────┐
    │        │
   YES       NO
    │        │
    ▼        ▼
run dbt    FAIL/WAIT
```

---

# 17. In production, don't immediately fail if data is late

This is another reason Composer is useful.

Suppose upstream data normally arrives around 01:45 but occasionally at 02:15.

If the DAG starts at 02:00:

```text
02:00

check_source

   ↓

No data

   ↓

FAIL
```

isn't ideal.

Instead use a sensor/retry strategy:

```text
02:00
 │
 ▼
Check TABLE_A
 │
 └── no data
       │
       ▼
      wait
       │
02:05 check
       │
       └── no
            │
02:10 check
            │
            ...
02:15 data arrives
            │
            ▼
          dbt
```

Composer handles this sort of dependency management much better than making dbt itself poll for source availability.

---

# 18. Complete logical DAG

Our enhanced design becomes:

```text
                         COMPOSER
                            │
                            ▼
                  ┌──────────────────┐
                  │ Wait for source  │
                  │ partition        │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Validate source  │
                  │ row count etc.   │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Cloud Run Job    │
                  │                  │
                  │ dbt build        │
                  └────────┬─────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  BigQuery   │
                    │             │
                    │ TABLE_A     │
                    │    │        │
                    │    ▼        │
                    │ TABLE_B     │
                    └──────┬──────┘
                           │
                           ▼
                  ┌─────────────────┐
                  │ Validate target │
                  │ partition       │
                  └────────┬────────┘
                           │
                           ▼
                        SUCCESS
```

---

# 19. Failure propagation

Suppose `customer_id` becomes null.

```text
Composer
   │
   ▼
check source ✓
   │
   ▼
Cloud Run
   │
   ▼
dbt build
   │
   ├── model ✓
   │
   └── not_null test ✗
              │
              ▼
        dbt exit != 0
              │
              ▼
       Cloud Run FAILED
              │
              ▼
       Airflow task FAILED
              │
              X
       validate_target
```

Composer now gives us a single orchestration view of the failure.

---

# 20. Retries should live at the correct level

Don't blindly configure:

```text
Composer retries = 3
Cloud Run retries = 3
dbt retry logic = ...
```

because you can accidentally create:

```text
3 × 3 = 9
```

pipeline executions.

I'd initially use:

```text
Composer
retries = 1

Cloud Run
max retries = 0
```

and make the dbt transformation idempotent.

Then Composer owns orchestration-level retries.

---

# 21. Final service-account architecture

This is important enough to put into your POC diagram.

```text
                   COMPOSER
                      │
                      │
            composer-runner SA
                      │
                      │ permission:
                      │ execute Run Job
                      ▼
                CLOUD RUN JOB
                      │
                      │
                 dbt-runner SA
                      │
                      │ permissions:
                      │ BQ Job User
                      │ source read
                      │ target write
                      ▼
                   BIGQUERY
```

Not:

```text
One giant service account
with every permission
```

That will be a good security talking point in your architecture presentation.

---

# 22. And this prepares us perfectly for Cloud Build

Our architecture now looks like this:

```text
                   ┌──────────────────┐
                   │   Source Code    │
                   │                  │
                   │ dbt project      │
                   │ Dockerfile       │
                   │ Composer DAG     │
                   └────────┬─────────┘
                            │
                      manual build
                            │
                            ▼
                   Artifact Registry
                            │
                            ▼
Composer ───────────► Cloud Run Job
                            │
                            ▼
                           dbt
                            │
                            ▼
                         BigQuery
```

There's now one obvious weakness:

> **We're manually building and deploying the container.**

That's exactly what **Option 3 — Cloud Build** should solve:

```text
Git commit
     │
     ▼
Cloud Build
     │
     ├── dbt compile
     ├── dbt validation
     ├── Docker build
     ├── push Artifact Registry
     └── update Cloud Run Job
                         │
                         ▼
                      Composer
                         │
                         ▼
                    Cloud Run
                         │
                         ▼
                        dbt
                         │
                         ▼
                      BigQuery
```

So after this enhancement, **Composer owns WHEN and dependency orchestration; Cloud Run owns WHERE dbt executes; dbt owns WHAT transformation is performed; BigQuery owns the compute; and Cloud Build will own HOW a new version gets deployed.**
