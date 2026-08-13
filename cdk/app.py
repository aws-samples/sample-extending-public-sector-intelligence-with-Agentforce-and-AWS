#!/usr/bin/env python3
import os
import json
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions
from bda_idp_cdk.bda_processing_stack import BdaProcessingStack
from bda_idp_cdk.mcp_gateway_stack import McpGatewayStack

app = cdk.App()

# Load configuration from CDK context
def get_nested_context(section: str, key: str, default=None):
    """Helper function to get nested context values with fallback to default"""
    section_data = app.node.try_get_context(section)
    if section_data is None:
        if default is None:
            raise ValueError(f"Required context section '{section}' not found. Please create cdk.context.json from cdk.context.example.json")
        return default
    
    value = section_data.get(key)
    if value is None and default is None:
        raise ValueError(f"Required context value '{section}.{key}' not found. Please check your cdk.context.json")
    return value if value is not None else default

# Define configuration parameters from context
deployment_config = app.node.try_get_context("deployment")
if not deployment_config:
    raise ValueError("Required 'deployment' section not found in cdk.context.json. Please create it from cdk.context.example.json")

BUCKET_NAME = deployment_config.get("input-bucket-name")
BDA_PROJECT_ARN = deployment_config.get("bda-project-arn")
BDA_STAGE = deployment_config.get("bda-stage", "LIVE")
ENVIRONMENT = deployment_config.get("environment", "dev")
REGION = deployment_config.get("region", "us-east-1")
CREATE_BUCKET = deployment_config.get("create-input-bucket", True)
S3_TRIGGER_PREFIX = deployment_config.get("s3-trigger-prefix", "__sfdcroot__/")
OUTPUT_BUCKET_NAME = deployment_config.get("output-bucket-name", "")
CREATE_OUTPUT_BUCKET = deployment_config.get("create-output-bucket", False)

if not BUCKET_NAME:
    raise ValueError("Required 'deployment.input-bucket-name' not found in cdk.context.json")
if not BDA_PROJECT_ARN and ENABLE_BDA:
    raise ValueError("Required 'deployment.bda-project-arn' not found in cdk.context.json (required when enable-bda is true)")

# Load supported file types from JSON configuration
with open("bda-supported-file-types.json", "r") as f:
    file_types_config = json.load(f)

# Combine all file types from all categories
PROCESSED_FILE_TYPES = []
for category, extensions in file_types_config.items():
    PROCESSED_FILE_TYPES.extend(extensions)

# Or select specific categories only:
# PROCESSED_FILE_TYPES = file_types_config["document"]  # Only documents
# PROCESSED_FILE_TYPES = file_types_config["document"] + file_types_config["image"]  # Documents + images

# CORS allowed origins from context
cors_config = app.node.try_get_context("cors") or {}
CORS_ALLOWED_ORIGINS = cors_config.get("allowed-origins", ["https://*.force.com"])

# Lambda configuration from context
lambda_config = app.node.try_get_context("lambda") or {}
LAMBDA_MEMORY_SIZE = lambda_config.get("memory-size", 1024)
LAMBDA_TIMEOUT = lambda_config.get("timeout", 300)

# Feature flags from context
features_config = app.node.try_get_context("features") or {}
WRITE_SF_METADATA = features_config.get("write-sf-metadata", False)
ENABLE_BDA = features_config.get("enable-bda", True)

# Define the environment
env = cdk.Environment(account=os.getenv('CDK_DEFAULT_ACCOUNT'), region=REGION)

# Deploy the main BDA processing stack
main_stack_id = f"BdaProcessingStack-{ENVIRONMENT}"
main_stack = BdaProcessingStack(app, main_stack_id, 
                           bucket_name=BUCKET_NAME,
                           bda_project_arn=BDA_PROJECT_ARN,
                           bda_stage=BDA_STAGE,
                           environment=ENVIRONMENT,
                           create_bucket=CREATE_BUCKET,
                           cors_allowed_origins=CORS_ALLOWED_ORIGINS,
                           processed_file_types=PROCESSED_FILE_TYPES,
                           s3_trigger_prefix=S3_TRIGGER_PREFIX,
                           output_bucket_name=OUTPUT_BUCKET_NAME,
                           create_output_bucket=CREATE_OUTPUT_BUCKET,
                           write_sf_metadata=WRITE_SF_METADATA,
                           enable_bda=ENABLE_BDA,
                           lambda_memory_size=LAMBDA_MEMORY_SIZE,
                           lambda_timeout=LAMBDA_TIMEOUT,
                           env=env)

# Deploy the MCP Gateway stack (deploy separately with: cdk deploy McpGatewayStack-{env})
mcp_config = app.node.try_get_context("mcp") or {}
MCP_KNOWLEDGE_BASE_ID = mcp_config.get("knowledge-base-id", "")
MCP_ENABLE_SEMANTIC_SEARCH = mcp_config.get("enable-semantic-search", False)
MCP_DYNAMODB_TABLE_NAME = mcp_config.get("dynamodb-table-name", f"{BUCKET_NAME}-{ENVIRONMENT}-documents")

MCP_OUTPUT_BUCKET_NAME = deployment_config.get("output-bucket-name", "")

mcp_stack_id = f"McpGatewayStack-{ENVIRONMENT}"
mcp_stack = McpGatewayStack(app, mcp_stack_id,
                            knowledge_base_id=MCP_KNOWLEDGE_BASE_ID,
                            enable_semantic_search=MCP_ENABLE_SEMANTIC_SEARCH,
                            dynamodb_table_name=MCP_DYNAMODB_TABLE_NAME,
                            output_bucket_name=MCP_OUTPUT_BUCKET_NAME,
                            environment=ENVIRONMENT,
                            env=env)

# Add CDK Nag checks (disabled for now)
# Uncomment to enable security checks:
# cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
