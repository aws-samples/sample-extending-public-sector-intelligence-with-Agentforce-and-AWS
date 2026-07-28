"""
Lambda handler for AgentCore Gateway MCP Lambda Target.

Implements semantic_search and s3_vectors_search tools,
invoked by AgentCore Gateway via the Lambda MCP target protocol.
"""

import json
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

SFDC_QUERY_STATE_MACHINE_ARN = os.environ.get("SFDC_QUERY_STATE_MACHINE_ARN", "")

logger.info(f"[INIT] KNOWLEDGE_BASE_ID={KNOWLEDGE_BASE_ID}")
logger.info(f"[INIT] SFDC_QUERY_STATE_MACHINE_ARN={SFDC_QUERY_STATE_MACHINE_ARN}")
logger.info(f"[INIT] AWS_REGION={AWS_REGION}")

_kb_client = None
_sfn_client = None


def _get_kb_client():
    global _kb_client
    if _kb_client is None:
        logger.info("[CLIENT] Initializing bedrock-agent-runtime client")
        _kb_client = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
    return _kb_client


def _get_sfn_client():
    global _sfn_client
    if _sfn_client is None:
        logger.info("[CLIENT] Initializing stepfunctions client")
        _sfn_client = boto3.client("stepfunctions", region_name=AWS_REGION)
    return _sfn_client


def semantic_search(query: str, max_results: int = 5, salesforce_object_id: str = None, salesforce_object_type: str = None) -> str:
    """Search documents using Bedrock Knowledge Base semantic similarity."""
    logger.info(f"[SEMANTIC_SEARCH] query='{query}', max_results={max_results}, sf_object_id={salesforce_object_id}, sf_object_type={salesforce_object_type}, kb_id={KNOWLEDGE_BASE_ID}")

    if not KNOWLEDGE_BASE_ID:
        logger.error("[SEMANTIC_SEARCH] KNOWLEDGE_BASE_ID not configured")
        return json.dumps({"error": "KNOWLEDGE_BASE_ID not configured"})

    # Input validation
    if not query or not isinstance(query, str) or len(query) > 1000:
        return json.dumps({"error": "Invalid query: must be a non-empty string, max 1000 characters"})
    if salesforce_object_id and (not isinstance(salesforce_object_id, str) or len(salesforce_object_id) > 18):
        return json.dumps({"error": "Invalid salesforce_object_id: must be a string, max 18 characters"})
    if salesforce_object_type and (not isinstance(salesforce_object_type, str) or len(salesforce_object_type) > 100):
        return json.dumps({"error": "Invalid salesforce_object_type: must be a string, max 100 characters"})

    max_results = max(1, min(max_results, 25))
    client = _get_kb_client()

    # Build vector search config with optional metadata filter
    vector_search_config = {"numberOfResults": max_results}

    # Build filter based on provided Salesforce metadata
    filters = []
    if salesforce_object_id:
        filters.append({
            "equals": {"key": "salesforce_object_id", "value": salesforce_object_id}
        })
    if salesforce_object_type:
        filters.append({
            "equals": {"key": "salesforce_object_type", "value": salesforce_object_type}
        })

    if len(filters) == 1:
        vector_search_config["filter"] = filters[0]
        logger.info(f"[SEMANTIC_SEARCH] Applied single filter: {json.dumps(filters[0])}")
    elif len(filters) > 1:
        vector_search_config["filter"] = {"andAll": filters}
        logger.info(f"[SEMANTIC_SEARCH] Applied AND filter with {len(filters)} conditions")

    try:
        logger.info(f"[SEMANTIC_SEARCH] Calling bedrock-agent-runtime.retrieve()")
        resp = client.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": vector_search_config
            },
        )
        logger.info(f"[SEMANTIC_SEARCH] Response keys: {list(resp.keys())}")
    except Exception as exc:
        logger.error(f"[SEMANTIC_SEARCH] KB query failed: {str(exc)}", exc_info=True)
        return json.dumps({"error": f"KB query failed: {str(exc)}"})

    retrieval_results = resp.get("retrievalResults", [])
    logger.info(f"[SEMANTIC_SEARCH] Got {len(retrieval_results)} results")

    results = []
    for i, r in enumerate(retrieval_results):
        text = r.get("content", {}).get("text", "")
        score = r.get("score", 0)
        loc = r.get("location", {})
        source = loc.get("s3Location", {}).get("uri", "unknown")
        logger.info(f"[SEMANTIC_SEARCH] Result {i}: score={score}, source={source}, text_len={len(text)}")
        results.append({"text": text, "score": score, "source": source})

    return json.dumps(results, indent=2)


def get_document_summaries(salesforce_object_id: str, filename: str = None) -> str:
    """Retrieve BDA-generated summaries for documents linked to a Salesforce record, optionally filtered to a single filename."""
    logger.info(f"[GET_DOC_SUMMARIES] salesforce_object_id={salesforce_object_id}, filename={filename}")

    if not SFDC_QUERY_STATE_MACHINE_ARN:
        logger.error("[GET_DOC_SUMMARIES] SFDC_QUERY_STATE_MACHINE_ARN not configured")
        return json.dumps({"error": "SFDC_QUERY_STATE_MACHINE_ARN not configured"})

    # Input validation
    if not salesforce_object_id or not isinstance(salesforce_object_id, str) or len(salesforce_object_id) > 18:
        return json.dumps({"error": "Invalid salesforce_object_id: must be a non-empty string, max 18 characters"})
    if filename and (not isinstance(filename, str) or len(filename) > 255 or '..' in filename or '/' in filename):
        return json.dumps({"error": "Invalid filename: must be a string, max 255 characters, no path traversal"})

    sfn_client = _get_sfn_client()
    sfn_input = {"salesforce_object_id": salesforce_object_id}
    if filename:
        sfn_input["filename"] = filename

    try:
        logger.info(f"[GET_DOC_SUMMARIES] Starting sync execution of {SFDC_QUERY_STATE_MACHINE_ARN}")
        resp = sfn_client.start_sync_execution(
            stateMachineArn=SFDC_QUERY_STATE_MACHINE_ARN,
            input=json.dumps(sfn_input),
        )
        logger.info(f"[GET_DOC_SUMMARIES] Execution status: {resp.get('status')}")
    except Exception as exc:
        logger.error(f"[GET_DOC_SUMMARIES] Step Function invocation failed: {str(exc)}", exc_info=True)
        return json.dumps({"error": f"Step Function invocation failed: {str(exc)}"})

    status = resp.get("status")
    if status != "SUCCEEDED":
        error = resp.get("error", "unknown")
        cause = resp.get("cause", "unknown")
        logger.error(f"[GET_DOC_SUMMARIES] Execution failed: status={status}, error={error}, cause={cause}")
        return json.dumps({"error": f"Step Function execution {status}", "details": cause})

    # Parse the output from the step function
    output_str = resp.get("output", "{}")
    logger.info(f"[GET_DOC_SUMMARIES] Output length: {len(output_str)} chars")
    return output_str


# Tool dispatch map
TOOLS = {
    "semantic_search": semantic_search,
    "get_document_summaries": get_document_summaries,
}


def lambda_handler(event, context):
    """
    AgentCore Gateway Lambda Target handler.
    The gateway sends only the tool arguments — no tool name.
    We determine the tool by inspecting which arguments are present.
    """
    logger.info(f"[HANDLER] ===== INVOCATION START =====")
    logger.info(f"[HANDLER] Event: {json.dumps(event, default=str)}")

    # The gateway sends raw arguments only — no tool name.
    # Determine tool by argument signature:
    #   - "salesforce_object_id" without "query" → get_document_summaries
    #   - "query" with any combo of max_results/salesforce_object_id/salesforce_object_type → semantic_search
    if "salesforce_object_id" in event and "query" not in event:
        tool_name = "get_document_summaries"
    elif "query" in event:
        tool_name = "semantic_search"
    else:
        logger.error(f"[HANDLER] Cannot determine tool from args: {list(event.keys())}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Cannot determine tool from arguments: {list(event.keys())}"})
        }

    logger.info(f"[HANDLER] Resolved tool: {tool_name}")

    try:
        logger.info(f"[HANDLER] Dispatching {tool_name} with args: {json.dumps(event, default=str)}")
        result = TOOLS[tool_name](**event)
        logger.info(f"[HANDLER] Tool returned {len(result)} chars")
        logger.info(f"[HANDLER] ===== INVOCATION SUCCESS =====")
        return {
            "statusCode": 200,
            "body": result
        }
    except TypeError as exc:
        logger.error(f"[HANDLER] Argument mismatch for {tool_name}: {str(exc)}", exc_info=True)
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Argument mismatch: {str(exc)}", "event": event})
        }
    except Exception as exc:
        logger.error(f"[HANDLER] Tool execution error: {str(exc)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Tool execution failed: {str(exc)}"})
        }
