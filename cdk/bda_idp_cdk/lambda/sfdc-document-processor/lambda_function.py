"""SFDC Document Processor Lambda.

Receives DynamoDB query results (items matching a salesforce_object_id)
and filters to a specific filename, returning the BDA summary for that document.

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
    modality_key = modality.lower()

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


def _trim_bda_result(result):
    """Trim heavy fields from a BDA result to reduce payload size.

    Removes bounding box data, frame-level details, and per-word/per-segment
    breakdowns while preserving summaries, metadata, statistics, transcripts,
    and chapter-level information.
    """
    trimmed = {}

    # Always keep metadata and statistics
    if "metadata" in result:
        trimmed["metadata"] = result["metadata"]
    if "statistics" in result:
        trimmed["statistics"] = result["statistics"]

    modality = result.get("metadata", {}).get("semantic_modality", "").lower()

    # IMAGE: keep summary, drop text_words/text_lines bounding boxes
    if modality == "image" and "image" in result:
        image_data = result["image"]
        trimmed["image"] = {"summary": image_data.get("summary")}

    # DOCUMENT: keep summary, description, and raw text content
    if modality == "document" and "document" in result:
        doc_data = result["document"]
        trimmed["document"] = {
            "summary": doc_data.get("summary"),
            "description": doc_data.get("description"),
        }
        # Extract raw text from representation (prefer markdown > text > html)
        rep = doc_data.get("representation", {})
        if isinstance(rep, dict):
            content = rep.get("markdown") or rep.get("text") or rep.get("html")
            if content:
                trimmed["document"]["content"] = content

    # VIDEO: keep summary, full transcript (or chapter transcripts as fallback), drop shots/frames
    if modality == "video":
        if "video" in result:
            video_data = result["video"]
            trimmed["video"] = {"summary": video_data.get("summary")}

            # Prioritize full transcript over chapter transcripts
            full_transcript = video_data.get("transcript", {}).get("representation", {}).get("text")
            if full_transcript:
                trimmed["video"]["transcript"] = full_transcript
            elif "chapters" in result:
                # Fall back to chapter-level transcripts
                chapter_transcripts = []
                for chapter in result["chapters"]:
                    ct = {
                        "chapter_index": chapter.get("chapter_index"),
                        "summary": chapter.get("summary"),
                    }
                    transcript = chapter.get("transcript", {})
                    if transcript:
                        rep = transcript.get("representation", {})
                        if rep:
                            ct["transcript_text"] = rep.get("text")
                    chapter_transcripts.append(ct)
                trimmed["chapter_transcripts"] = chapter_transcripts

    # AUDIO: keep summary, full transcript (or topic transcripts as fallback)
    if modality == "audio":
        if "audio" in result:
            audio_data = result["audio"]
            trimmed["audio"] = {"summary": audio_data.get("summary")}

            # Prioritize full transcript over topic transcripts
            full_transcript = audio_data.get("transcript", {}).get("representation", {}).get("text")
            if full_transcript:
                trimmed["audio"]["transcript"] = full_transcript
            elif "topics" in result:
                # Fall back to topic-level transcripts
                topic_transcripts = []
                for topic in result["topics"]:
                    tt = {
                        "topic_index": topic.get("topic_index"),
                        "summary": topic.get("summary"),
                    }
                    transcript = topic.get("transcript", {})
                    if transcript:
                        rep = transcript.get("representation", {})
                        if rep:
                            tt["transcript_text"] = rep.get("text")
                    topic_transcripts.append(tt)
                trimmed["topic_transcripts"] = topic_transcripts

    return trimmed


def lambda_handler(event, context):
    """Process DynamoDB query results, filter to a specific filename, and extract its BDA summary.

    Args:
        event: Dict containing:
            - salesforce_object_id: The Salesforce record ID
            - filename: The filename to filter to
            - items: List of DynamoDB items (in typed attribute format)
        context: Lambda context object.

    Returns:
        dict with statusCode and the extracted summary for the matching document.
    """
    logger.info("Received event: %s", json.dumps(event, default=str))

    salesforce_object_id = event.get("salesforce_object_id", "")
    target_filename = event.get("filename", "")
    raw_items = event.get("items", [])

    # Input validation
    if not salesforce_object_id or not isinstance(salesforce_object_id, str) or len(salesforce_object_id) > 18:
        return {
            "statusCode": 400,
            "body": {"error": "Invalid salesforce_object_id: must be a non-empty string, max 18 characters"},
        }
    if not isinstance(raw_items, list):
        return {
            "statusCode": 400,
            "body": {"error": "Invalid items: must be a list"},
        }

    logger.info(
        "Processing %d items for salesforce_object_id: %s, filtering to filename: %s",
        len(raw_items),
        salesforce_object_id,
        target_filename,
    )

    if not target_filename:
        return {
            "statusCode": 400,
            "body": {
                "error": "filename is required",
                "salesforce_object_id": salesforce_object_id,
            },
        }

    # Find the matching item by filename
    matched_item = None
    for raw_item in raw_items:
        item = _deserialize_item(raw_item)
        if item.get("filename") == target_filename:
            matched_item = item
            break

    if not matched_item:
        return {
            "statusCode": 404,
            "body": {
                "message": f"No document found with filename '{target_filename}'",
                "salesforce_object_id": salesforce_object_id,
                "filename": target_filename,
            },
        }

    document_id = matched_item.get("document_id", "unknown")
    status = matched_item.get("status", "unknown")
    output_bucket = matched_item.get("output_bucket", "")
    output_file_keys = matched_item.get("output_file_keys", [])

    if status != "completed":
        return {
            "statusCode": 200,
            "body": {
                "salesforce_object_id": salesforce_object_id,
                "document_id": document_id,
                "filename": target_filename,
                "status": status,
                "message": f"Document processing not complete (status: {status})",
            },
        }

    if not output_bucket or not output_file_keys:
        return {
            "statusCode": 200,
            "body": {
                "salesforce_object_id": salesforce_object_id,
                "document_id": document_id,
                "filename": target_filename,
                "status": status,
                "message": "Document has no output files available",
            },
        }

    # Fetch BDA results — trim heavy fields and check payload size
    bda_results = []
    for key in output_file_keys:
        logger.info("Fetching s3://%s/%s", output_bucket, key)
        result = _fetch_s3_json(output_bucket, key)
        if isinstance(result, dict) and "error" not in result:
            trimmed = _trim_bda_result(result)
            bda_results.append(trimmed)
        else:
            bda_results.append(result)

    response_body = {
        "salesforce_object_id": salesforce_object_id,
        "document_id": document_id,
        "filename": target_filename,
        "bda_results": bda_results,
    }

    # Check if payload exceeds Step Functions limit (256 KB)
    # Use 200 KB as a safe threshold to account for wrapper overhead
    MAX_PAYLOAD_BYTES = 200 * 1024
    payload_size = len(json.dumps(response_body))
    logger.info("Response payload size: %d bytes", payload_size)

    if payload_size > MAX_PAYLOAD_BYTES:
        logger.warning(
            "Payload size %d exceeds %d byte limit — falling back to summary only",
            payload_size,
            MAX_PAYLOAD_BYTES,
        )
        # Fall back to summary-only response
        modality = None
        summaries = []
        for result in bda_results:
            if isinstance(result, dict) and "error" not in result:
                m = result.get("metadata", {}).get("semantic_modality", "")
                if m and not modality:
                    modality = m
                modality_key = m.lower() if m else ""
                modality_data = result.get(modality_key, {})
                if isinstance(modality_data, dict) and modality_data.get("summary"):
                    summaries.append(modality_data["summary"])

        response_body = {
            "salesforce_object_id": salesforce_object_id,
            "document_id": document_id,
            "filename": target_filename,
            "modality": modality,
            "summaries": summaries,
            "_truncated": True,
            "_message": "Full BDA output exceeded size limit; returning summaries only",
        }

    return {
        "statusCode": 200,
        "body": response_body,
    }
