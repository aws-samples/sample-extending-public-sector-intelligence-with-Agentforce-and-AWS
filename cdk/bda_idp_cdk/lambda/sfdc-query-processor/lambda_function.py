"""SFDC Query Processor Lambda.

Receives DynamoDB query results (items matching a salesforce_object_id)
and collects BDA summaries from S3 for each document.

Environment Variables:
    None required — bucket info comes from the DynamoDB items themselves.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")


def _deserialize_value(typed_value):
    """Deserialize a single DynamoDB typed value to a plain Python value."""
    if "S" in typed_value:
        return typed_value["S"]
    if "N" in typed_value:
        return typed_value["N"]
    if "L" in typed_value:
        return [_deserialize_value(item) for item in typed_value["L"]]
    if "M" in typed_value:
        return {k: _deserialize_value(v) for k, v in typed_value["M"].items()}
    if "BOOL" in typed_value:
        return typed_value["BOOL"]
    if "NULL" in typed_value:
        return None
    # Fallback: return as-is
    return typed_value


def _deserialize_item(item):
    """Deserialize a DynamoDB typed item dict to a plain dict."""
    return {key: _deserialize_value(value) for key, value in item.items()}


def _extract_summary(result):
    """Extract the summary and modality from a BDA result based on its modality.

    BDA output structure by modality:
        IMAGE:    result["image"]["summary"]
        DOCUMENT: result["document"]["summary"]
        VIDEO:    result["video"]["summary"]
        AUDIO:    result["audio"]["summary"]

    Returns a tuple of (modality, summary) or (None, None).
    """
    if not isinstance(result, dict) or "error" in result:
        return None, None

    modality = result.get("metadata", {}).get("semantic_modality", "")
    modality_key = modality.lower()  # IMAGE -> image, DOCUMENT -> document, VIDEO -> video, AUDIO -> audio

    modality_data = result.get(modality_key, {})
    if isinstance(modality_data, dict):
        return modality, modality_data.get("summary")

    return modality, None


def _fetch_s3_json(bucket, key):
    """Fetch and parse a JSON file from S3.

    Returns the parsed JSON or an error dict if retrieval fails.
    """
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode("utf-8")
        return json.loads(content)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.error("S3 error fetching %s/%s: %s", bucket, key, error_code)
        return {"error": error_code, "bucket": bucket, "key": key}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s/%s: %s", bucket, key, str(e))
        return {"error": "InvalidJSON", "bucket": bucket, "key": key}


def lambda_handler(event, context):
    """Process DynamoDB query results and extract BDA summaries from S3.

    Args:
        event: Dict containing:
            - salesforce_object_id: The Salesforce record ID
            - items: List of DynamoDB items (in typed attribute format)
        context: Lambda context object.

    Returns:
        dict with statusCode and extracted summaries per document.
    """
    logger.info("Received event: %s", json.dumps(event, default=str))

    salesforce_object_id = event.get("salesforce_object_id", "")
    raw_items = event.get("items", [])

    logger.info(
        "Processing %d items for salesforce_object_id: %s",
        len(raw_items),
        salesforce_object_id,
    )

    documents = []

    for raw_item in raw_items:
        # Deserialize DynamoDB typed attributes to plain values
        item = _deserialize_item(raw_item)

        document_id = item.get("document_id", "unknown")
        filename = item.get("filename", "unknown")
        status = item.get("status", "unknown")
        output_bucket = item.get("output_bucket", "")
        output_file_keys = item.get("output_file_keys", [])

        # Skip items that are not completed or have no output
        if status != "completed":
            logger.info("Skipping document %s — status: %s", document_id, status)
            continue

        if not output_bucket or not output_file_keys:
            logger.info(
                "Skipping document %s — missing output_bucket or output_file_keys",
                document_id,
            )
            continue

        # Fetch each output file from S3 and extract the summary
        summaries = []
        modality = None
        for key in output_file_keys:
            logger.info("Fetching s3://%s/%s", output_bucket, key)
            result = _fetch_s3_json(output_bucket, key)
            result_modality, summary = _extract_summary(result)

            if result_modality and not modality:
                modality = result_modality

            if summary:
                summaries.append(summary)
            else:
                logger.info(
                    "No summary found in result for document %s, key %s",
                    document_id,
                    key,
                )

        documents.append(
            {
                "document_id": document_id,
                "filename": filename,
                "modality": modality,
                "summaries": summaries,
            }
        )

    logger.info(
        "Collected summaries for %d completed documents out of %d total items",
        len(documents),
        len(raw_items),
    )

    return {
        "statusCode": 200,
        "body": {
            "salesforce_object_id": salesforce_object_id,
            "total_items": len(raw_items),
            "documents_processed": len(documents),
            "documents": documents,
        },
    }
