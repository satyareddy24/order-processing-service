# Scalable Order Processing Service

An asynchronous order-processing service on AWS. The intake accepts an order and replies immediately; a worker confirms it afterwards through a queue. Built with API Gateway, Lambda, SQS, DynamoDB, and CloudWatch, defined as code with AWS SAM.

## Architecture

```
Client (curl / k6)
    |
    v
[API Gateway]  POST /orders, GET /orders/{id}
    |
    v
[Intake Lambda]  validate -> claim idempotency key -> save RECEIVED -> enqueue
    |
    v
[SQS OrdersQueue]  (DLQ after 3 failed attempts)
    |
    v
[Worker Lambda]  one DynamoDB transaction: decrement stock + confirm order
    |
    v
[DynamoDB]  OrdersTable (orders + idempotency claims), InventoryTable (stock)

[CloudWatch]  logs for both Lambdas, alarms on DLQ depth, error rate, queue age
```

## How it works

- **Async by design.** `POST /orders` returns `202 RECEIVED` at once with an `order_id`. The order is confirmed later by the worker, so intake stays fast under bursts and a worker crash never loses an order — the message is retried.
- **Idempotency.** Each request carries an `Idempotency-Key` header. The key is claimed with a conditional write (`IDEMPOTENCY#<key>`), so a retried POST returns `200` with the same `order_id` and `"duplicate": true` instead of creating a second order or charging twice.
- **Atomic confirmation.** The worker runs one DynamoDB transaction per order: decrement inventory for every item (only if `stock >= qty`) and flip the order to `CONFIRMED`. All or nothing — stock can never go negative and no order is half-processed. Out of stock becomes `OUT_OF_STOCK`, a terminal state.
- **Failure handling.** A message that fails 3 times moves to the dead-letter queue. The worker reports partial batch failures so only failed messages are retried. CloudWatch alarms fire on DLQ depth, Lambda errors, and queue age.
- **Limits.** Max 10 items per order, quantity 1–100 per item. SQS batch size 5, visibility timeout 180s, Lambda timeout 30s.

## API

| Method | Endpoint | Success | Notes |
|---|---|---|---|
| POST | `/orders` | `202` + `{"order_id", "status": "RECEIVED"}` | Header `Idempotency-Key: <unique-key>`; body `{"items": [{"sku": "SKU-1", "qty": 1}]}` |
| POST | `/orders` (same key again) | `200` + `"duplicate": true`, same `order_id` | No second order, no second stock decrement |
| GET | `/orders/{order_id}` | `200` + `{"order_id", "status", "items"}` | `RECEIVED` first, `CONFIRMED` once the worker finishes |

## Measured results (k6, Nov 2026)

Load-tested against the live deployment with SKU-1 seeded to 100,000 units.

| Run | Requests | Passed | p95 latency | Result |
|---|---|---|---|---|
| 5 VUs, ~9 min | 3,438 | 99.88% (3,434) | 288 ms | Pass. DLQ stayed 0; stock 100,000 → 96,562, exactly 3,438 decremented, so every request's order confirmed. The 4 failures were client request timeouts, not 500s. |
| 50 VUs, 5 min | 70,119 | 35.44% (24,857) | 219 ms | Fail. 64.55% got generic API Gateway 500s. Root cause found and proved: a brand-new AWS account allows only 10 concurrent Lambda executions (shared by intake + worker); 10 slots / ~0.13s per invocation ≈ 77/s matches the ~78–83/s success cap in all runs. Throttled requests never execute, so they leave no logs. |

## Quickstart

Prerequisites: an AWS account, AWS CLI, SAM CLI, Python 3.12. No credentials live in this repo — they are stored by `aws configure` in `~/.aws`.

```bash
# 1. Deploy (first time answers a few prompts and saves them to samconfig.toml)
sam build
sam deploy --guided

# Later deploys
sam build
sam deploy

# 2. Get the API URL from the stack outputs
API_URL=$(aws cloudformation describe-stacks --stack-name order-processing-service \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text --region us-east-1)

# 3. Seed stock
TABLE=$(aws dynamodb list-tables --region us-east-1 \
  --query "TableNames[?contains(@, 'Inventory')]" --output text)
aws dynamodb put-item --table-name "$TABLE" \
  --item '{"sku": {"S": "SKU-1"}, "stock": {"N": "100000"}}' --region us-east-1

# 4. Place an order, then check it
curl -i -X POST "$API_URL" -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-1" -d '{"items":[{"sku":"SKU-1","qty":1}]}'
curl "$API_URL/<order_id>"
```

## Load test

```bash
k6 run -e API_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com/Prod load/k6-step7.js
```

(`API_URL` ends in `/Prod`; the script appends `/orders`. Seed stock first — see above.)

## Watch it live

```bash
# Intake logs (streaming)
aws logs tail /aws/lambda/order-processing-service-IntakeFunction-<suffix> --follow --region us-east-1
# Worker logs (streaming)
aws logs tail /aws/lambda/order-processing-service-WorkerFunction-<suffix> --follow --region us-east-1
```

Get the exact function names from `aws lambda list-functions --region us-east-1` (or `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/order-processing-service --region us-east-1`) instead of retyping the suffixes. With the log lines in this repo, the intake prints `ORDER_ACCEPTED order_id=...` and the worker prints `ORDER_CONFIRMED order_id=...`.

## Project structure

```
template.yaml            SAM template: API, both Lambdas, SQS + DLQ, DynamoDB tables, CloudWatch alarms
src/intake/app.py        Intake Lambda: validate, claim idempotency key, save RECEIVED, enqueue, GET status
src/worker/app.py        Worker Lambda: SQS batch, DynamoDB transaction, CONFIRMED / OUT_OF_STOCK / retry
load/k6-step7.js         k6 load-test script
load/k6-probe.js         1-minute probe script
samconfig.toml           Saved sam deploy answers (stack name, region — no secrets)
```

## Cleanup

```bash
sam delete
```

The project fits in the AWS free tier for this scale; delete the stack when you are done with it.

## Known limits

- No authentication on the API — fine for a demo, not for real users (production would add an API key or Cognito).
- Deploys run with `sam deploy` from a machine; there is no CI/CD pipeline in this repo.
- A new AWS account caps Lambda concurrency at 10 — request an increase in Service Quotas before load-testing with many concurrent users.
- Single-SKU demo (SKU-1); no frontend or payment handling.
