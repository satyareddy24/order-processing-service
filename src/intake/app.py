import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
ORDERS_TABLE = dynamodb.Table(os.environ.get("ORDERS_TABLE", ""))


def lambda_handler(event, context):
    if event.get("httpMethod") == "GET":
        return _get_order(event)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})

    items = body.get("items")
    if not isinstance(items, list) or not items:
        return _response(400, {"error": "items must be a non-empty list"})
    if len(items) > 10:
        return _response(400, {"error": "at most 10 items per order"})
    for item in items:
        if not isinstance(item, dict) or "sku" not in item or "qty" not in item:
            return _response(400, {"error": "each item needs sku and qty"})
        if not isinstance(item["qty"], int) or not 1 <= item["qty"] <= 100:
            return _response(400, {"error": "qty must be 1-100"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    idempotency_key = headers.get("idempotency-key") or str(uuid.uuid4())

    order_id = str(uuid.uuid4())
    now = int(time.time())

    # Claim the key first. If it already exists, this is a retry: return the original order.
    try:
        ORDERS_TABLE.put_item(
            Item={"pk": f"IDEMPOTENCY#{idempotency_key}", "order_id": order_id, "created_at": now},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _duplicate_response(idempotency_key)
        raise

    ORDERS_TABLE.put_item(
        Item={
            "pk": f"ORDER#{order_id}",
            "order_id": order_id,
            "status": "RECEIVED",
            "items": items,
            "idempotency_key": idempotency_key,
            "created_at": now,
            "updated_at": now,
        }
    )

    message = {"order_id": order_id, "idempotency_key": idempotency_key, "items": items}
    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(message))
    except Exception:
        return _response(500, {"error": "failed to queue order"})
    print(f"ORDER_ACCEPTED order_id={order_id} status=RECEIVED")
    return _response(202, {"order_id": order_id, "status": "RECEIVED"})


def _duplicate_response(idempotency_key):
    claim = ORDERS_TABLE.get_item(Key={"pk": f"IDEMPOTENCY#{idempotency_key}"}).get("Item", {})
    order_id = claim.get("order_id")
    if order_id:
        order = ORDERS_TABLE.get_item(Key={"pk": f"ORDER#{order_id}"}).get("Item", {})
        return _response(200, {"order_id": order_id, "status": order.get("status", "RECEIVED"), "duplicate": True})
    return _response(200, {"duplicate": True})


def _get_order(event):
    order_id = (event.get("pathParameters") or {}).get("id")
    order = ORDERS_TABLE.get_item(Key={"pk": f"ORDER#{order_id}"}).get("Item")
    if not order:
        return _response(404, {"error": "order not found"})
    items = [{"sku": item["sku"], "qty": int(item["qty"])} for item in order["items"]]
    return _response(200, {"order_id": order["order_id"], "status": order["status"], "items": items})

def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
