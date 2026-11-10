"""
Expense Tracker backend (AWS Lambda, Python).

Handles four actions on expenses stored in DynamoDB:
    POST   /expenses  -> add an expense
    GET    /expenses  -> list expenses (optional ?month=2026-11)
    PUT    /expenses  -> update amount / category / description
    DELETE /expenses  -> delete one expense (?expense_sk=...)

Works with an API Gateway HTTP API (payload format 2.0).
"""
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("TABLE_NAME", "ExpenseTracker")
table = boto3.resource("dynamodb").Table(TABLE_NAME)

# Amazon Bedrock (AI) settings for the "AI insights" feature
bedrock = boto3.client("bedrock-runtime", region_name="ap-south-1")
MODEL_ID = os.environ.get("MODEL_ID", "apac.amazon.nova-micro-v1:0")
CATEGORIES = {"Food", "Travel", "Bills", "Shopping", "Health", "Entertainment", "Other"}


def get_user_id(event):
    """ID of the logged-in user, added by the API Gateway JWT authorizer."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return None


# ---------- helpers ----------

# Lets the web page (on any address) read the API's answers.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "content-type,authorization",
}

def _json_default(value):
    # DynamoDB returns numbers as Decimal; JSON needs normal numbers.
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Cannot serialise {type(value)}")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, default=_json_default),
    }


def parse_body(event):
    """Returns a dict, {} if no body, or None if the body is not valid JSON."""
    raw = event.get("body")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_amount(value):
    """Turns input into a positive Decimal with 2 places, or None if invalid."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            return None
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def valid_date(text):
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


# ---------- actions ----------

def create_expense(event):
    data = parse_body(event)
    if data is None:
        return respond(400, {"error": "Request body must be valid JSON"})

    amount = parse_amount(data.get("amount"))
    if amount is None:
        return respond(400, {"error": "amount must be a positive number"})

    date = data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not valid_date(date):
        return respond(400, {"error": "date must look like 2026-11-06"})

    item = {
        "user_id": event["user_id"],
        # date first, so expenses sort by date and a month can be fetched easily
        "expense_sk": f"{date}#{uuid.uuid4().hex[:12]}",
        "date": date,
        "amount": amount,
        "category": str(data.get("category") or "Other")[:50],
        "description": str(data.get("description") or "")[:200],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    table.put_item(Item=item)
    return respond(201, item)


def list_expenses(event):
    params = event.get("queryStringParameters") or {}
    month = params.get("month")

    condition = Key("user_id").eq(event["user_id"])
    if month:
        if not valid_date(month + "-01"):
            return respond(400, {"error": "month must look like 2026-11"})
        condition = condition & Key("expense_sk").begins_with(month)

    items = []
    kwargs = {"KeyConditionExpression": condition, "ScanIndexForward": False}
    while True:
        result = table.query(**kwargs)
        items.extend(result["Items"])
        if "LastEvaluatedKey" not in result:
            break
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    total = sum((item["amount"] for item in items), Decimal("0"))
    return respond(200, {"count": len(items), "total": total, "expenses": items})


def update_expense(event):
    data = parse_body(event)
    if not data or not data.get("expense_sk"):
        return respond(400, {"error": "expense_sk is required"})

    names, values, parts = {}, {}, []

    if "amount" in data:
        amount = parse_amount(data["amount"])
        if amount is None:
            return respond(400, {"error": "amount must be a positive number"})
        names["#a"], values[":a"] = "amount", amount
        parts.append("#a = :a")
    if "category" in data:
        names["#c"], values[":c"] = "category", str(data["category"] or "Other")[:50]
        parts.append("#c = :c")
    if "description" in data:
        names["#d"], values[":d"] = "description", str(data["description"] or "")[:200]
        parts.append("#d = :d")

    if not parts:
        return respond(400, {"error": "Nothing to update"})

    try:
        result = table.update_item(
            Key={"user_id": event["user_id"], "expense_sk": data["expense_sk"]},
            UpdateExpression="SET " + ", ".join(parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(expense_sk)",
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return respond(404, {"error": "Expense not found"})
    return respond(200, result["Attributes"])


def delete_expense(event):
    params = event.get("queryStringParameters") or {}
    expense_sk = params.get("expense_sk")
    if not expense_sk:
        return respond(400, {"error": "expense_sk query parameter is required"})

    table.delete_item(Key={"user_id": event["user_id"], "expense_sk": expense_sk})
    return respond(200, {"message": "Expense deleted"})


def get_insights(event):
    """Category percentages are calculated here in code; the AI only writes advice."""
    params = event.get("queryStringParameters") or {}
    month = params.get("month")
    if not month or not valid_date(month + "-01"):
        return respond(400, {"error": "month must look like 2026-11"})

    items = []
    kwargs = {"KeyConditionExpression": Key("user_id").eq(event["user_id"])
              & Key("expense_sk").begins_with(month)}
    while True:
        result = table.query(**kwargs)
        items.extend(result["Items"])
        if "LastEvaluatedKey" not in result:
            break
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    if not items:
        return respond(200, {"month": month, "total": 0, "breakdown": [],
                             "tips": "Add some expenses for this month to get insights."})

    totals = {}
    for item in items:
        category = item["category"] if item["category"] in CATEGORIES else "Other"
        totals[category] = totals.get(category, Decimal("0")) + item["amount"]
    total = sum(totals.values(), Decimal("0"))
    breakdown = sorted(
        ({"category": c, "amount": a,
          "percent": int((a / total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))}
         for c, a in totals.items()),
        key=lambda row: -row["amount"],
    )

    facts = "\n".join(f"{b['category']}: Rs {b['amount']:.0f} ({b['percent']}%)" for b in breakdown)
    prompt = (
        "You are a personal finance assistant. Facts (already calculated, do not recalculate):\n"
        f"Total spent: Rs {total:.0f}\n{facts}\n"
        "For the top two categories, give: (1) where to cut back, (2) one cheaper alternative. "
        "Under 70 words. Be specific, no generic advice."
    )

    tips = None
    try:
        reply = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.3},
        )
        tips = reply["output"]["message"]["content"][0]["text"].strip()
    except Exception as error:  # the breakdown is still returned if the AI call fails
        print("BEDROCK ERROR:", repr(error))

    return respond(200, {"month": month, "total": total, "breakdown": breakdown, "tips": tips})


# ---------- entry point ----------

def preflight(event):
    # The browser asks this "may I?" question before POST / PUT / DELETE.
    return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}


ROUTES = {
    "OPTIONS /expenses": preflight,
    "OPTIONS /insights": preflight,
    "GET /insights": get_insights,
    "POST /expenses": create_expense,
    "GET /expenses": list_expenses,
    "PUT /expenses": update_expense,
    "DELETE /expenses": delete_expense,
}


def lambda_handler(event, context):
    handler = ROUTES.get(event.get("routeKey"))
    if handler is None:
        return respond(404, {"error": "Route not found"})
    if handler is not preflight:
        event["user_id"] = get_user_id(event)
        if not event["user_id"]:
            return respond(401, {"error": "Please sign in"})
    try:
        return handler(event)
    except Exception as error:  # shows up in CloudWatch Logs for debugging
        print("ERROR:", repr(error))
        return respond(500, {"error": "Something went wrong"})
