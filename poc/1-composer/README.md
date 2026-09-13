**Option 1 = Cloud Composer → dbt → BigQuery**.

For this first implementation, let's keep it simple and make **Composer execute dbt directly**.

Once that works, in Option 2 we'll containerise the same dbt project and run it through Cloud Run.

### Target for Option 1

```text
Cloud Composer
      │
      │ Airflow DAG
      ▼
   dbt run
      │
      ▼
   BigQuery
      │
      ├── READ  → TABLE_A
      │
      │ transformation
      │
      └── WRITE → TABLE_B
```

We'll build it in this order:

```text
1. Create/choose GCP project
        ↓
2. Enable APIs
        ↓
3. Create BigQuery dataset
        ↓
4. Create TABLE_A + sample data
        ↓
5. Create dbt project
        ↓
6. Create Composer environment
        ↓
7. Configure IAM
        ↓
8. Put dbt project where Composer can access it
        ↓
9. Create Airflow DAG
        ↓
10. Trigger DAG
        ↓
11. Verify TABLE_B
        ↓
12. Add dbt tests
```

## Step 1 — GCP project

Let's use example values throughout:

```text
Project ID : sturdy-practice-245323
Region     : europe-west2
BQ Dataset : dbt_poc
Source     : table_a
Target     : table_b
```

You can substitute your actual project ID.

From Cloud Shell:

```bash
gcloud config set project YOUR_PROJECT_ID
```

Check:

```bash
gcloud config get-value project
```

## Step 2 — Enable required APIs

Run:

```bash
gcloud services enable \
    datalineage.googleapis.com \
    bigquery.googleapis.com \
    composer.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com
```

Composer environment creation can take a while, so we'll start that shortly.

## Step 3 — Create BigQuery dataset

Go to:

**Google Cloud Console → BigQuery → your project → Create dataset**

Use:

```text
Dataset ID:
dbt_poc

Location:
europe-west2
```

Or:

```bash
bq --location=europe-west2 mk \
  --dataset \
  YOUR_PROJECT_ID:dbt_poc
```

You should now have:

```text
YOUR_PROJECT
└── dbt_poc
```

## Step 4 — Create `table_a`

Run in BigQuery:

```sql
CREATE OR REPLACE TABLE `YOUR_PROJECT_ID.dbt_poc.table_a`
(
    customer_id INT64,
    customer_name STRING,
    transaction_amount NUMERIC,
    transaction_timestamp TIMESTAMP
);
```

Insert sample data:

```sql
INSERT INTO `YOUR_PROJECT_ID.dbt_poc.table_a`
VALUES
    (1, 'Yogesh', 100.00, CURRENT_TIMESTAMP()),
    (2, 'John',   250.00, CURRENT_TIMESTAMP()),
    (3, 'Sarah',   75.50, CURRENT_TIMESTAMP()),
    (4, 'Mike',   -20.00, CURRENT_TIMESTAMP()),
    (5, 'Anna',   500.00, CURRENT_TIMESTAMP());
```

Verify:

```sql
SELECT *
FROM `YOUR_PROJECT_ID.dbt_poc.table_a`;
```

You should get five rows.

---

# Step 5 — Create our dbt project

Now we need:

```text
dbt
 │
 ├── source = table_a
 │
 ├── transformation
 │
 └── target = table_b
```

Create:

```text
dbt_poc/
│
├── dbt_project.yml
│
├── profiles.yml
│
└── models/
    ├── sources.yml
    ├── transform_table_b.sql
    └── schema.yml
```

### `dbt_project.yml`

```yaml
name: 'dbt_poc'
version: '1.0.0'
config-version: 2

profile: 'dbt_poc'

model-paths: ["models"]

models:
  dbt_poc:
    +materialized: table
```

### `models/sources.yml`

```yaml
version: 2

sources:
  - name: source
    database: YOUR_PROJECT_ID
    schema: dbt_poc

    tables:
      - name: table_a
```

Now dbt understands that:

```text
source('source', 'table_a')

              │
              ▼

YOUR_PROJECT_ID.dbt_poc.table_a
```

### `models/transform_table_b.sql`

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

Notice Mike has:

```text
transaction_amount = -20
```

so he should disappear from `table_b`.

Our expected result:

```text
TABLE_A
5 rows
   │
   │ WHERE transaction_amount > 0
   ▼
TABLE_B
4 rows
```

### Add dbt tests

`models/schema.yml`:

```yaml
version: 2

models:
  - name: transform_table_b
    description: "Curated customer transaction table"

    columns:

      - name: customer_id
        tests:
          - not_null
          - unique

      - name: customer_name
        tests:
          - not_null

      - name: final_amount
        tests:
          - not_null
```

Eventually Composer will execute:

```bash
dbt run --select transform_table_b
```

followed by:

```bash
dbt test --select transform_table_b
```

---

# Step 6 — Create Cloud Composer

Now the interesting part.

Go to:

**Google Cloud Console → Managed Airflow → Environments → Create**

Choose **Composer 3** for a new POC.

Keep it small because we're learning rather than building production capacity.

Use:

```text
Name:
sturdy-practice-245323

Region:
europe-west2
```

Composer creates an Airflow environment roughly like:

```text
Cloud Composer
      │
      ├── Airflow Scheduler
      │
      ├── Airflow Workers
      │
      ├── Airflow Webserver
      │
      └── GCS bucket
             │
             ├── dags/
             ├── data/
             ├── logs/
             └── plugins/
```

That GCS bucket becomes important.

When you upload:

```text
dags/dbt_poc_dag.py
```

Composer automatically discovers the DAG.

---

# Step 7 — Install dbt in Composer

Composer allows us to install additional PyPI packages.

In the Composer environment:

**PyPI packages → Edit**

Add:

```text
dbt-bigquery
```

For a proper POC, **pin the version** rather than leaving it floating, after checking compatibility with your Composer Python environment.

Conceptually:

```text
Composer Worker
      │
      ├── Python
      ├── Airflow
      └── dbt-bigquery
```

Then Airflow can execute:

```bash
dbt --version
```

and ultimately:

```bash
dbt run
```

---

# Step 8 — Authentication

This is one of the most important concepts.

Don't put:

```text
service-account-key.json
```

inside the dbt project.

Composer already runs using a Google Cloud identity.

We want:

```text
Composer worker
      │
      │ service account
      ▼
Google IAM
      │
      ▼
BigQuery
```

The Composer service account needs appropriate BigQuery permissions.

For the POC, conceptually:

```text
Composer Service Account

BigQuery Job User
       +
BigQuery Data Viewer
       +
BigQuery Data Editor
       +
Storage Object List
```

In a production environment we'd tighten this considerably.

---

# Step 9 — `profiles.yml`

Configure dbt:

```yaml
dbt_poc:

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

No password.

No private key.

No JSON credentials.

The runtime identity supplies authentication.

---

# Step 10 — Put dbt project into Composer

For this first POC, use Composer's bucket.

For example:

```text
gs://YOUR_COMPOSER_BUCKET/
│
├── dags/
│   └── dbt_poc_dag.py
│
└── data/
    └── dbt_poc/
        ├── dbt_project.yml
        ├── profiles.yml
        │
        └── models/
            ├── sources.yml
            ├── schema.yml
            └── transform_table_b.sql
```

This gives us:

```text
Composer Worker
       │
       │ dbt project
       ▼
Composer GCS data directory
```

---

# Step 11 — Airflow DAG

Now create:

```text
dbt_poc_dag.py
```

Start with three tasks:

```text
START
  │
  ▼
DBT RUN
  │
  ▼
DBT TEST
  │
  ▼
END
```

Conceptually:

```python
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

    dbt_debug >> dbt_run >> dbt_test
```

Now Airflow understands:

```text
dbt_debug
    │
    ▼
dbt_run
    │
    ▼
dbt_test
```

And if `dbt_run` fails:

```text
dbt_debug ✓
    │
    ▼
dbt_run ✗
    │
    X
dbt_test
```

The test won't execute.

That's exactly what we want.

---

# Step 12 — Upload the DAG

Put:

```text
dbt_poc_dag.py
```

into Composer's:

```text
/dags
```

Composer synchronises it.

Open:

**Composer → your environment → Airflow UI → DAGs**

Eventually you should see:

```text
dbt_bigquery_poc
```

Open the DAG.

Graph view:

```text
┌─────────────┐
│ dbt_debug   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   dbt_run   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  dbt_test   │
└─────────────┘
```

Trigger it manually.

---

# Step 13 — What happens internally

This is worth understanding rather than treating Composer as magic.

Airflow executes:

```bash
dbt run --select transform_table_b
```

dbt reads:

```sql
FROM {{ source('source', 'table_a') }}
```

and compiles it into BigQuery SQL referencing:

```sql
FROM `YOUR_PROJECT_ID.dbt_poc.table_a`
```

dbt then submits a job to BigQuery.

So:

```text
                 COMPOSER
                    │
                    │ BashOperator
                    ▼
                 dbt CLI
                    │
                    │ compile
                    ▼
                 SQL query
                    │
                    ▼
                 BIGQUERY
                    │
          ┌─────────┴─────────┐
          │                   │
          ▼                   ▼
      READ TABLE_A       WRITE TABLE_B
```

**Composer itself isn't doing the data transformation.**

BigQuery performs the actual compute.

Composer is orchestrating dbt; dbt is generating/submitting SQL; BigQuery executes it.

That's a very important architectural distinction.

---

# Step 14 — Verify `table_b`

After DAG success, run:

```sql
SELECT *
FROM `YOUR_PROJECT_ID.dbt_poc.table_b`
ORDER BY customer_id;
```

Expected:

```text
Yogesh → 100 → VAT 20 → final 120
John   → 250 → VAT 50 → final 300
Sarah  → 75.5 → VAT 15.1 → final 90.6
Anna   → 500 → VAT 100 → final 600
```

Mike isn't present because:

```sql
WHERE transaction_amount > 0
```

So we've successfully demonstrated:

```text
TABLE_A
   │
   │ 5 records
   ▼

Cloud Composer
   │
   ▼
dbt
   │
   │ transform
   ▼

TABLE_B
4 records
```

## One improvement before we build it

For **learning**, the direct Composer approach above is useful because you see how Airflow, dbt and BigQuery interact.

For the architecture I'd ultimately recommend, however, we will evolve it to:

```text
Composer
    │
    │ orchestration
    ▼
Cloud Run Job
    │
    │ dbt container
    ▼
dbt
    │
    ▼
BigQuery
```

That keeps the Composer environment from becoming the dbt runtime and gives us a reusable container that Cloud Composer, Functions and other services can trigger.

