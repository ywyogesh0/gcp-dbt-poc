## Option 5 — Dataflow + dbt + BigQuery

For Dataflow, I would change the pattern slightly. **Dataflow should process/ingest the data into Table A; it should not be used merely as a machine on which to execute `dbt run`.**

Our POC will therefore be:

```text
                SOURCE
                  │
            GCS / Pub/Sub
                  │
                  ▼
            ┌───────────┐
            │ DATAFLOW  │
            │ Beam      │
            └─────┬─────┘
                  │
                  ▼
          BigQuery TABLE_A
                  │
             data ready
                  ▼
            Composer
               OR
          Event → Function
                  │
                  ▼
          Cloud Run dbt Job
                  │
                  ▼
                 dbt
                  │
                  ▼
          BigQuery TABLE_B
```

This gives Dataflow a genuine data-engineering responsibility.

### 1. What Dataflow adds

So far we assumed `TABLE_A` already existed. Now we'll build its upstream ingestion.

For example, a CSV arrives containing:

```csv
customer_id,customer_name,transaction_amount,transaction_timestamp
1,Yogesh,100.00,2026-09-19T01:00:00Z
2,John,250.00,2026-09-19T01:01:00Z
3,Sarah,75.50,2026-09-19T01:02:00Z
```

We want Dataflow to perform:

```text
GCS
 │
 ▼
Read CSV
 │
 ▼
Parse
 │
 ▼
Validate
 │
 ▼
Convert schema
 │
 ▼
BigQuery TABLE_A
```

Then dbt performs the analytical/business transformation:

```text
TABLE_A
   │
   ▼
dbt
   │
   ├── uppercase customer
   ├── calculate VAT
   ├── calculate final amount
   └── data tests
   │
   ▼
TABLE_B
```

That's a useful separation between **ingestion/processing** and **warehouse modelling**.

---

# 2. Enable Dataflow APIs

```bash
gcloud services enable \
    dataflow.googleapis.com \
    compute.googleapis.com \
    storage.googleapis.com \
    bigquery.googleapis.com
```

We'll continue using:

```text
Project : YOUR_PROJECT_ID
Region  : europe-west2
Dataset : dbt_poc
```

Create an input bucket:

```bash
gcloud storage buckets create \
    gs://YOUR_PROJECT_ID-dataflow-input \
    --location=europe-west2
```

and a staging/temp bucket:

```bash
gcloud storage buckets create \
    gs://YOUR_PROJECT_ID-dataflow-temp \
    --location=europe-west2
```

Architecture:

```text
dataflow-input
      │
      ▼
   Dataflow
      │
      ├── staging/temp → dataflow-temp
      │
      └── output       → BigQuery
```

---

# 3. Create a Dataflow service account

Again, don't reuse our dbt identity.

For example:

```bash
gcloud iam service-accounts create dataflow-runner \
    --display-name="Dataflow POC Runner"
```

Result:

```text
dataflow-runner@
YOUR_PROJECT_ID.iam.gserviceaccount.com
```

Its responsibilities are different:

```text
Dataflow Runner
      │
      ├── read GCS input
      ├── use staging bucket
      ├── run Dataflow workers
      └── write TABLE_A


dbt Runner
      │
      ├── read TABLE_A
      └── write TABLE_B
```

This distinction will become useful when we draw the final IAM architecture.

---

# 4. Apache Beam pipeline

Let's implement the Dataflow job using Python Apache Beam.

Project:

```text
dataflow/
│
├── main.py
└── requirements.txt
```

`requirements.txt`:

```text
apache-beam[gcp]
```

Now `main.py`.

```python
import apache_beam as beam

from apache_beam.options.pipeline_options import (
    PipelineOptions
)


class ParseTransaction(beam.DoFn):

    def process(self, line):

        if line.startswith("customer_id"):
            return

        fields = line.split(",")

        yield {
            "customer_id": int(fields[0]),
            "customer_name": fields[1],
            "transaction_amount": float(fields[2]),
            "transaction_timestamp": fields[3]
        }


def run():

    options = PipelineOptions(
        save_main_session=True
    )

    with beam.Pipeline(options=options) as pipeline:

        (
            pipeline

            | "Read CSV"
            >> beam.io.ReadFromText(
                "gs://YOUR_PROJECT_ID-dataflow-input/*.csv"
            )

            | "Parse Transactions"
            >> beam.ParDo(ParseTransaction())

            | "Write Table A"
            >> beam.io.WriteToBigQuery(

                "YOUR_PROJECT_ID:dbt_poc.table_a",

                schema="""
                    customer_id:INTEGER,
                    customer_name:STRING,
                    transaction_amount:FLOAT,
                    transaction_timestamp:TIMESTAMP
                """,

                write_disposition=
                    beam.io.BigQueryDisposition.WRITE_APPEND,

                create_disposition=
                    beam.io.BigQueryDisposition.CREATE_IF_NEEDED
            )
        )


if __name__ == "__main__":
    run()
```

Conceptually:

```text
CSV line

"1,Yogesh,100,..."

        │
        ▼

ParseTransaction

        │
        ▼

{
 customer_id: 1,
 customer_name: "Yogesh",
 transaction_amount: 100
}

        │
        ▼

BigQuery
TABLE_A
```

For production CSV parsing, use a proper CSV parser rather than `split(",")`, because quoted commas and escaping will otherwise break parsing.

---

# 5. Test Beam locally first

This is one of Beam's nice properties.

We can execute the same logical pipeline with:

```text
DirectRunner
```

during development and:

```text
DataflowRunner
```

in GCP.

Conceptually:

```text
                  Beam code
                     │
             ┌───────┴───────┐
             ▼               ▼
       DirectRunner      DataflowRunner
          local               GCP
```

This gives us a good development model.

---

# 6. Execute on Dataflow

The command is conceptually:

```bash
python main.py \
    --runner=DataflowRunner \
    --project=YOUR_PROJECT_ID \
    --region=europe-west2 \
    --temp_location=gs://YOUR_PROJECT_ID-dataflow-temp/temp \
    --staging_location=gs://YOUR_PROJECT_ID-dataflow-temp/staging \
    --service_account_email=dataflow-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

Now Google provisions the managed Dataflow workers and executes the Beam graph.

You can inspect:

**Google Cloud → Dataflow → Jobs**

and you'll see something like:

```text
Read CSV
   │
   ▼
Parse Transactions
   │
   ▼
Write Table A
```

Dataflow provides the managed execution layer for Apache Beam batch and streaming pipelines. [Google Cloud Dataflow documentation](https://cloud.google.com/dataflow/docs?utm_source=chatgpt.com)

---

# 7. Verify Table A

After Dataflow succeeds:

```sql
SELECT *
FROM `YOUR_PROJECT_ID.dbt_poc.table_a`;
```

Now we've finally implemented the source side of our architecture:

```text
transactions.csv
       │
       ▼
     GCS
       │
       ▼
   Dataflow
       │
       ▼
BigQuery TABLE_A
```

But we still have an important problem.

How does dbt know that Dataflow has finished?

---

# 8. Pattern A — Composer orchestrates everything

This is probably the cleanest **batch** implementation.

Instead of independently starting Dataflow, Composer owns the entire workflow:

```text
                     COMPOSER
                         │
                         ▼
                 Start Dataflow
                         │
                         ▼
                  Wait for success
                         │
                         ▼
                  Validate TABLE_A
                         │
                         ▼
                 Cloud Run dbt Job
                         │
                         ▼
                  Validate TABLE_B
```

This is much stronger than:

```text
Dataflow starts somehow

and

dbt starts somehow
```

because Composer explicitly knows the dependency:

```text
Dataflow SUCCESS

       ↓

dbt allowed to start
```

---

# 9. Composer DAG

Conceptually:

```python
with DAG(
    dag_id="dataflow_dbt_pipeline",
    schedule="0 2 * * *",
    catchup=False,
    ...
) as dag:

    run_dataflow = ...

    check_table_a = ...

    run_dbt = ...

    check_table_b = ...


    run_dataflow \
        >> check_table_a \
        >> run_dbt \
        >> check_table_b
```

Graph:

```text
┌───────────────────┐
│    DATAFLOW       │
│ GCS → TABLE_A     │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ Validate TABLE_A  │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ CLOUD RUN JOB     │
│ dbt build         │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ Validate TABLE_B  │
└───────────────────┘
```

This is the pattern I'd demonstrate for batch.

---

# 10. Pass the same processing date everywhere

This becomes particularly powerful with Composer.

Suppose the DAG is processing:

```text
2026-09-19
```

Composer passes that date to Dataflow:

```text
Composer

{{ ds }}
   │
   ▼
2026-09-19
   │
   ▼
Dataflow

transactions_2026-09-19.csv
```

Then passes exactly the same value to dbt:

```text
2026-09-19
     │
     ▼
Cloud Run
     │
     ▼
dbt

--vars
processing_date=2026-09-19
```

Therefore:

```text
                 processing_date
                    2026-09-19
                         │
                ┌────────┴────────┐
                ▼                 ▼
            Dataflow             dbt
                │                 │
                ▼                 ▼
             TABLE_A           TABLE_B
```

You now have a common business-processing date across the whole pipeline.

---

# 11. Pattern B — Event-driven Dataflow

For streaming or asynchronous pipelines, Composer may not be the right trigger.

Instead:

```text
Source
  │
  ▼
Pub/Sub
  │
  ▼
DATAFLOW
  │
  │ continuous stream
  ▼
TABLE_A
```

For example:

```text
Payment event

{
 payment_id,
 amount,
 timestamp
}

        │
        ▼
      Pub/Sub
        │
        ▼
     Dataflow
        │
        ├── validate
        ├── enrich
        ├── window
        └── write
        │
        ▼
    BigQuery L1
```

This resembles the kind of streaming → BigQuery architecture you're already familiar with, except Dataflow/Beam replaces the ingestion component.

---

# 12. But when should dbt run?

Suppose Dataflow is a **24×7 streaming job**.

There is no:

```text
Dataflow finished
```

event.

It never finishes.

So this doesn't make sense:

```text
Wait for Dataflow completion
       ↓
run dbt
```

Instead:

```text
Streaming Dataflow
        │
        ▼
TABLE_A continuously populated


02:00
  │
  ▼
Composer
  │
  ▼
dbt
  │
  ▼
process completed partition/window
```

Architecture:

```text
             STREAMING PATH

Pub/Sub
   │
   ▼
Dataflow  ───────────────────┐
   │                         │
   ▼                         │
TABLE_A                      │
                             │
                             │ continuous


             BATCH CURATION PATH

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
  │
  ▼
TABLE_B
```

This is a very common hybrid design.

---

# 13. Dataflow vs dbt transformation

This is probably the most important design question in Option 5:

> If Dataflow can transform data, why do we need dbt?

Because they solve different classes of transformations.

### Good Dataflow transformation

For example:

```text
Kafka/PubSub message
       │
       ▼
deserialize
       │
       ▼
schema validation
       │
       ▼
PII tokenisation
       │
       ▼
event-time window
       │
       ▼
deduplication
       │
       ▼
TABLE_A
```

These are ingestion/stream-processing concerns.

### Good dbt transformation

```text
TABLE_A
   │
   ├── join customers
   ├── join accounts
   ├── business rules
   ├── aggregate payments
   ├── derive metrics
   └── data tests
   │
   ▼
TABLE_B
```

These are warehouse modelling concerns.

---

# 14. Don't duplicate transformations

Bad architecture:

```text
Dataflow

calculate customer status
calculate VAT
calculate settlement category
calculate reporting metric

        ↓

TABLE_A

        ↓

dbt

calculate customer status again
calculate VAT again
...
```

Now business logic exists in two places.

Instead:

```text
Dataflow
    │
    ├── ingestion
    ├── validation
    ├── technical enrichment
    └── streaming concerns
    │
    ▼
TABLE_A
    │
    ▼
dbt
    │
    ├── business transformations
    ├── analytical joins
    ├── curated models
    └── data quality
    │
    ▼
TABLE_B
```

That's much easier to govern.

---

# 15. Dead-letter handling

Let's make the Dataflow part more production-like.

Suppose:

```text
1,Yogesh,100,...
```

is valid, but:

```text
ABC,Broken,HELLO,...
```

isn't.

Don't fail the entire pipeline.

Use tagged outputs:

```text
                    Dataflow
                       │
                    Parse
                       │
             ┌─────────┴─────────┐
             │                   │
           VALID               INVALID
             │                   │
             ▼                   ▼
          TABLE_A             DLQ
                             BigQuery/
                             GCS/PubSub
```

Then:

```text
valid rows
   ↓
TABLE_A

bad rows
   ↓
quarantine
```

This is a good addition to the POC because it demonstrates operational data engineering rather than only a happy path.

---

# 16. Add ingestion metadata

I'd also add technical fields to Table A:

```text
customer_id
customer_name
transaction_amount
transaction_timestamp

_source_file
_ingestion_timestamp
_processing_date
_pipeline_run_id
```

For example:

```text
1
Yogesh
100
2026-09-19T01:00:00

_source_file
transactions_2026-09-19.csv

_ingestion_timestamp
2026-09-19T02:04:27

_processing_date
2026-09-19

_pipeline_run_id
df-20260919-001
```

This becomes extremely useful later for lineage, debugging and agentic RCA.

---

# 17. Dataflow Templates

For a proper deployment, I wouldn't want Composer shipping raw Python commands every day.

Package the Dataflow pipeline as a **Flex Template**.

Conceptually:

```text
Beam source
    │
    ▼
Docker image
    │
    ▼
Artifact Registry
    │
    ▼
Flex Template specification
    │
    ▼
GCS
```

Then Composer simply says:

```text
Run template:

input =
gs://.../transactions_2026-09-19.csv

output =
project.dbt_poc.table_a

processing_date =
2026-09-19
```

Google recommends templates when you want reusable parameterised Dataflow pipelines rather than recompiling/repackaging the pipeline for every execution. [Dataflow templates documentation](https://cloud.google.com/dataflow/docs/concepts/dataflow-templates?utm_source=chatgpt.com)

---

# 18. Cloud Build can now deploy Dataflow too

Our Option 3 architecture gets another component.

Previously:

```text
Cloud Build
   │
   ├── dbt image
   └── Function
```

Now:

```text
                        CLOUD BUILD
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
        dbt image       Function       Dataflow image
            │                │                │
            ▼                ▼                ▼
      Cloud Run Job    Cloud Run      Artifact Registry
                       Function              │
                                             ▼
                                      Flex Template
```

Now Git controls:

```text
dbt/
dataflow/
functions/
composer/
```

which is starting to look like a genuine data-platform repository.

---

# 19. IAM architecture

We now have four main runtime identities:

```text
Cloud Build SA
     │
     └── build/deploy


Composer SA
     │
     ├── launch Dataflow
     └── execute Cloud Run Job


Dataflow SA
     │
     ├── read source
     └── write TABLE_A


dbt-runner SA
     │
     ├── read TABLE_A
     └── write TABLE_B
```

Visualised:

```text
                     Composer SA
                         │
               ┌─────────┴──────────┐
               ▼                    ▼
            Dataflow            Cloud Run
               │                    │
          Dataflow SA           dbt-runner SA
               │                    │
               ▼                    ▼
            TABLE_A ───────────► TABLE_B
```

Again, no single mega-service-account is required.

---

# 20. Dataflow lineage

This will help us later when we reach Knowledge Catalog.

Ultimately we want lineage resembling:

```text
GCS
 │
 ▼
Dataflow
 │
 ▼
TABLE_A
 │
 ▼
BigQuery/dbt transformation
 │
 ▼
TABLE_B
```

Google Cloud's lineage capabilities support Dataflow lineage for supported sources/sinks and BigQuery lineage, which we'll bring together when we implement the Knowledge Catalog stage. [Google Cloud data lineage documentation](https://cloud.google.com/dataplex/docs/about-data-lineage?utm_source=chatgpt.com)

So eventually the POC isn't just:

```text
A → B
```

but potentially:

```text
transactions.csv
       │
       ▼
    Dataflow
       │
       ▼
    TABLE_A
       │
       ▼
 dbt / BigQuery
       │
       ▼
    TABLE_B
```

That's much more compelling for lineage.

---

# 21. Failure propagation with Composer

Suppose Dataflow fails.

```text
Dataflow ✗
    │
    X
TABLE_A validation
    │
    X
dbt
```

dbt should never start.

Suppose Dataflow succeeds but source validation fails:

```text
Dataflow ✓
    │
    ▼
TABLE_A validation ✗
    │
    X
dbt
```

Suppose Dataflow and dbt succeed but a dbt test fails:

```text
Dataflow ✓
    │
    ▼
TABLE_A ✓
    │
    ▼
dbt model ✓
dbt test ✗
    │
    ▼
Cloud Run ✗
    │
    ▼
Composer DAG ✗
```

Now Composer gives us a unified workflow view.

---

# 22. Complete batch architecture

At this stage I'd present the batch implementation as:

```text
                      GCS
                       │
              transactions.csv
                       │
                       ▼
                ┌────────────┐
                │  COMPOSER  │
                └──────┬─────┘
                       │
                       ▼
                ┌────────────┐
                │  DATAFLOW  │
                │ Apache Beam│
                └──────┬─────┘
                       │
                       ▼
               ┌───────────────┐
               │ BIGQUERY L1   │
               │    TABLE_A    │
               └───────┬───────┘
                       │
                source validation
                       │
                       ▼
               ┌───────────────┐
               │ CLOUD RUN JOB │
               │               │
               │     dbt       │
               └───────┬───────┘
                       │
                       ▼
               ┌───────────────┐
               │ BIGQUERY L2   │
               │    TABLE_B    │
               └───────────────┘
```

And CI/CD sits alongside it:

```text
Git
 │
 ▼
Cloud Build
 │
 ├── Dataflow Flex Template
 ├── dbt container
 ├── Composer DAG
 └── Function
```

---

## What Option 5 proves

We can now explain the platform in terms of responsibilities:

| Component              | Responsibility                        |
| ---------------------- | ------------------------------------- |
| **Dataflow**           | Ingestion, streaming, Beam processing |
| **BigQuery Table A**   | Raw/L1 warehouse layer                |
| **dbt**                | Warehouse/business transformation     |
| **BigQuery Table B**   | Curated/L2 layer                      |
| **Cloud Run Job**      | dbt runtime                           |
| **Composer**           | End-to-end workflow orchestration     |
| **Cloud Build**        | CI/CD                                 |
| **Functions/Eventarc** | Event-driven initiation               |

The important architectural lesson is **not** "Dataflow can trigger dbt". It's:

> **Dataflow handles data movement/stream processing; dbt handles warehouse modelling; Composer coordinates batch dependencies; Cloud Run provides the dbt runtime.**

That distinction will also make **Option 6 — Dataproc** much clearer. Next we can implement **GCS → Dataproc Serverless/Spark → BigQuery Table A → Composer → Cloud Run/dbt → Table B**, including a PySpark job, parameter passing, IAM, and then compare **Dataproc vs Dataflow vs BigQuery/dbt** so it's clear when each one should actually be chosen.
