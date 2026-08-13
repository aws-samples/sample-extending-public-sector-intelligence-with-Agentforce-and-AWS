# Extending Public Sector Intelligence with Agentforce and AWS

> **Sample code** — This repository accompanies the AWS blog post *Extending Public Sector Intelligence with Agentforce and AWS*. It is intended as a reference implementation demonstrating how to extend [Salesforce Agentforce](https://www.salesforce.com/agentforce/) with AWS services through an [Amazon Bedrock AgentCore Gateway](https://aws.amazon.com/bedrock/agentcore/) using the Model Context Protocol (MCP). This is **not** a production-ready workload. Review, harden, and test any adaptation before deploying to production environments.

---

## What This Sample Shows

Public sector agencies process large volumes of unstructured evidence — body camera footage, surveillance video, scanned documents, photographs, and audio recordings — that require manual review before anyone can act on them. This sample demonstrates how to:

1. **Process unstructured data with AI** — Automatically extract insights from documents, images, video, and audio using [Amazon Bedrock Data Automation](https://aws.amazon.com/bedrock/data-automation/).
2. **Expose those insights to Agentforce via MCP** — Surface document summaries and semantic search capabilities to Salesforce Agentforce agents through an Amazon Bedrock AgentCore Gateway MCP endpoint.

With this pattern, Agentforce users can ask natural language questions about case evidence and receive AI-powered answers without leaving the Salesforce UI or managing AWS resources directly.

### How It Builds on Previous Work

This project extends the pattern established in [Modernizing evidence management in Salesforce Public Sector Solutions with Amazon S3](https://aws.amazon.com/blogs/publicsector/modernizing-evidence-management-in-salesforce-public-sector-solutions-with-amazon-s3/). That post set up durable, cost-efficient storage for evidence files using the External Storage of Files with Amazon S3 connector. This sample adds the intelligence layer — processing those stored files and making the results available through MCP.

---

## Architecture

![Solution Architecture](assets/Solution_ArchitectureDetail.png)

### End-to-End Flow

| Step | What Happens |
|------|-------------|
| 1 | Documents uploaded to Amazon S3 (via the Agentforce Public Sector connector) trigger processing |
| 2 | Amazon Bedrock Data Automation processes the file based on media type — extracting text, generating summaries, transcribing audio/video |
| 3 | Amazon DynamoDB assigns a document ID and stores metadata alongside BDA-generated insights |
| 4 | Processed results are saved to a dedicated output bucket in Amazon S3 |
| 5 | A Salesforce user's chat in Agentforce triggers an action that calls AWS through MCP via the Amazon Bedrock AgentCore Gateway |
| 6 | The call authenticates through Amazon Cognito OAuth 2.0 (client_credentials flow), and the gateway invokes an MCP server on AWS Lambda |
| 7 | The Lambda function queries DynamoDB to locate document records, then retrieves results from S3 |
| 8 | Results are returned to Agentforce and loaded into the agent's context for a natural language response |

---

## Stacks Deployed

The CDK application deploys two CloudFormation stacks:

### BdaProcessingStack

Handles document ingestion and AI processing.

| Resource | Purpose |
|----------|---------|
| S3 Buckets (2) | Document storage + access logging |
| Lambda Functions | `InvokeBDAProject` (triggers BDA), `BDAEventProcessor` (handles completion events) |
| DynamoDB Tables (2) | Document metadata (with GSIs for job_id and salesforce_object_id) + counters |
| EventBridge Rule | Fires on BDA job completion |
| IAM Roles | Least-privilege execution roles |

### McpGatewayStack

Exposes AI-powered tools to Agentforce through an Amazon Bedrock AgentCore Gateway.

| Resource | Purpose |
|----------|---------|
| Amazon Bedrock AgentCore Gateway | OAuth-secured MCP endpoint for tool invocation |
| Lambda Function (`McpTools`) | Implements `get_document_summaries` and optionally `semantic_search` |
| Step Functions (Express) | Orchestrates DynamoDB query + S3 retrieval |
| Cognito User Pool + App Client | OAuth 2.0 client_credentials authentication |
| IAM Roles | Scoped permissions for Bedrock KB retrieve, S3 read, DynamoDB query |

---

## Prerequisites

Before you begin:

1. **Complete the previous post** — [Modernizing evidence management in Salesforce Public Sector Solutions with Amazon S3](https://aws.amazon.com/blogs/publicsector/modernizing-evidence-management-in-salesforce-public-sector-solutions-with-amazon-s3/). This sample builds directly on that foundation.
2. **Verify Agentforce MCP support** — In Salesforce Setup, navigate to **Setup > Agentforce Registry** and confirm the option to register an MCP server is available.

Additionally, ensure you have:

- **AWS CLI** installed and configured (`aws sts get-caller-identity` should succeed)
- **AWS CDK v2** installed globally (`npm install -g aws-cdk`)
- **Node.js 18+** (required by CDK CLI)
- **Python 3.9+** with `pip`
- **An Amazon Bedrock Data Automation project** created in the AWS Console
- Your **AWS Account ID** and target **region**

---

## Deploy the AWS CDK Stack

### Step 1: Clone and Navigate

```bash
git clone https://github.com/aws-samples/sample-extending-public-sector-intelligence-with-Agentforce-and-AWS.git
cd sample-extending-public-sector-intelligence-with-Agentforce-and-AWS/cdk
```

### Step 2: Create a Python Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Create Your Configuration File

```bash
cp cdk.context.example.json cdk.context.json
```

Open `cdk.context.json` and fill in the **required** values:

```json
{
  "deployment": {
    "input-bucket-name": "my-org-evidence-input",
    "create-input-bucket": true,
    "output-bucket-name": "my-org-evidence-output",
    "create-output-bucket": true,
    "bda-project-arn": "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/YOUR-PROJECT-ID",
    "environment": "dev",
    "region": "us-east-1"
  }
}
```

| Field | Where to Find It |
|-------|-----------------|
| `input-bucket-name` | Choose any globally unique S3 bucket name |
| `output-bucket-name` | Choose any globally unique S3 bucket name (can differ from input) |
| `bda-project-arn` | AWS Console → Amazon Bedrock → Data Automation → Your Project → ARN |
| `region` | The region where your BDA project lives |

> **Bucket creation notes:** `create-input-bucket` and `create-output-bucket` default to `true`, meaning CDK will create new buckets with all required policies, encryption, and CORS configured automatically. The bucket names you provide must be globally unique across all AWS accounts. If you set either to `false`, you are referencing an existing bucket — CDK will not modify its policies or CORS. You will need to manually configure CORS permissions and ensure the Lambda roles have the required S3 access. See [cdk/deployment-config.md](cdk/deployment-config.md) for details.

For the full configuration reference (optional fields, feature flags, MCP settings), see [cdk/README.md](cdk/README.md).

### Step 5: Bootstrap CDK (First Time Only)

```bash
cdk bootstrap aws://YOUR-ACCOUNT-ID/YOUR-REGION
```

### Step 6: Preview the Deployment

```bash
cdk synth   # Synthesize CloudFormation templates
cdk diff    # Show what will be created/changed
```

### Step 7: Deploy

```bash
cdk deploy --all
```

To deploy stacks individually:

```bash
cdk deploy BdaProcessingStack-dev
cdk deploy McpGatewayStack-dev
```

> Replace `dev` with your configured `environment` value.

### Step 8: Retrieve Stack Outputs

After deployment, retrieve the CloudFormation outputs you'll need for the Salesforce configuration:

```
McpGatewayStack-dev.GatewayMcpEndpoint = https://xxxx.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
McpGatewayStack-dev.CognitoTokenEndpoint = https://sfdc-mcp-dev-xxxx.auth.us-east-1.amazoncognito.com/oauth2/token
McpGatewayStack-dev.CognitoClientId = xxxxxxxxxxxxxxxxx
```

You also need the **Cognito client secret**:
1. Open **Amazon Cognito** in the AWS Console
2. Navigate to **User Pools** → select the pool created by the stack
3. Choose the **App client** → copy the **Client secret**

---

## Connect Agentforce to the MCP Endpoint

With the AWS stack deployed and credentials in hand, register the MCP server in Salesforce.

### Step 9: Create MCP Connection in Salesforce

1. In Salesforce Setup, search for **Agentforce Registry** in Quick Find
2. Choose **New** → **Register MCP Server**
3. Enter the values from Step 8:
   - **MCP Endpoint URL** → `GatewayMcpEndpoint`
   - **Token Endpoint** → `CognitoTokenEndpoint`
   - **Client ID** → `CognitoClientId`
   - **Client Secret** → the secret retrieved from the Cognito console
4. Choose **Create and Continue**
5. When prompted for the **MCP Server Allowlist**, choose **Select All**
6. Choose **Save**

### Step 10: Configure an Agentforce Subagent

Create a subagent dedicated to evidence retrieval:

1. In **Agentforce Builder**, create a new agent or select an existing one
2. Create a **New Subagent** with:
   - **Name**: `Media Processor`
   - **Description**: `Subagent that handles all questions related to files, documents, photos, images, videos, or audio attached to the current case. Retrieves AI-generated insights from processed media and responds in natural language.`
3. Under **Actions Available For Reasoning**, select the MCP connection created in Step 9
4. In **Reasoning Instructions**, add:
   > Handle all questions about files, documents, photos, images, videos, or audio attached to the current case. Run @[Your MCP Action] to retrieve processed insights. If no insights are available, inform the user the attachment has not yet been processed. Do not fabricate content about unprocessed files.
5. Replace `@[Your MCP Action]` by typing `@` and selecting the MCP resource associated with this subagent
6. Choose **Save**

### Step 11: Test and Validate

1. In Agentforce Builder, open the **Preview** panel
2. Set **Context Variables** → assign `currentRecordId` to a case ID with processed evidence
3. Choose **Apply and Restart Session**
4. Enter a question such as *"Summarize the files for this case"*
5. Verify the agent returns summaries of evidence associated with the case

---

## Extend This Pattern

This architecture is not limited to evidence management. The same modular pattern applies to any workflow involving unstructured data:

- **Permits and compliance reviews** — Extract fields and summaries from submitted documents
- **Benefits claims and tax forms** — Classify documents and validate extracted data
- **Loan applications** — Process supporting documentation and surface key details to case workers

You can also integrate the open source [GenAI IDP Accelerator](https://github.com/aws-samples/genai-idp-accelerator) into the processing pipeline for intelligent document processing with classification, structured extraction, validation, and human-in-the-loop review.

The MCP query path remains the same regardless of processing approach — AgentCore Gateway exposes your processed data as tools that any MCP-compatible agent can discover and invoke.

---

## Configuration Reference

| Section | Purpose |
|---------|---------|
| `deployment` | Bucket names, BDA project ARN, region, environment |
| `lambda` | Memory and timeout sizing |
| `cors` | Allowed origins for S3 CORS |
| `features` | Feature flags (`enable-bda`, `write-sf-metadata`) |
| `logging` | Log retention periods |
| `mcp` | Knowledge Base ID, semantic search toggle |

See [cdk/README.md](cdk/README.md) for the complete reference.

---

## Clean Up

### Destroy the CDK Stacks

When you no longer need this sample, use `cdk destroy` to tear down the deployed infrastructure. This deletes the CloudFormation stacks and all resources managed by them (Lambda functions, IAM roles, EventBridge rules, Step Functions, etc.). Stateful resources with `RETAIN` removal policies (S3 buckets, DynamoDB tables, Cognito user pools) are preserved and must be removed separately — see [Retained Resources](#retained-resources) below.

```bash
cd cdk
cdk destroy BdaProcessingStack-dev McpGatewayStack-dev
```

Alternatively, you can delete the stacks directly from the AWS Console:

1. Open the [CloudFormation console](https://console.aws.amazon.com/cloudformation/)
2. Select the `McpGatewayStack-dev` stack and choose **Delete**
3. Wait for deletion to complete, then repeat for `BdaProcessingStack-dev`

> Delete `McpGatewayStack-dev` first since it depends on resources in `BdaProcessingStack-dev`.

### Retained Resources

The stacks use `RETAIN` removal policies on stateful resources. After `cdk destroy`, manually delete these via the AWS Console if no longer needed:

**S3 Buckets:**
1. Open the [Amazon S3 console](https://console.aws.amazon.com/s3/)
2. Select each of the following buckets: your input bucket, output bucket, and the access logging bucket (named `YOUR-INPUT-BUCKET-dev-logs`)
3. Choose **Empty** to remove all objects, then choose **Delete** to remove the bucket itself

**DynamoDB Tables:**
1. Open the [DynamoDB console](https://console.aws.amazon.com/dynamodb/)
2. Navigate to **Tables** and delete: `YOUR-INPUT-BUCKET-dev-documents` and `YOUR-INPUT-BUCKET-dev-counters`

**Cognito User Pool:**
1. Open the [Amazon Cognito console](https://console.aws.amazon.com/cognito/)
2. Navigate to **User Pools**, select the pool created by the stack, and choose **Delete**

**CloudWatch Log Groups:**
1. Open the [CloudWatch console](https://console.aws.amazon.com/cloudwatch/)
2. Navigate to **Logs > Log groups** and delete the following:
   - `/aws/lambda/InvokeBDAProject-dev`
   - `/aws/lambda/BDAEventProcessor-dev`
   - `/aws/lambda/McpTools-dev`
   - `/aws/lambda/SfdcQueryProcessor-dev`
   - `/aws/lambda/SfdcDocProcessor-dev`
   - `/aws/stepfunctions/SfdcQuery-dev`

**Salesforce:** Remove the Agentforce MCP connection from **Setup > Agentforce Registry**.

> Because this is an event-driven, serverless architecture, you only pay for what you use. Processing costs are incurred only when evidence is actively uploaded and analyzed.

---

## Cost Considerations

This sample uses a fully serverless architecture, so there are no always-on instances or fixed hourly charges. Costs are driven almost entirely by usage — you pay only when evidence is uploaded, processed, or queried. When the system is idle, ongoing costs are limited to S3 storage for any retained objects.

| Service | Cost Driver |
|---------|------------|
| AWS Lambda | Invocations and duration |
| Amazon S3 | Storage and requests |
| Amazon Bedrock Data Automation | Per-page/per-minute processing |
| Amazon DynamoDB | Read/write capacity (on-demand) |
| Amazon Cognito | Monthly active users (minimal for machine-to-machine) |

Refer to the pricing pages for each service for current rates.

---

## Security

For comprehensive security documentation, see [SECURITY.md](SECURITY.md).

Key controls enabled by default:
- S3 Block Public Access on all buckets
- SSE-S3 encryption at rest
- TLS enforcement via bucket policies
- OAuth 2.0 (Cognito client_credentials) for MCP Gateway access
- IAM least-privilege roles scoped to specific resources
- S3 access logging to a dedicated bucket

---

## Contributors

- Christian Ramirez
- Bridget Concannon
- Varun Ghatge
- Kishore Dhamodaran

---

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
