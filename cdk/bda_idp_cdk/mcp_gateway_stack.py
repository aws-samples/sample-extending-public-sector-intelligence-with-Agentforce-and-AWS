"""CDK Stack: AgentCore Gateway with Lambda MCP Target for semantic search tools."""

import os
import json
import aws_cdk as cdk
from constructs import Construct
import aws_cdk.aws_bedrockagentcore as agentcore
import aws_cdk.aws_cognito as cognito
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_logs as logs
import aws_cdk.aws_stepfunctions as sfn


class McpGatewayStack(cdk.Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        knowledge_base_id: str = "",
        enable_semantic_search: bool = False,
        dynamodb_table_name: str = "<DYNAMODB_TABLE_NAME>",
        output_bucket_name: str = "",
        environment: str = "dev",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Cognito User Pool for OAuth 2.0 Client Credentials ──
        user_pool = cognito.UserPool(self, "McpUserPool",
            user_pool_name=f"sfdc-mcp-pool-{environment}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            sign_in_aliases=cognito.SignInAliases(email=True),
        )

        # Resource server defines the OAuth scopes
        resource_server = user_pool.add_resource_server("McpResourceServer",
            identifier=f"mcp-gateway-{environment}",
            user_pool_resource_server_name=f"MCP Gateway - {environment}",
            scopes=[
                cognito.ResourceServerScope(
                    scope_name="tools.invoke",
                    scope_description="Invoke MCP tools"
                )
            ]
        )

        # App client using client_credentials grant
        user_pool_client = user_pool.add_client("McpAppClient",
            user_pool_client_name=f"sfdc-mcp-client-{environment}",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(resource_server,
                        cognito.ResourceServerScope(
                            scope_name="tools.invoke",
                            scope_description="Invoke MCP tools"
                        )
                    )
                ]
            ),
        )

        # Domain for the token endpoint
        user_pool_domain = user_pool.add_domain("McpDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"sfdc-mcp-{environment}-{cdk.Aws.ACCOUNT_ID}"
            )
        )

        # ── Lambda for MCP tools ──
        mcp_tools_log_group = logs.LogGroup(self, "McpToolsLogGroup",
            log_group_name=f"/aws/lambda/McpTools-{environment}",
            retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY
        )

        mcp_tools_role = iam.Role(self, "McpToolsRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ],
            description="Execution role for MCP tools Lambda"
        )

        # KB retrieve permission (only when semantic search is enabled)
        if enable_semantic_search and knowledge_base_id:
            mcp_tools_role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{knowledge_base_id}"
                ]
            ))

        # S3 Vectors query permission - removed

        mcp_tools_function = lambda_.Function(self, "McpToolsFunction",
            function_name=f"McpTools-{environment}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/mcp-tools")),
            role=mcp_tools_role,
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
            },
            log_group=mcp_tools_log_group
        )

        # ── AgentCore Gateway (OAuth 2.0 via Cognito) ──
        gateway = agentcore.Gateway(self, "McpGateway",
            gateway_name=f"sfdc-mcp-gateway-{environment}",
            description=f"MCP Gateway for semantic search tools ({environment})",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(
                user_pool=user_pool,
                allowed_clients=[user_pool_client],
                allowed_scopes=[f"mcp-gateway-{environment}/tools.invoke"],
            ),
        )

        # ── Lambda Target with inline tool schema ──
        # Build tool list — semantic_search is only available when a Knowledge Base is configured
        tool_definitions = []

        if enable_semantic_search and knowledge_base_id:
            tool_definitions.append(
                agentcore.ToolDefinition(
                    name="semantic_search",
                    description="Search documents using Bedrock Knowledge Base semantic similarity. Can filter by Salesforce object ID or type.",
                    input_schema=agentcore.SchemaDefinition(
                        type=agentcore.SchemaDefinitionType.OBJECT,
                        properties={
                            "query": agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.STRING,
                                description="Natural language search query"
                            ),
                            "max_results": agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.INTEGER,
                                description="Number of results to return (1-25, default 5)"
                            ),
                            "salesforce_object_id": agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.STRING,
                                description="Filter results to a specific Salesforce record ID"
                            ),
                            "salesforce_object_type": agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.STRING,
                                description="Filter results to a specific Salesforce object type (e.g. Case, Account)"
                            )
                        },
                        required=["query"]
                    )
                )
            )

        tool_definitions.append(
            agentcore.ToolDefinition(
                name="get_document_summaries",
                description="Retrieve BDA-generated summaries for all documents associated with a Salesforce record. Optionally filter to a single document by filename. Returns extracted summaries from images, documents, videos, and audio files.",
                input_schema=agentcore.SchemaDefinition(
                    type=agentcore.SchemaDefinitionType.OBJECT,
                    properties={
                        "salesforce_object_id": agentcore.SchemaDefinition(
                            type=agentcore.SchemaDefinitionType.STRING,
                            description="The Salesforce record ID to retrieve document summaries for (e.g. Case ID, Account ID)"
                        ),
                        "filename": agentcore.SchemaDefinition(
                            type=agentcore.SchemaDefinitionType.STRING,
                            description="Optional filename to filter to a single document's summary"
                        )
                    },
                    required=["salesforce_object_id"]
                )
            )
        )

        lambda_target = gateway.add_lambda_target("McpToolsTarget",
            gateway_target_name=f"mcp-tools-{environment}",
            description=f"Lambda target for MCP tools ({environment})",
            lambda_function=mcp_tools_function,
            tool_schema=agentcore.ToolSchema.from_inline(tool_definitions)
        )

        # ── SFDC Query Processor Lambda (used by Step Function) ──
        sfdc_query_role = iam.Role(self, "SfdcQueryLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ],
            description="Execution role for SFDC Query Processor Lambda"
        )

        # S3 read-only access to the output bucket
        if output_bucket_name:
            sfdc_query_role.add_to_policy(iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    f"arn:aws:s3:::{output_bucket_name}",
                    f"arn:aws:s3:::{output_bucket_name}/*"
                ]
            ))

        sfdc_query_log_group_lambda = logs.LogGroup(self, "SfdcQueryLambdaLogGroup",
            log_group_name=f"/aws/lambda/SfdcQueryProcessor-{environment}",
            retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY
        )

        sfdc_query_function = lambda_.Function(self, "SfdcQueryProcessorFunction",
            function_name=f"SfdcQueryProcessor-{environment}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/sfdc-query-processor")),
            role=sfdc_query_role,
            timeout=cdk.Duration.seconds(60),
            memory_size=256,
            environment={
                "OUTPUT_BUCKET": output_bucket_name,
            },
            log_group=sfdc_query_log_group_lambda
        )

        # Single-document processor Lambda (filters by filename)
        sfdc_doc_log_group = logs.LogGroup(self, "SfdcDocProcessorLogGroup",
            log_group_name=f"/aws/lambda/SfdcDocProcessor-{environment}",
            retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY
        )

        sfdc_doc_function = lambda_.Function(self, "SfdcDocProcessorFunction",
            function_name=f"SfdcDocProcessor-{environment}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/sfdc-document-processor")),
            role=sfdc_query_role,
            timeout=cdk.Duration.seconds(60),
            memory_size=256,
            log_group=sfdc_doc_log_group
        )

        # Use the deployed Lambda ARN for the Step Function
        resolved_lambda_arn = sfdc_query_function.function_arn

        # ── Express Step Function: Query DynamoDB by salesforce_object_id ──
        sfdc_query_sfn_role = iam.Role(self, "SfdcQuerySfnRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Execution role for SFDC query Express Step Function"
        )

        # DynamoDB Query permission (GSI query)
        sfdc_query_sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:Query"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{dynamodb_table_name}",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{dynamodb_table_name}/index/*"
            ]
        ))

        # Lambda invoke permission (use the deployed functions)
        sfdc_query_sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                sfdc_query_function.function_arn,
                sfdc_doc_function.function_arn
            ]
        ))

        # CloudWatch Logs permission for Express Step Function
        sfdc_query_log_group = logs.LogGroup(self, "SfdcQuerySfnLogGroup",
            log_group_name=f"/aws/stepfunctions/SfdcQuery-{environment}",
            retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY
        )

        sfdc_query_sfn_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogDelivery",
                "logs:GetLogDelivery",
                "logs:UpdateLogDelivery",
                "logs:DeleteLogDelivery",
                "logs:ListLogDeliveries",
                "logs:PutResourcePolicy",
                "logs:DescribeResourcePolicies",
                "logs:DescribeLogGroups",
            ],
            resources=["*"]
        ))

        sfdc_query_sfn_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:PutLogEvents",
                "logs:CreateLogStream",
            ],
            resources=[
                sfdc_query_log_group.log_group_arn,
                f"{sfdc_query_log_group.log_group_arn}:*"
            ]
        ))

        # Step Function ASL definition
        sfdc_query_definition = {
            "Comment": "Query DynamoDB by salesforce_object_id and invoke Lambda with results",
            "StartAt": "QueryDynamoDB",
            "States": {
                "QueryDynamoDB": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::aws-sdk:dynamodb:query",
                    "Parameters": {
                        "TableName": dynamodb_table_name,
                        "IndexName": "salesforce-object-id-index",
                        "KeyConditionExpression": "salesforce_object_id = :sfdc_id",
                        "ExpressionAttributeValues": {
                            ":sfdc_id": {
                                "S.$": "$.salesforce_object_id"
                            }
                        }
                    },
                    "ResultPath": "$.queryResult",
                    "Next": "CheckResults"
                },
                "CheckResults": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.queryResult.Count",
                            "NumericEquals": 0,
                            "Next": "NoResults"
                        }
                    ],
                    "Default": "CheckFilename"
                },
                "NoResults": {
                    "Type": "Pass",
                    "Result": {
                        "message": "No documents found for the specified Salesforce Object Id",
                        "documents_processed": 0,
                        "documents": []
                    },
                    "ResultPath": "$.noResultsOutput",
                    "OutputPath": "$.noResultsOutput",
                    "End": True
                },
                "CheckFilename": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.filename",
                            "IsPresent": True,
                            "Next": "InvokeDocumentProcessor"
                        }
                    ],
                    "Default": "InvokeSummaryProcessor"
                },
                "InvokeSummaryProcessor": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": sfdc_query_function.function_arn,
                        "Payload": {
                            "salesforce_object_id.$": "$.salesforce_object_id",
                            "items.$": "$.queryResult.Items"
                        }
                    },
                    "OutputPath": "$.Payload.body",
                    "End": True
                },
                "InvokeDocumentProcessor": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": sfdc_doc_function.function_arn,
                        "Payload": {
                            "salesforce_object_id.$": "$.salesforce_object_id",
                            "filename.$": "$.filename",
                            "items.$": "$.queryResult.Items"
                        }
                    },
                    "OutputPath": "$.Payload.body",
                    "End": True
                }
            }
        }

        sfdc_query_sfn = sfn.CfnStateMachine(self, "SfdcQueryStateMachine",
            state_machine_name=f"SfdcQuery-{environment}",
            state_machine_type="EXPRESS",
            definition=sfdc_query_definition,
            role_arn=sfdc_query_sfn_role.role_arn,
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                            log_group_arn=sfdc_query_log_group.log_group_arn
                        )
                    )
                ],
                include_execution_data=True,
                level="ALL"
            )
        )
        sfdc_query_sfn.node.add_dependency(sfdc_query_sfn_role)
        sfdc_query_sfn.node.add_dependency(sfdc_query_log_group)

        # Add Step Function ARN to MCP tools Lambda and grant invoke permission
        mcp_tools_function.add_environment(
            "SFDC_QUERY_STATE_MACHINE_ARN", sfdc_query_sfn.attr_arn
        )
        mcp_tools_role.add_to_policy(iam.PolicyStatement(
            actions=["states:StartSyncExecution"],
            resources=[sfdc_query_sfn.attr_arn]
        ))

        # ── Outputs ──
        cdk.CfnOutput(self, "GatewayArn",
            description="ARN of the AgentCore MCP Gateway",
            value=gateway.gateway_arn
        )
        cdk.CfnOutput(self, "GatewayMcpEndpoint",
            description="MCP endpoint URL for the AgentCore Gateway",
            value=cdk.Fn.join("", [
                "https://",
                gateway.gateway_id,
                ".gateway.bedrock-agentcore.",
                self.region,
                ".amazonaws.com/mcp"
            ])
        )
        cdk.CfnOutput(self, "McpToolsFunctionArn",
            description="ARN of the MCP tools Lambda",
            value=mcp_tools_function.function_arn
        )
        cdk.CfnOutput(self, "SfdcQueryStateMachineArn",
            description="ARN of the SFDC Query Express Step Function",
            value=sfdc_query_sfn.attr_arn
        )
        cdk.CfnOutput(self, "SfdcQueryProcessorFunctionArn",
            description="ARN of the SFDC Query Processor Lambda",
            value=sfdc_query_function.function_arn
        )
        cdk.CfnOutput(self, "CognitoUserPoolId",
            description="Cognito User Pool ID",
            value=user_pool.user_pool_id
        )
        cdk.CfnOutput(self, "CognitoClientId",
            description="Cognito App Client ID (use with client_credentials grant)",
            value=user_pool_client.user_pool_client_id
        )
        cdk.CfnOutput(self, "CognitoTokenEndpoint",
            description="Token endpoint for client_credentials flow",
            value=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token"
        )
