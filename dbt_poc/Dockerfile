FROM python:3.12-slim

WORKDIR /app

COPY dbt_poc/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DBT_PROFILES_DIR=/app

CMD ["dbt", "build", "--select", "transform_table_b"]