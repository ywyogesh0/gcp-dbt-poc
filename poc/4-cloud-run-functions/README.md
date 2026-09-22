## Option 4 — Cloud Run Functions → Cloud Run Job → dbt → BigQuery

Now we'll make the pipeline **event-driven**.

We will **not run dbt inside the function**. The function's responsibility is simply to react to an event and trigger the Cloud Run Job we already built.

```text
Cloud Storage
     │
     │ file arrives
     ▼
  Eventarc
     │
     ▼
Cloud Run Function
     │
     │ trigger
     ▼
Cloud Run Job
     │
     ▼
dbt container
     │
     ▼
BigQuery
TABLE_A → TABLE_B
```

This gives us three trigger mechanisms so far:

```text
Option 2: Manual/API ──────┐
                           │
Composer: Scheduled ───────┼──► Cloud Run Job ► dbt ► BigQuery
                           │
Option 4: Event-driven ────┘
```

### 1. Choose the event

For the POC, let's use a Cloud Storage event:

> When `transactions_YYYY-MM-DD.csv` lands in a bucket, trigger dbt for that processing date.

For example:

```text
gs://dbt-poc-landing/

transactions_2026-09-18.csv
             │
             ▼
processing_date = 2026-09-18
```

In a real architecture, if the file itself populates `TABLE_A`, you'd normally have an ingestion step before dbt:

```text
File
 │
 ▼
ingestion/load
 │
 ▼
TABLE_A
 │
 ▼
dbt
 │
 ▼
TABLE_B
```

For this POC we'll assume `TABLE_A` has already been hydrated when the event is handled.

---

# 2. Why not execute dbt inside the function?

Technically you can package dependencies into a function, but it gives us an unnecessary second dbt runtime:

```text
Function A
 └── dbt installation

Cloud Run Job
 └── dbt installation
```

Instead:

```text
Function
   │
   │ lightweight trigger
   ▼
Cloud Run Job
   │
   │ standard dbt runtime
   ▼
dbt
```

Now Cloud Build deploys **one dbt image**, while Composer, Functions and other systems can all invoke it.

---

# 3. Enable APIs

Enable the relevant services:

```bash
gcloud services enable \
    run.googleapis.com \
    cloudfunctions.googleapis.com \
    eventarc.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com
```

Cloud Run functions use Eventarc for event-driven invocation patterns.

---

# 4. Create landing bucket

For example:

```bash
gcloud storage buckets create \
    gs://sturdy-practice-245323-dbt-poc-landing \
    --location=europe-west2
```

Our event source becomes:

```text
sturdy-practice-245323-dbt-poc-landing
               │
               └── Object finalized
                         │
                         ▼
                       Event
```

We specifically care about **object finalisation**: a new object has successfully been created in the bucket.

---

# 5. Create a dedicated Function identity

Don't make the function run as the dbt service account.

Create:

```bash
gcloud iam service-accounts create dbt-trigger \
    --display-name="dbt Event Trigger"
```

Result:

```text
dbt-trigger@sturdy-practice-245323.iam.gserviceaccount.com
```

Our IAM architecture now becomes:

```text
Composer SA
     │
     └──────┐
            │
            ▼
       Cloud Run Job
            │
            │ runs as
            ▼
      dbt-runner SA
            │
            ▼
         BigQuery


Function
   │
   │ runs as
   ▼
dbt-trigger SA
   │
   │ execute Cloud Run Job
   └──────────────► Cloud Run Job
```

The trigger identity doesn't need permission to edit BigQuery tables.

---

# 6. Function project

Create:

```text
dbt-trigger-function/
│
├── main.py
└── requirements.txt
```

`requirements.txt`:

```text
functions-framework
google-cloud-run
cloudevents
```

The important library is the Cloud Run client library because we're going to call the Run Job API.

---

# 7. First version of the function

`main.py`:

```python
import functions_framework

from google.cloud import run_v2


PROJECT_ID = "sturdy-practice-245323"
REGION = "europe-west2"
JOB_NAME = "dbt-bigquery-job"


@functions_framework.cloud_event
def trigger_dbt(cloud_event):

    data = cloud_event.data

    bucket = data["bucket"]
    filename = data["name"]

    print(f"Received file: gs://{bucket}/{filename}")

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

    print(
        f"Cloud Run Job triggered: {operation.operation.name}"
    )
```

Notice what's missing:

```text
dbt
BigQuery SQL
business transformation
```

That's intentional.

The function only does:

```text
receive event
      ↓
understand event
      ↓
trigger Cloud Run
```

---

# 8. Deploy the function

Conceptually:

```bash
gcloud run deploy dbt-trigger-function \
    --source=. \
    --function=trigger_dbt \
    --base-image=python312 \
    --region=europe-west2 \
    --service-account=dbt-trigger@sturdy-practice-245323.iam.gserviceaccount.com
```

Then create/configure the Cloud Storage Eventarc trigger according to the current Cloud Run function deployment flow.

The event we want is:

```text
google.cloud.storage.object.v1.finalized
```

for:

```text
sturdy-practice-245323-dbt-poc-landing
```

Google documents Cloud Storage `object.v1.finalized` as the event emitted when an object is created/finalised. [Cloud Storage Eventarc events](https://cloud.google.com/eventarc/docs/event-types?utm_source=chatgpt.com)

---

# 9. Test it

Upload:

```bash
gcloud storage cp \
    transactions_2026-09-18.csv \
    gs://sturdy-practice-245323-dbt-poc-landing/
```

Now watch the chain:

```text
transactions_2026-09-18.csv
             │
             ▼
       Cloud Storage
             │
      object.finalized
             │
             ▼
         Eventarc
             │
             ▼
    dbt-trigger-function
             │
             ▼
     Cloud Run API
             │
             ▼
    dbt-bigquery-job
             │
             ▼
         dbt build
             │
             ▼
          BigQuery
             │
       TABLE_A → TABLE_B
```

Check:

**Cloud Run → Jobs → dbt-bigquery-job → Executions**

You should see a new execution created by the event.

---

# 10. Now make it useful: extract processing date

We don't want the function merely to trigger:

```bash
dbt build
```

We want:

```bash
dbt build \
  --select transform_table_b \
  --vars '{"processing_date":"2026-09-18"}'
```

Our filename contains the date:

```text
transactions_2026-09-18.csv
             │
             ▼
          extract
             │
             ▼
        2026-09-18
```

Function:

```python
import re

def extract_processing_date(filename):

    match = re.search(
        r"transactions_(\d{4}-\d{2}-\d{2})\.csv$",
        filename
    )

    if not match:
        raise ValueError(
            f"Unexpected filename: {filename}"
        )

    return match.group(1)
```

Then:

```python
processing_date = extract_processing_date(filename)
```

---

# 11. Pass arguments to Cloud Run

Recall our Dockerfile:

```dockerfile
ENTRYPOINT ["dbt"]
CMD ["build"]
```

We can override the Cloud Run Job's container arguments for this particular execution.

Conceptually:

```python
request = run_v2.RunJobRequest(
    name=job_name,

    overrides=run_v2.RunJobRequest.Overrides(
        container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(
                args=[
                    "build",
                    "--select",
                    "transform_table_b",
                    "--vars",
                    f'{{"processing_date":"{processing_date}"}}'
                ]
            )
        ]
    )
)
```

So:

```text
Storage event

transactions_2026-09-18.csv
            │
            ▼
Function

processing_date
=
2026-09-18
            │
            ▼
Cloud Run

dbt build
--select transform_table_b
--vars
processing_date=2026-09-18
            │
            ▼
BigQuery

process only
2026-09-18
```

This is where the design becomes genuinely useful.

---

# 12. Update our dbt model

From the Composer enhancement, we already made the model parameterised:

```sql
SELECT

    customer_id,

    UPPER(customer_name)
        AS customer_name,

    transaction_amount,

    ROUND(
        transaction_amount * 0.20,
        2
    ) AS vat_amount,

    ROUND(
        transaction_amount * 1.20,
        2
    ) AS final_amount,

    transaction_timestamp,

    CURRENT_TIMESTAMP()
        AS dbt_processed_timestamp

FROM {{ source('source', 'table_a') }}

WHERE transaction_amount > 0

AND DATE(transaction_timestamp)
    = DATE('{{ var("processing_date") }}')
```

The exact same dbt model now works for:

```text
Composer
   │
   │ {{ ds }}
   ▼
processing_date


Function
   │
   │ filename
   ▼
processing_date
```

That's excellent reuse.

---

# 13. Composer and Functions now coexist

This is important.

We haven't replaced Composer.

We now have two legitimate invocation models.

### Scheduled

```text
02:00
  │
  ▼
Composer
  │
  ▼
Cloud Run
  │
  ▼
dbt
```

### Event-driven

```text
File arrives
    │
    ▼
Eventarc
    │
    ▼
Function
    │
    ▼
Cloud Run
    │
    ▼
dbt
```

Same Cloud Run Job.

Same container.

Same dbt project.

Same BigQuery tables.

---

# 14. Which one should actually be used?

It depends on the requirement.

If the business says:

> Process every day at 02:00.

Use:

```text
Composer
```

If the business says:

> Process as soon as the file arrives.

Use:

```text
Eventarc
   ↓
Function
```

If they say:

> Wait for files A, B and C, verify yesterday's upstream job, run three dbt models in sequence and send an alert if reconciliation fails.

That's workflow orchestration:

```text
Composer
```

A function should not gradually become a home-made orchestration engine.

---

# 15. Important problem: duplicate events

This is one of the most useful engineering lessons in this POC.

Event-driven systems must assume duplicate delivery can occur.

Imagine:

```text
Storage event
     │
     ├──── event 1
     │
     └──── event 1 duplicate
```

Without protection:

```text
Function
  │
  ├── Cloud Run execution #1
  │
  └── Cloud Run execution #2
```

Now dbt runs twice for:

```text
2026-09-18
```

Therefore our data pipeline should be **idempotent**.

For example, if you're using a partitioned incremental model with `insert_overwrite`, processing the same partition again should converge to the same target state rather than duplicating rows. That's especially relevant to the partition-overwrite pattern you've been working with in BigQuery/dbt.

---

# 16. Add event filtering

Don't trigger dbt for every random object.

Suppose the bucket gets:

```text
transactions_2026-09-18.csv

README.txt

manifest.json

transactions_2026-09-18.failed

archive.zip
```

Our function should accept only:

```text
transactions_YYYY-MM-DD.csv
```

For example:

```python
pattern = r"^transactions_\d{4}-\d{2}-\d{2}\.csv$"

if not re.match(pattern, filename):

    print(
        f"Ignoring unsupported file: {filename}"
    )

    return
```

Now:

```text
README.txt
    │
    ▼
Function
    │
    ▼
IGNORE


transactions_2026-09-18.csv
    │
    ▼
Function
    │
    ▼
TRIGGER
```

Where possible, also push coarse filtering into the Eventarc trigger itself so unwanted events don't invoke your function unnecessarily.

---

# 17. But there is another race condition

This is more subtle.

Suppose:

```text
File arrives
    │
    ▼
Function immediately starts dbt
```

but `TABLE_A` is populated by another ingestion pipeline:

```text
File
 │
 ├──────────────► Function → dbt
 │
 └──────────────► ingestion → TABLE_A
```

dbt could start **before ingestion finishes**.

That's bad.

You want:

```text
File
 │
 ▼
Ingestion
 │
 ▼
TABLE_A ready
 │
 ▼
READY event
 │
 ▼
Function
 │
 ▼
dbt
```

This distinction is important.

The event should represent:

> **Data is ready for transformation**

not merely:

> **Something appeared in storage.**

---

# 18. A better real-world event design

For example:

```text
Cloud Storage
 transactions.csv
       │
       ▼
Dataflow / load job
       │
       ▼
BigQuery TABLE_A
       │
       ▼
Publish Pub/Sub
"TABLE_A_READY"
       │
       ▼
Eventarc
       │
       ▼
Function
       │
       ▼
Cloud Run dbt Job
       │
       ▼
TABLE_B
```

Event payload:

```json
{
  "event": "TABLE_A_READY",
  "processing_date": "2026-09-18",
  "source": "transactions",
  "row_count": 8242513
}
```

Now the trigger has real semantic meaning.

---

# 19. Function should not wait for dbt

Another important distinction.

We generally don't want:

```text
Function
   │
   ▼
start Cloud Run
   │
   ▼
wait 25 minutes
   │
   ▼
dbt finishes
   │
   ▼
Function finishes
```

Instead:

```text
Function
   │
   ▼
start Cloud Run Job
   │
   ▼
receive operation
   │
   ▼
return
```

The Cloud Run Job owns the long-running batch execution.

This keeps the function lightweight.

---

# 20. Then who monitors completion?

This depends on the architecture.

If it's a simple event-driven transformation:

```text
Cloud Run Job
    │
    ▼
Cloud Logging / Monitoring
```

may be sufficient.

For a workflow requiring downstream steps:

```text
dbt
 ↓
quality
 ↓
reconciliation
 ↓
report generation
 ↓
notification
```

we're back in orchestration territory.

Use Composer rather than making Functions coordinate all of that.

---

# 21. Best hybrid architecture

We can actually combine event-driven triggering with Composer.

Instead of:

```text
Event
 ↓
Function
 ↓
Cloud Run
```

you can have:

```text
Event
 ↓
Function
 ↓
trigger Composer DAG
 ↓
Composer
 ↓
Cloud Run
 ↓
dbt
```

Why?

Because the event controls **when the workflow begins**, while Composer controls the multi-step workflow.

```text
                  DATA READY EVENT
                         │
                         ▼
                     Function
                         │
                         ▼
                     Composer
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
       source check    dbt run    validation
                         │
                         ▼
                    Cloud Run
```

For a large enterprise pipeline, this is often more maintainable than letting the function orchestrate everything.

For our POC, however, direct:

```text
Function → Cloud Run
```

is perfect because it demonstrates the Function use case clearly.

---

# 22. Cloud Build now deploys two things

Our Option 3 CI/CD can now expand.

Previously:

```text
Cloud Build
    │
    └── deploy dbt Cloud Run Job
```

Now:

```text
Cloud Build
        │
        ├───────────────┐
        │               │
        ▼               ▼
dbt container      Function code
        │               │
        ▼               ▼
Artifact Registry   Cloud Run
        │             Function
        ▼
Cloud Run Job
```

So source control might look like:

```text
data-platform/
│
├── dbt/
│   ├── models/
│   ├── dbt_project.yml
│   └── Dockerfile
│
├── functions/
│   └── dbt_trigger/
│       ├── main.py
│       └── requirements.txt
│
├── composer/
│   └── dags/
│
└── cloudbuild.yaml
```

That's starting to resemble a real platform repository.

---

# 23. IAM after four options

Our identity model now looks like:

```text
                 CLOUD BUILD SA
                       │
                       │ deploy
          ┌────────────┴────────────┐
          ▼                         ▼
     Cloud Run Job             Function
          ▲                         │
          │                         │
          │                         │ trigger
          │                         ▼
          ├─────────────────────────┘
          │
          │
    Composer SA
          │
          │ trigger
          ▼
     Cloud Run Job
          │
          │ runs as
          ▼
      dbt-runner SA
          │
          │
          ▼
       BigQuery
```

Responsibilities remain:

| Identity            | Responsibility  |
| ------------------- | --------------- |
| Cloud Build SA      | Build/deploy    |
| Composer SA         | Orchestration   |
| Function trigger SA | Trigger dbt job |
| dbt-runner SA       | BigQuery access |

That's much better than giving every component one powerful service account.

---

# 24. Failure scenarios to demonstrate

For your POC presentation, I'd intentionally demonstrate four.

**Normal event**

```text
transactions_2026-09-18.csv
       ↓
Function
       ↓
Cloud Run
       ↓
dbt SUCCESS
       ↓
TABLE_B
```

**Wrong file**

```text
README.txt
    ↓
Function
    ↓
ignored
```

**dbt test failure**

```text
valid file
   ↓
Function
   ↓
Cloud Run
   ↓
dbt model ✓
dbt test  ✗
   ↓
Cloud Run FAILED
```

**Duplicate event**

```text
same processing date
       ↓
two executions
       ↓
idempotent dbt transformation
       ↓
same final TABLE_B state
```

Those demonstrate that you understand operational behaviour rather than just the happy path.

---

# 25. Our architecture after Option 4

We now have:

```text
                          GIT
                           │
                           ▼
                     CLOUD BUILD
                           │
                  build + deploy
                           │
                           ▼
                    Artifact Registry
                           │
                           ▼
                    CLOUD RUN JOB
                           ▲
                ┌──────────┴───────────┐
                │                      │
             scheduled              event
                │                      │
                │                      ▼
            COMPOSER               EVENTARC
                │                      │
                │                      ▼
                │               CLOUD RUN
                │                FUNCTION
                │                      │
                └──────────┬───────────┘
                           ▼
                     CLOUD RUN JOB
                           │
                           ▼
                          dbt
                           │
                           ▼
                       BIGQUERY

                  TABLE_A ─────► TABLE_B
```

The key learning from **Option 4** is that **Cloud Run functions are best used here as lightweight event adapters, not as another place to host dbt**.

Our four components now have very clear jobs:

```text
Cloud Build
    → HOW code gets deployed

Composer
    → WHEN / in what workflow it runs

Cloud Run Function
    → WHAT EVENT triggers it

Cloud Run Job
    → WHERE dbt executes

dbt
    → WHAT transformation happens

BigQuery
    → WHERE the SQL/data processing happens
```

**Option 5 — Dataflow** is where the architecture changes more significantly. Rather than using Dataflow as a strange dbt runner, we'll build a proper pipeline such as **Pub/Sub/GCS → Apache Beam/Dataflow → `TABLE_A` → data-ready event → Cloud Run/dbt → `TABLE_B`**, and then compare when the Dataflow transformation should stay in Beam versus when it belongs in dbt.
