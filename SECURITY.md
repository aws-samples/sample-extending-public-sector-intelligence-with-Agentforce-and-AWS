# Security

## Shared Responsibility Model

This solution follows the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/):

### AWS Responsibilities
- Physical infrastructure security
- Managed service availability (Amazon Bedrock, Amazon S3, AWS Lambda, Amazon DynamoDB)
- Encryption implementation (SSE-S3, TLS)
- Network infrastructure

### Customer Responsibilities
- IAM policy configuration and least-privilege enforcement
- S3 bucket access policies and CORS configuration
- Data classification and handling procedures
- Monitoring and incident response
- Key management decisions (AWS-managed vs. customer-managed KMS keys)
- Salesforce authentication configuration (JWT certificates)
- Network access controls and VPC configuration (if applicable)

## Threat Model

### Threat Actors
1. **External attacker**: Attempts to access evidence data via public endpoints or misconfigured permissions
2. **Compromised Cognito credentials**: Attacker with a valid client secret could invoke MCP tools via the AgentCore Gateway
3. **Insider threat**: Authorized user attempting to access evidence outside their case scope

### Attack Vectors and Controls

| Vector | Control |
|--------|---------|
| Public S3 bucket access | Block Public Access enabled on all buckets |
| Data in transit interception | TLS enforced via bucket policy (deny non-HTTPS) |
| Unauthorized BDA invocation | IAM policy scoped to specific BDA project ARN |
| Cross-case evidence access | Salesforce metadata filtering on queries |
| Lambda code injection | Input validation on all parameters |
| Log data exposure | CloudWatch log retention policies, scoped log group access |
| Credential theft | OAuth 2.0 (Cognito client_credentials), short-lived tokens |

### Data Classification

| Data Type | Classification | Handling |
|-----------|---------------|----------|
| Raw evidence files (video, images, documents, audio) | Sensitive/Restricted | Encrypted at rest (SSE-S3), access via IAM only, no public access |
| BDA processed output (summaries, transcriptions, OCR) | Sensitive | Encrypted at rest, scoped access via Step Functions |
| DynamoDB metadata (document IDs, Salesforce IDs, status) | Internal | Encrypted at rest (AWS-managed), point-in-time recovery enabled |
| Access logs | Internal | Lifecycle policies, separate logging bucket |
| Cognito tokens | Confidential | Short-lived, scoped OAuth scopes |

## Key Management Strategy

This solution uses **AWS-managed keys** (SSE-S3) for S3 encryption and **AWS-managed encryption** for DynamoDB.

**Rationale:**
- AWS-managed keys provide encryption at rest with zero management overhead
- Key rotation is handled automatically by AWS

**When to use customer-managed keys (KMS-CMK):**
- FedRAMP High or DoD IL4+ compliance requirements
- Need for cross-account key sharing
- Regulatory requirement for explicit key rotation control
- Need for key usage audit via CloudTrail

To enable customer-managed KMS keys, modify the `encryption` parameter on the S3 bucket and DynamoDB table definitions in `bda_processing_stack.py`.

## Security Controls by AWS Service

### Amazon S3
- Block Public Access: enabled on all buckets
- Server-side encryption: SSE-S3 (default) or SSE-KMS (configurable)
- Bucket policy: denies all non-HTTPS access
- Versioning: enabled on input and output buckets (not on the logging bucket)
- Access logging: enabled to dedicated logging bucket
- CORS: restricted to `*.force.com` origins

### AWS Lambda
- Execution role: least-privilege IAM permissions
- No internet access by default (runs in AWS-managed VPC)
- Input validation on all user-provided parameters
- Timeout and memory limits configured
- Log retention policies enforced

### Amazon DynamoDB
- Encryption at rest: AWS-managed keys
- Point-in-time recovery: enabled
- Access: IAM-only (no public endpoints)
- GSI access scoped to Lambda execution role

### Amazon EventBridge
- Rules scoped to specific event patterns (source: aws.bedrock)
- Targets limited to specific Lambda functions

### Amazon Bedrock AgentCore Gateway
- Authentication: Cognito OAuth 2.0 (client_credentials flow)
- Scoped OAuth scopes (tools.invoke only)
- IAM authorization for internal Lambda targets

### AWS Step Functions (Express)
- IAM role scoped to specific DynamoDB table and Lambda functions
- CloudWatch logging with scoped write permissions
- Express type: no persistent execution history (reduced data exposure)

### Amazon Cognito
- Client credentials flow only (no user passwords stored)
- Generated client secret
- Scoped OAuth resource server

## Security Implementation Checklist

### Pre-Deployment
- [ ] Review IAM policies in `bda_processing_stack.py` and `mcp_gateway_stack.py`
- [ ] Configure `cdk.context.json` with production-appropriate settings
- [ ] Verify Bedrock Data Automation project region matches deployment region
- [ ] Review CORS allowed origins for your Salesforce domain

### Post-Deployment
- [ ] Verify S3 Block Public Access is active (check in S3 console)
- [ ] Verify encryption is enabled on all buckets
- [ ] Test Lambda functions with invalid input to confirm validation works
- [ ] Review CloudWatch log groups for appropriate retention
- [ ] Confirm Cognito client secret is stored securely in Salesforce

### Ongoing
- [ ] Monitor CloudWatch Logs for errors or unexpected access patterns
- [ ] Review IAM Access Analyzer findings periodically
- [ ] Rotate Cognito client secrets on a regular schedule
- [ ] Update CDK dependencies for security patches

## Compliance Notes

- This solution is deployable to AWS GovCloud for FedRAMP High requirements
- All data remains within the configured AWS region
- No data is sent to third-party services
- Amazon Bedrock Data Automation processes data within the AWS region boundary
- MCP (Model Context Protocol) is an open standard; no third-party MCP servers are used
