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
    AND DATE(transaction_timestamp) = DATE('{{ var("processing_date") }}')