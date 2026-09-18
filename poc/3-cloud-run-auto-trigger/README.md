Now **Option 3 = Cloud Build**, and we'll integrate it with what we already built rather than creating an isolated example.

Our architecture evolves into:

```text
Git repository
      │
      │ commit / merge
      ▼
┌─────────────────────────┐
│       CLOUD BUILD       │
│                         │
│  1. Validate dbt        │
│  2. Build Docker image  │
│  3. Push image          │
│  4. Update Cloud Run    │
└────────────┬────────────┘
             │
             ▼
      Artifact Registry
             │
             ▼
      Cloud Run dbt Job
             ▲
             │
        Cloud Composer
             │
             ▼
           dbt
             │
             ▼
          BigQuery
      TABLE_A → TABLE_B
```

Cloud Build supports source-triggered builds, containerised build steps, Artifact Registry output and automated deployment workflows. ([Google Cloud Documentation][1])

The key principle is:

```text
Cloud Build  = CI/CD
Composer     = orchestration
Cloud Run    = runtime
dbt          = transformation framework
BigQuery     = data processing/storage
```

## 1. What changes from Option 2?

Previously we manually did:

```bash
gcloud builds submit \
  --tag europe-west2-docker.pkg.dev/PROJECT_ID/dbt-poc/dbt-bigquery:v1
```

and manually updated Cloud Run.

That's the part we're eliminating.

We want:

```text
Developer changes dbt SQL
        │
        ▼
git commit
        │
        ▼
git push
        │
        ▼
Cloud Build Trigger
        │
        ▼
dbt validation
        │
        ▼
Docker build
        │
        ▼
Artifact Registry
        │
        ▼
Cloud Run Job updated
```

Composer doesn't change.

---

# 2. Repository structure

I would structure the repository something like:

```text
dbt-data-platform/
│
├── dbt_project.yml
├── profiles.yml
├── packages.yml
├── requirements.txt
│
├── models/
│   ├── sources.yml
│   ├── schema.yml
│   └── transform_table_b.sql
│
├── macros/
│
├── tests/
│
├── Dockerfile
│
├── cloudbuild.yaml
│
└── composer/
    └── dbt_cloud_run_dag.py
```

You now have three kinds of code in Git:

```text
Transformation
     │
     └── dbt

Runtime
     │
     └── Dockerfile

Deployment
     │
     └── cloudbuild.yaml

Orchestration
     │
     └── Airflow DAG
```

This separation is useful later when the POC becomes a real project.

---

# 3. Keep our Dockerfile

From Option 2:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DBT_PROFILES_DIR=/app

ENTRYPOINT ["dbt"]

CMD ["build"]
```

And:

```text
requirements.txt
```

contains a pinned tested version:

```text
dbt-bigquery==<YOUR_TESTED_VERSION>
```

Don't use an unpinned dependency in your deployment pipeline.

---

# 4. What should Cloud Build actually do?

I recommend five stages:

```text
                 CLOUD BUILD

                     START
                       │
                       ▼
             ┌─────────────────┐
             │ 1. dbt deps     │
             └────────┬────────┘
                      ▼
             ┌─────────────────┐
             │ 2. dbt compile  │
             └────────┬────────┘
                      ▼
             ┌─────────────────┐
             │ 3. Docker build │
             └────────┬────────┘
                      ▼
             ┌─────────────────┐
             │ 4. Push image   │
             └────────┬────────┘
                      ▼
             ┌─────────────────┐
             │ 5. Update       │
             │ Cloud Run Job   │
             └────────┬────────┘
                      ▼
                    SUCCESS
```

Notice I said **`dbt compile`**, not necessarily `dbt run`.

That's deliberate.

Your CI pipeline shouldn't normally modify production BigQuery tables just because somebody pushed code.

---

# 5. CI vs runtime testing

This distinction is important.

Cloud Build should answer:

> Is this dbt project structurally valid and deployable?

Cloud Run should answer:

> Can this model execute against the actual data?

So:

```text
CLOUD BUILD

dbt deps
dbt parse/compile
Docker build
deployment validation
      │
      ▼
Deploy


CLOUD RUN

dbt build
     │
     ├── execute model
     └── execute data tests
```

Eventually we can create a dedicated CI BigQuery dataset and run modified models against test data, but don't point CI straight at production tables.

---

# 6. Create `cloudbuild.yaml`

Here's our first useful version:

```yaml
steps:

  # ---------------------------------
  # STEP 1
  # Install dbt and compile project
  # ---------------------------------

  - id: "dbt-compile"

    name: "python:3.12-slim"

    entrypoint: "bash"

    args:
      - "-c"
      - |
        pip install --no-cache-dir -r requirements.txt

        dbt deps --profiles-dir .

        dbt compile --profiles-dir .


  # ---------------------------------
  # STEP 2
  # Build Docker image
  # ---------------------------------

  - id: "docker-build"

    name: "gcr.io/cloud-builders/docker"

    args:
      - "build"
      - "-t"
      - "europe-west2-docker.pkg.dev/$PROJECT_ID/dbt-poc/dbt-bigquery:$COMMIT_SHA"
      - "."


  # ---------------------------------
  # STEP 3
  # Push Docker image
  # ---------------------------------

  - id: "docker-push"

    name: "gcr.io/cloud-builders/docker"

    args:
      - "push"
      - "europe-west2-docker.pkg.dev/$PROJECT_ID/dbt-poc/dbt-bigquery:$COMMIT_SHA"


  # ---------------------------------
  # STEP 4
  # Update Cloud Run Job
  # ---------------------------------

  - id: "deploy-cloud-run"

    name: "gcr.io/google.com/cloudsdktool/cloud-sdk"

    entrypoint: "gcloud"

    args:
      - "run"
      - "jobs"
      - "update"
      - "dbt-bigquery-job"

      - "--image"
      - "europe-west2-docker.pkg.dev/$PROJECT_ID/dbt-poc/dbt-bigquery:$COMMIT_SHA"

      - "--region"
      - "europe-west2"


images:

  - "europe-west2-docker.pkg.dev/$PROJECT_ID/dbt-poc/dbt-bigquery:$COMMIT_SHA"
```

Cloud Build supports this build → push → deploy pattern, and `$COMMIT_SHA` is one of the substitutions populated for repository-triggered builds. ([Google Cloud Documentation][2])

---

# 7. Why `$COMMIT_SHA` instead of `latest`?

This is very important.

Don't rely only on:

```text
dbt-bigquery:latest
```

Use:

```text
dbt-bigquery:a73bd92...
```

where the tag corresponds to the Git commit.

Then you have traceability:

```text
Git commit
a73bd92
    │
    ▼
Cloud Build
a73bd92
    │
    ▼
Docker image
dbt-bigquery:a73bd92
    │
    ▼
Cloud Run
a73bd92
```

Suppose tomorrow a transformation produces bad data.

You can answer:

> Which exact version of the dbt code produced this?

```text
Cloud Run execution
        │
        ▼
container image
        │
        ▼
commit SHA
        │
        ▼
Git commit
```

Much better than:

```text
latest
```

You can go even further and deploy by immutable image digest.

---

# 8. First test without Git trigger

Before automating everything, test `cloudbuild.yaml` manually.

Run:

```bash
gcloud builds submit \
    --config=cloudbuild.yaml \
    --region=europe-west2
```

There's one issue, though.

A manually submitted build doesn't necessarily have the repository-trigger `$COMMIT_SHA` value you expect.

For manual testing, parameterise the tag.

For example:

```yaml
substitutions:

  _IMAGE_TAG: "manual"
```

Then:

```text
${_IMAGE_TAG}
```

instead of:

```text
$COMMIT_SHA
```

And run:

```bash
gcloud builds submit \
  --config=cloudbuild.yaml \
  --substitutions=_IMAGE_TAG=v2 \
  --region=europe-west2
```

Artifact Registry then gets:

```text
dbt-bigquery:v2
```

Once the Git trigger is configured, use the commit SHA for immutable traceability.

Cloud Build also supports user-defined substitutions specifically for parameterising things like repository, location and image name. ([Google Cloud Documentation][3])

---

# 9. Better parameterised `cloudbuild.yaml`

Rather than hardcoding everything, I'd use:

```yaml
substitutions:

  _REGION: "europe-west2"

  _REPOSITORY: "dbt-poc"

  _IMAGE: "dbt-bigquery"

  _JOB: "dbt-bigquery-job"


steps:

  - id: "dbt-compile"

    name: "python:3.12-slim"

    entrypoint: "bash"

    args:
      - "-c"
      - |
        pip install --no-cache-dir -r requirements.txt
        dbt deps --profiles-dir .
        dbt compile --profiles-dir .


  - id: "docker-build"

    name: "gcr.io/cloud-builders/docker"

    args:
      - "build"
      - "-t"
      - "${_REGION}-docker.pkg.dev/$PROJECT_ID/${_REPOSITORY}/${_IMAGE}:$COMMIT_SHA"
      - "."


  - id: "docker-push"

    name: "gcr.io/cloud-builders/docker"

    args:
      - "push"
      - "${_REGION}-docker.pkg.dev/$PROJECT_ID/${_REPOSITORY}/${_IMAGE}:$COMMIT_SHA"


  - id: "deploy"

    name: "gcr.io/google.com/cloudsdktool/cloud-sdk"

    entrypoint: "gcloud"

    args:

      - "run"
      - "jobs"
      - "update"
      - "${_JOB}"

      - "--image"
      - "${_REGION}-docker.pkg.dev/$PROJECT_ID/${_REPOSITORY}/${_IMAGE}:$COMMIT_SHA"

      - "--region"
      - "${_REGION}"


images:

  - "${_REGION}-docker.pkg.dev/$PROJECT_ID/${_REPOSITORY}/${_IMAGE}:$COMMIT_SHA"
```

Now the same pipeline can eventually deploy to different environments.

---

# 10. Cloud Build service account

This introduces another identity.

Previously:

```text
Composer SA
     │
     ▼
Cloud Run


dbt-runner SA
     │
     ▼
BigQuery
```

Now:

```text
                       Git
                        │
                        ▼
                 CLOUD BUILD
                        │
                  build/deploy SA
                        │
           ┌────────────┴────────────┐
           ▼                         ▼
    Artifact Registry          Cloud Run Job
```

So conceptually:

```text
cloud-build-deployer SA

     ├── write Artifact Registry
     │
     └── update Cloud Run Job
```

Google's deployment documentation lists Artifact Registry and Cloud Run permissions among the permissions needed for build-and-deploy workflows. ([Google Cloud Documentation][2])

I'd use a **dedicated user-managed Cloud Build service account** for a real implementation rather than progressively giving a default identity broad permissions.

---

# 11. Three identities now

Your security architecture is becoming quite nice:

```text
             ┌────────────────────────┐
             │ Cloud Build SA         │
             │                        │
             │ Build + deploy only    │
             └───────────┬────────────┘
                         │
                         ▼
                  Cloud Run Job
                         ▲
                         │
             ┌───────────┴────────────┐
             │ Composer SA            │
             │                        │
             │ Execute job only       │
             └────────────────────────┘


                  Cloud Run Job
                         │
                         │ runs as
                         ▼
             ┌────────────────────────┐
             │ dbt-runner SA          │
             │                        │
             │ BigQuery access        │
             └───────────┬────────────┘
                         │
                         ▼
                      BigQuery
```

So:

```text
Cloud Build SA
    → deploy

Composer SA
    → orchestrate

dbt-runner SA
    → access data
```

That's a strong least-privilege story.

---

# 12. Create the Git trigger

Now connect the repository.

Cloud Build supports repository-triggered builds from supported source repositories, including GitHub, GitLab and Bitbucket. ([Google Cloud Documentation][1])

In Google Cloud:

**Cloud Build → Triggers → Create Trigger**

Example:

```text
Name
dbt-main-deploy

Event
Push to branch

Repository
your dbt repository

Branch
^main$

Configuration
Cloud Build configuration file

Location
/cloudbuild.yaml
```

Now:

```text
git push origin main
       │
       ▼
Cloud Build trigger
       │
       ▼
cloudbuild.yaml
```

---

# 13. Development workflow

Now imagine you change:

```sql
ROUND(transaction_amount * 1.20, 2)
```

to some new business logic.

Developer:

```bash
git checkout -b feature/new-vat-rule
```

changes:

```text
transform_table_b.sql
```

then:

```bash
git add .
git commit -m "Update VAT calculation"
git push
```

Ideally the feature branch runs **CI validation only**.

It should NOT deploy production.

---

# 14. Separate CI from CD

This is where I'd improve the design further.

### Pull request

```text
Developer
    │
    ▼
Pull Request
    │
    ▼
Cloud Build CI
    │
    ├── dbt deps
    ├── dbt parse
    ├── dbt compile
    └── optional test environment
```

No production deployment.

### Merge to main

```text
PR approved
    │
    ▼
Merge main
    │
    ▼
Cloud Build CD
    │
    ├── build image
    ├── push Artifact Registry
    └── update Cloud Run Job
```

So:

```text
              FEATURE BRANCH
                    │
                    ▼
                   PR
                    │
                    ▼
             ┌─────────────┐
             │     CI      │
             │             │
             │ validate    │
             └──────┬──────┘
                    │
                  PASS
                    │
                    ▼
                 APPROVE
                    │
                    ▼
                  MAIN
                    │
                    ▼
             ┌─────────────┐
             │     CD      │
             │             │
             │ deploy      │
             └──────┬──────┘
                    │
                    ▼
              Cloud Run Job
```

---

# 15. Should Cloud Build run `dbt test`?

There's a subtle point here.

A normal:

```bash
dbt test
```

is not merely static code validation.

It queries data.

Therefore, don't blindly do:

```text
PR
 ↓
dbt test
 ↓
production BigQuery
```

A better architecture is:

```text
PR
 │
 ▼
CI dataset
 │
 ▼
dbt build
 │
 ▼
temporary/test tables
```

For example:

```text
Production

dbt_poc
   ├── table_a
   └── table_b


CI

dbt_ci_12345
   ├── table_a
   └── table_b
```

where `12345` might correspond to a pull-request/build identifier.

Then CI can safely run:

```text
dbt build
```

against isolated objects.

---

# 16. Very useful dbt CI pattern

Eventually:

```text
PR #527
    │
    ▼
Cloud Build
    │
    ▼
create schema

dbt_ci_527
    │
    ▼
dbt build
    │
    ▼
run tests
    │
    ▼
PASS
    │
    ▼
delete dbt_ci_527
```

This gives genuine data-level CI rather than merely checking whether Jinja compiles.

For the initial POC, however:

```text
dbt deps
+
dbt parse/compile
```

is sufficient.

---

# 17. What happens after deployment?

Suppose commit:

```text
abc123
```

is merged.

Cloud Build produces:

```text
Artifact Registry

dbt-bigquery:abc123
```

and updates:

```text
Cloud Run Job
dbt-bigquery-job

Image:
dbt-bigquery:abc123
```

**But it doesn't need to execute the job.**

This is another important separation.

Deployment:

```text
Cloud Build
     │
     ▼
Cloud Run Job updated
```

Execution:

```text
Composer
     │
     ▼
Cloud Run Job executed
```

So deployment and data processing remain independent.

---

# 18. Why I wouldn't execute dbt immediately after every deployment

You could add:

```bash
gcloud run jobs execute dbt-bigquery-job
```

to Cloud Build.

But then:

```text
code merge
   │
   ▼
deployment
   │
   ▼
production transformation
```

become tightly coupled.

Usually I prefer:

```text
CODE LIFECYCLE

Git
 ↓
Cloud Build
 ↓
Cloud Run deployment


DATA LIFECYCLE

source arrives
 ↓
Composer
 ↓
Cloud Run execution
 ↓
dbt
 ↓
BigQuery
```

Much cleaner.

---

# 19. Composer doesn't care about image versions

This is another nice property.

Our Composer DAG still says:

```python
run_dbt = CloudRunExecuteJobOperator(
    task_id="run_dbt",
    project_id=PROJECT_ID,
    region=REGION,
    job_name="dbt-bigquery-job",
)
```

Composer only knows:

```text
dbt-bigquery-job
```

It doesn't need to know:

```text
v1
v2
abc123
xyz789
```

Cloud Build changes the implementation behind the stable job name:

```text
Composer
    │
    ▼
dbt-bigquery-job
    │
    ├── yesterday → image abc123
    │
    └── today     → image def456
```

That's excellent decoupling.

---

# 20. Rollback becomes straightforward

Suppose:

```text
abc123 → GOOD
def456 → BAD
```

Cloud Run currently points to:

```text
dbt-bigquery:def456
```

Rollback means updating the job back to the known-good image:

```bash
gcloud run jobs update dbt-bigquery-job \
  --image=europe-west2-docker.pkg.dev/PROJECT_ID/dbt-poc/dbt-bigquery:abc123 \
  --region=europe-west2
```

Then:

```text
BAD
def456

    ↓ rollback

GOOD
abc123
```

The immutable commit-based image history makes this far safer than relying solely on `latest`.

---

# 21. Full Option 3 architecture

Now put everything together:

```text
                         DEVELOPER
                             │
                             ▼
                          Git Repo
                             │
                         Pull Request
                             │
                             ▼
                     ┌───────────────┐
                     │ CLOUD BUILD   │
                     │      CI       │
                     │               │
                     │ dbt deps      │
                     │ dbt compile   │
                     │ validation    │
                     └───────┬───────┘
                             │
                           PASS
                             │
                             ▼
                         Merge main
                             │
                             ▼
                     ┌───────────────┐
                     │ CLOUD BUILD   │
                     │      CD       │
                     └───────┬───────┘
                             │
                  ┌──────────┼──────────┐
                  ▼          ▼          ▼
               Docker     Artifact    Update
               build      Registry   Cloud Run
                                        │
                                        ▼
                                ┌──────────────┐
                                │ CLOUD RUN    │
                                │              │
                                │ dbt Job      │
                                └───────▲──────┘
                                        │
                                        │ trigger
                                        │
                                ┌───────┴──────┐
                                │   COMPOSER   │
                                └───────┬──────┘
                                        │
                                        ▼
                                      dbt
                                        │
                                        ▼
                                ┌───────────────┐
                                │   BIGQUERY    │
                                │               │
                                │ A ───────► B  │
                                └───────────────┘
```

Cloud Build's documented workflow supports building images, pushing them to Artifact Registry and automating Cloud Run deployment from source changes. ([Google Cloud Documentation][2])

## What we've achieved so far

We now have a surprisingly solid mini data platform:

| Layer              | GCP technology    | Responsibility          |
| ------------------ | ----------------- | ----------------------- |
| Source control     | Git               | dbt/DAG/deployment code |
| **CI/CD**          | **Cloud Build**   | Validate, build, deploy |
| Container registry | Artifact Registry | Versioned dbt images    |
| Orchestration      | Composer          | Schedule/dependencies   |
| Runtime            | Cloud Run Job     | Execute dbt             |
| Transformation     | dbt               | SQL models/tests        |
| Compute/storage    | BigQuery          | A → B                   |

And our flow has two deliberately separate paths:

```text
DEPLOYMENT PATH

Git
 ↓
Cloud Build
 ↓
Artifact Registry
 ↓
Cloud Run


EXECUTION PATH

Composer
 ↓
Cloud Run
 ↓
dbt
 ↓
BigQuery
```

That's the key takeaway from **Option 3**.

Next, **Option 4 — Cloud Run Functions** changes the trigger model completely. Instead of *“run this every day at 02:00”*, we'll implement **event-driven dbt** — for example **file arrives in Cloud Storage → Eventarc → Function → execute our existing Cloud Run dbt Job → Table A → Table B**. The nice part is that we can reuse the Cloud Run container, IAM separation, dbt project and Cloud Build pipeline we've already created.

[1]: https://docs.cloud.google.com/build/docs?utm_source=chatgpt.com "Cloud Build documentation  |  Google Cloud Documentation"
[2]: https://docs.cloud.google.com/build/docs/deploying-builds/deploy-cloud-run?authuser=610&utm_source=chatgpt.com "Deploying to Cloud Run using Cloud Build  |  Google Cloud Documentation"
[3]: https://docs.cloud.google.com/artifact-registry/docs/configure-cloud-build?hl=en&utm_source=chatgpt.com "Connect to Cloud Build  |  Artifact Registry  |  Google Cloud Documentation"
