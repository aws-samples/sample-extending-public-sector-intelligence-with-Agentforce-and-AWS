# AWS CDK Deployment Configuration

This document describes the configuration parameters available in `cdk.context.json` for deploying the Extending Public Sector Intelligence with Agentforce and AWS infrastructure.

## Quick Start

```bash
cp cdk.context.example.json cdk.context.json
# Edit cdk.context.json with your values
```

---

## Configuration Sections

### `deployment` (required)

Core infrastructure parameters for the BdaProcessingStack.

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `input-bucket-name` | Yes | — | S3 bucket name for document input (see bucket creation notes below) |
| `create-input-bucket` | No | `true` | Whether CDK creates a new bucket or references an existing one |
| `output-bucket-name` | No | `""` | S3 bucket for BDA output (uses input bucket if empty) |
| `create-output-bucket` | No | `true` | Whether CDK creates a new output bucket or references an existing one |

#### Bucket Creation Behavior

`create-input-bucket` and `create-output-bucket` are both `true` by default. This controls whether CDK creates new S3 buckets or references existing ones:

**When set to `true` (default):**
- CDK creates a new S3 bucket with the name you provide.
- The bucket name **must be globally unique** across all AWS accounts. If the name is already taken, deployment will fail.
- CDK automatically configures the bucket with Block Public Access, SSE-S3 encryption, TLS enforcement, access logging, and CORS rules.

**When set to `false`:**
- CDK references an existing bucket by name. The bucket must already exist in the target account and region.
- CDK will **not** modify the existing bucket's policies, encryption settings, or CORS configuration.
- If the existing bucket has custom bucket policies, they may conflict with the permissions the Lambda functions need. You may need to manually grant `s3:GetObject`, `s3:PutObject`, and `s3:ListBucket` to the Lambda execution roles.
- CORS permissions required for Salesforce integration (`PUT`, `GET`, `HEAD` from your `*.force.com` origin) must be configured manually on the existing bucket.
| `bda-project-arn` | Yes* | — | Amazon Bedrock Data Automation project ARN (*required when `enable-bda` is true) |
| `bda-stage` | No | `LIVE` | `LIVE` or `DRAFT` |
| `environment` | No | `dev` | `dev`, `staging`, or `prod` — controls retention policies and log levels |
| `region` | No | `us-east-1` | AWS region for deployment |
| `s3-trigger-prefix` | No | `__sfdcroot__/` | S3 key prefix that triggers BDA processing on upload |

### `lambda` (optional)

Lambda function sizing for the BdaProcessingStack functions.

| Key | Default | Description |
|-----|---------|-------------|
| `memory-size` | `1024` | Lambda memory in MB (128–10240) |
| `timeout` | `300` | Lambda timeout in seconds (1–900) |

### `cors` (optional)

Cross-origin resource sharing configuration for the S3 document bucket.

| Key | Default | Description |
|-----|---------|-------------|
| `allowed-origins` | `["https://*.force.com"]` | Origins permitted for CORS requests |

### `features` (optional)

Feature flags to enable or disable capabilities.

| Key | Default | Description |
|-----|---------|-------------|
| `enable-bda` | `true` | Enable Amazon Bedrock Data Automation processing |
| `write-sf-metadata` | `false` | Write a `.metadata.json` sidecar file alongside each input document in S3 |

#### About `write-sf-metadata`

When enabled, the ingestion Lambda writes a `{filename}.metadata.json` file next to the uploaded document in the input bucket. This file contains Salesforce record metadata extracted from the S3 path or object metadata:

```json
{
  "metadataAttributes": {
    "salesforce_object_id": "5001a00000ABC123",
    "salesforce_object_type": "Case"
  }
}
```

This sidecar file is used by Amazon Bedrock Knowledge Bases as a [metadata attributes file](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ds-s3.html). When the Knowledge Base ingests your S3 data, it picks up these attributes and attaches them to the corresponding document vectors. This enables the `semantic_search` MCP tool to filter results by Salesforce record — for example, returning only documents linked to a specific Case ID.

If you are not using semantic search or do not need per-record filtering, you can safely leave this as `false` (the default). Set it to `true` when you enable semantic search and want to filter results by Salesforce record.

### `logging` (optional)

Log retention configuration.

| Key | Default | Description |
|-----|---------|-------------|
| `s3-log-retention-days` | `90` | Days to retain S3 access logs |
| `cloudwatch-log-retention-days` | `30` | Days to retain CloudWatch logs |

### `mcp` (optional — for McpGatewayStack)

Configuration for the AgentCore MCP Gateway and semantic search tools.

| Key | Default | Description |
|-----|---------|-------------|
| `knowledge-base-id` | `""` | Amazon Bedrock Knowledge Base ID for `semantic_search` tool |
| `enable-semantic-search` | `false` | Enable the `semantic_search` tool (requires `knowledge-base-id`) |
| `dynamodb-table-name` | `{input-bucket-name}-{environment}-documents` | DynamoDB table name for document queries |

---

## Supported File Types

The file `bda-supported-file-types.json` defines which file extensions trigger BDA processing when uploaded to the input bucket. Extensions are organized by media category:

| Category | Extensions |
|----------|-----------|
| `document` | `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls`, `.txt`, `.rtf`, `.html`, `.htm`, `.md`, `.csv`, `.tsv` |
| `image` | `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp` |
| `audio` | `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, `.m4a` |
| `video` | `.mp4`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.mkv` |

By default, `app.py` includes **all categories**. To limit processing to specific media types, edit `app.py`:

```python
# Process all file types (default)
PROCESSED_FILE_TYPES = []
for category, extensions in file_types_config.items():
    PROCESSED_FILE_TYPES.extend(extensions)

# Process only documents
PROCESSED_FILE_TYPES = file_types_config["document"]

# Process documents and images
PROCESSED_FILE_TYPES = file_types_config["document"] + file_types_config["image"]

# Process specific extensions
PROCESSED_FILE_TYPES = [".pdf", ".png", ".mp4"]
```

Only files matching these extensions will trigger BDA processing when uploaded to the S3 trigger prefix path. Other files are stored but not processed.

---

## Environment-Specific Behavior

### Development (`environment: "dev"`)
- **Retention Policy**: RETAIN (resources preserved on stack deletion)
- **CloudWatch Log Retention**: 1 week
- **Debug Logging**: Enabled
- **S3 Log Lifecycle**: Deleted after 30 days

### Production (`environment: "prod"`)
- **Retention Policy**: RETAIN (resources preserved on stack deletion)
- **CloudWatch Log Retention**: 1 month
- **Debug Logging**: Disabled
- **S3 Log Lifecycle**: Transitioned to IA after 30 days, deleted after 90 days

---

## Example Configurations

### Minimal (BDA processing only)

```json
{
  "deployment": {
    "input-bucket-name": "my-org-evidence-bucket",
    "create-input-bucket": true,
    "output-bucket-name": "my-org-evidence-output",
    "create-output-bucket": true,
    "bda-project-arn": "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/abc123",
    "environment": "dev",
    "region": "us-east-1"
  }
}
```

### Full (BDA + MCP Gateway with semantic search)

```json
{
  "deployment": {
    "input-bucket-name": "my-org-evidence-bucket",
    "create-input-bucket": true,
    "output-bucket-name": "my-org-evidence-output",
    "create-output-bucket": true,
    "bda-project-arn": "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/abc123",
    "bda-stage": "LIVE",
    "environment": "dev",
    "region": "us-east-1",
    "s3-trigger-prefix": "__sfdcroot__/"
  },
  "lambda": {
    "memory-size": 1024,
    "timeout": 300
  },
  "cors": {
    "allowed-origins": [
      "https://*.force.com"
    ]
  },
  "features": {
    "enable-bda": true,
    "write-sf-metadata": true
  },
  "logging": {
    "s3-log-retention-days": 90,
    "cloudwatch-log-retention-days": 30
  },
  "mcp": {
    "knowledge-base-id": "ABCDEFGHIJ",
    "enable-semantic-search": true
  }
}
```

---

## Enabling Semantic Search (Optional)

The `semantic_search` MCP tool allows Agentforce to perform natural language queries against your processed documents using vector similarity. This is powered by an Amazon Bedrock Knowledge Base that you create and point at your output data.

### Step 1: Create an Amazon Bedrock Knowledge Base

1. Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/) in the same region as your deployment
2. Navigate to **Knowledge bases** → **Create knowledge base**
3. Give it a name (e.g., `evidence-kb-dev`) and create or select an IAM service role
4. For the data source, choose **Amazon S3** and point it to your **output bucket** (the bucket where BDA writes processed results)
5. Choose an embeddings model (e.g., Amazon Titan Embeddings V2)
6. Select a vector store — for quick setup, let Bedrock create an OpenSearch Serverless collection for you
7. Complete the wizard and wait for the Knowledge Base to become active

### Step 2: Sync the Data Source

After creating the Knowledge Base:

1. Select your Knowledge Base in the console
2. Under **Data source**, choose **Sync** to ingest the documents from your output bucket
3. Wait for the sync to complete — you can monitor progress in the console

> You will need to re-sync whenever new documents are processed by BDA. You can automate this with an EventBridge rule or run it on a schedule.

### Step 3: Update Your CDK Configuration

Copy the Knowledge Base ID from the console (found on the Knowledge Base detail page) and update `cdk.context.json`:

```json
{
  "mcp": {
    "knowledge-base-id": "YOUR_KNOWLEDGE_BASE_ID",
    "enable-semantic-search": true
  }
}
```

### Step 4: Redeploy

```bash
cdk deploy McpGatewayStack-dev
```

After redeployment, the `semantic_search` tool will be registered with the AgentCore Gateway and available to Agentforce. Users can ask questions like *"Find documents mentioning the suspect's vehicle"* and get results ranked by semantic relevance.

---

## Validation

The stack automatically validates at deploy time:
- `input-bucket-name` is at least 3 characters
- `bda-project-arn` is a valid `arn:aws:bedrock:` ARN (when `enable-bda` is true)
- All `processed-file-types` start with a dot (e.g., `.pdf`)
- At least one file type is configured for processing

---

## Security Features

### Always Enabled
- S3 Block Public Access on all buckets
- SSL/TLS enforcement via bucket policies (deny non-HTTPS)
- SSE-S3 encryption at rest on S3 buckets
- AWS-managed encryption on DynamoDB tables
- DynamoDB Point-in-Time Recovery
- IAM least-privilege roles scoped to specific resources
- S3 access logging to dedicated logging bucket
- OAuth 2.0 (Cognito client_credentials) for MCP Gateway access

### Configurable
- CORS allowed origins
- CloudWatch log retention periods
- Lambda memory and timeout sizing

---

## Resource Tagging

All resources are automatically tagged with:

| Tag | Value |
|-----|-------|
| `Project` | `S3-Amazon-Bedrock-Integration` |
| `Environment` | Value of `deployment.environment` |
| `Stack` | CDK stack name |
| `ManagedBy` | `CDK` |

---

## BDA Cross-Region Inference

The stack automatically configures Amazon Bedrock Data Automation Cross-Region Inference Service (CRIS) based on your deployment region:

| Geography | Profile | Source Regions |
|-----------|---------|----------------|
| US | `us.data-automation-v1` | us-east-1, us-west-2 |
| EU | `eu.data-automation-v1` | eu-central-1, eu-west-1 |
| EU (London) | `eu.data-automation-v1` | eu-west-2 |
| APAC | `apac.data-automation-v1` | ap-south-1, ap-southeast-2 |
| US GovCloud | `us-gov.data-automation-v1` | us-gov-west-1 |

---

## Stack Outputs

### BdaProcessingStack

| Output | Description |
|--------|-------------|
| `S3BucketName` | Document storage bucket |
| `LoggingBucketName` | Access log bucket |
| `InvokeBDAFunctionName` / `Arn` | Lambda that triggers BDA |
| `BDAEventProcessorFunctionName` / `Arn` | Lambda that handles BDA completion (when BDA enabled) |
| `DocumentTableName` | DynamoDB document metadata table |
| `DocumentUploadPath` | S3 path for uploading documents |
| `EventBridgeRuleName` | EventBridge rule (when BDA enabled) |
| `BDAEnabled` | Whether BDA processing is enabled |

### McpGatewayStack

| Output | Description |
|--------|-------------|
| `GatewayArn` | ARN of the AgentCore MCP Gateway |
| `GatewayMcpEndpoint` | MCP endpoint URL for the gateway |
| `McpToolsFunctionArn` | ARN of the MCP tools Lambda |
| `SfdcQueryStateMachineArn` | ARN of the SFDC Query Express Step Function |
| `SfdcQueryProcessorFunctionArn` | ARN of the SFDC Query Processor Lambda |
| `CognitoUserPoolId` | Cognito User Pool ID |
| `CognitoClientId` | App Client ID for client_credentials grant |
| `CognitoTokenEndpoint` | Token endpoint URL for OAuth 2.0 |
