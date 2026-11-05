import json
import os
import time

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
ORDERS_TABLE = dynamodb.Table(os.environ.get("ORDERS_TABLE", ""))
INVENTORY_TABLE = dynamodb.Table(os.environ.get("INVENTORY_TABLE", ""))
client = dynamodb.meta.client


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            _process_record(record)
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _process_record(record):
    body = json.loads(record["body"])
    order_id = body["order_id"]
    items = body["items"]

    order = ORDERS_TABLE.get_item(Key={"pk": f"ORDER#{order_id}"}).get("Item")
    if not order or order.get("status") != "RECEIVED":
        return  # already handled, or unknown: nothing to do (redelivery-safe)

    now = int(time.time())
    transact_items = []
    for item in items:
        transact_items.append(
            {
                "Update": {
                    "TableName": INVENTORY_TABLE.name,
                    "Key": {"sku": item["sku"]},
                    "UpdateExpression": "SET stock = stock - :qty",
                    "ConditionExpression": "stock >= :qty",
                    "ExpressionAttributeValues": {":qty": item["qty"]},
                }
            }
        )
    transact_items.append(
        {
            "Update": {
                "TableName": ORDERS_TABLE.name,
                "Key": {"pk": f"ORDER#{order_id}"},
                "UpdateExpression": "SET #status = :confirmed, updated_at = :now",
                "ConditionExpression": "#status = :received",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":confirmed": "CONFIRMED", ":received": "RECEIVED", ":now": now},
            }
        }
    )

    try:
        client.transact_write_items(TransactItems=transact_items)
        print(f"ORDER_CONFIRMED order_id={order_id}")

    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                # Genuinely out of stock: terminal state, message is done.
                ORDERS_TABLE.update_item(
                    Key={"pk": f"ORDER#{order_id}"},
                    UpdateExpression="SET #status = :out, updated_at = :now",
                    ConditionExpression="#status = :received",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":out": "OUT_OF_STOCK", ":received": "RECEIVED", ":now": now},

                )
                return
        raise  # transient problem: fail the message so SQS retries it
