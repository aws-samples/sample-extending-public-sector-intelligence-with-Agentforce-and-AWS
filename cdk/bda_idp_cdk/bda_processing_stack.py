from aws_cdk import (
    Duration,
    Stack,
    RemovalPolicy,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_s3_notifications as s3n,
    aws_dynamodb as dynamodb,
    aws_logs as logs,
)
from constructs import Construct
from cdk_nag import NagSuppressions
import os

class BdaProcessingStack(Stack):
    
    def __init__(self, scope: Construct, construct_id: str, 
                 bucket_name: str,
                 bda_project_arn: str,
                 bda_stage: str = "LIVE",
                 environment: str = "dev",
                 create_bucket: bool = True,
                 lambda_memory_size: int = 1024,
                 lambda_timeout: int = 300,
                 cors_allowed_origins: list = None,
                 processed_file_types: list = None,
                 s3_trigger_prefix: str = "__sfdcroot__/",
                 output_bucket_name: str = "",
                 create_output_bucket: bool = False,
                 write_sf_metadata: bool = False,
                 enable_bda: bool = True,
                 dynamodb_table_name: str = "",
                 dynamodb_counter_table_name: str = "",
                 removal_policy: str = "retain",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.aws_account = self.account
        self.aws_region = self.region
        
        # Removal policy for stateful resources (S3 buckets, DynamoDB tables).
        # Controlled via deployment.removal-policy context; defaults to RETAIN so
        # data is preserved on stack deletion. Set to "destroy" only for
        # disposable environments where data loss on teardown is acceptable.
        if str(removal_policy).strip().lower() == "destroy":
            retention_policy = RemovalPolicy.DESTROY
        else:
            retention_policy = RemovalPolicy.RETAIN

        # When destroying, S3 buckets must be emptied first or deletion fails on
        # non-empty buckets; auto_delete_objects wires a custom resource to do so.
        # Only enabled with DESTROY (it requires removal_policy=DESTROY).
        auto_delete_objects = retention_policy == RemovalPolicy.DESTROY
        
        # Common resource tags for cost tracking and management
        common_tags = {
            "Project": "S3-Amazon-Bedrock-Integration",
            "Environment": environment,
            "Stack": construct_id,
            "ManagedBy": "CDK"
        }
        
        # Use passed parameters
        # BDA Cross-Region Inference Service (CRIS) region mappings
        # See: https://docs.aws.amazon.com/bedrock/latest/userguide/bda-cris.html
        BDA_CRIS_CONFIG = {
            "us": {
                "profile": "us.data-automation-v1",
                "regions": ["us-east-1", "us-east-2", "us-west-1", "us-west-2"],
                "source_regions": ["us-east-1", "us-west-2"],
            },
            "eu": {
                "profile": "eu.data-automation-v1",
                "regions": ["eu-central-1", "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-3"],
                "source_regions": ["eu-central-1", "eu-west-1"],
            },
            "eu-west-2": {
                "profile": "eu.data-automation-v1",
                "regions": ["eu-west-2"],
                "source_regions": ["eu-west-2"],
            },
            "apac": {
                "profile": "apac.data-automation-v1",
                "regions": ["ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ap-southeast-4"],
                "source_regions": ["ap-south-1", "ap-southeast-2"],
            },
            "us-gov": {
                "profile": "us-gov.data-automation-v1",
                "regions": ["us-gov-west-1"],
                "source_regions": ["us-gov-west-1"],
            },
        }

        # Determine which CRIS geography this deployment region belongs to
        def _get_cris_config(region):
            # Check special single-region cases first
            if region == "eu-west-2":
                return BDA_CRIS_CONFIG["eu-west-2"]
            if region.startswith("us-gov"):
                return BDA_CRIS_CONFIG["us-gov"]
            for geo, config in BDA_CRIS_CONFIG.items():
                if region in config["regions"]:
                    return config
            # Fallback: assume US
            return BDA_CRIS_CONFIG["us"]

        cris_config = _get_cris_config(self.aws_region)
        bda_profile_name = cris_config["profile"]
        bda_cris_regions = cris_config["regions"]

        # Use the stack's partition for ARN construction (aws, aws-us-gov, aws-cn, etc.)
        aws_partition = self.partition

        default_bda_profile = f"arn:{aws_partition}:bedrock:{self.aws_region}:{self.aws_account}:data-automation-profile/{bda_profile_name}"
        
        # Default CORS origins if none provided
        if cors_allowed_origins is None:
            cors_allowed_origins = ["https://*.force.com"]
            
        # Default file types if none provided
        if processed_file_types is None:
            processed_file_types = [".pdf"]
            
        # Validate configuration parameters
        self._validate_configuration(bucket_name, bda_project_arn, processed_file_types, enable_bda)

        # Create dedicated logging bucket for S3 access logs and other logs
        # Configure lifecycle rules based on environment
        if environment == "prod":
            lifecycle_rules = [
                s3.LifecycleRule(
                    id="TransitionAndDeleteOldLogs",
                    enabled=True,
                    expiration=Duration.days(90),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(30)
                        )
                    ]
                )
            ]
        else:
            # Dev: Delete after 30 days, no transition (expiration must be > transition)
            lifecycle_rules = [
                s3.LifecycleRule(
                    id="DeleteOldLogs",
                    enabled=True,
                    expiration=Duration.days(30)
                )
            ]
        
        logging_bucket = s3.Bucket(self, "LoggingBucket",
            bucket_name=f"{bucket_name}-{environment}-logs",
            versioned=False,
            removal_policy=retention_policy,
            auto_delete_objects=auto_delete_objects,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=lifecycle_rules,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )
        
        # Apply tags to logging bucket
        for key, value in common_tags.items():
            logging_bucket.node.add_metadata(key, value)

        # Create or import S3 bucket based on parameter
        if create_bucket:
            document_bucket = s3.Bucket(self, "DocumentBucket",
                bucket_name=bucket_name,
                versioned=True,
                removal_policy=retention_policy,
                auto_delete_objects=auto_delete_objects,
                enforce_ssl=True,
                encryption=s3.BucketEncryption.S3_MANAGED,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                server_access_logs_bucket=logging_bucket,
                server_access_logs_prefix="s3-access-logs/",
                cors=[
                    s3.CorsRule(
                        allowed_origins=cors_allowed_origins,
                        allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.DELETE],
                        allowed_headers=["*"],
                        exposed_headers=["ETag"],
                        max_age=3000
                    )
                ]
            )
            
            # Apply tags to the bucket
            for key, value in common_tags.items():
                document_bucket.node.add_metadata(key, value)
            
            # Add bucket policy to enforce SSL (s3:* in DENY is intentional -
            # denies all actions over non-HTTPS per AWS security best practices)
            document_bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="DenyInsecureConnections",
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=["s3:*"],
                    resources=[
                        document_bucket.bucket_arn,
                        f"{document_bucket.bucket_arn}/*"
                    ],
                    conditions={
                        "Bool": {
                            "aws:SecureTransport": "false"
                        }
                    }
                )
            )
        else:
            document_bucket = s3.Bucket.from_bucket_name(self, "DocumentBucket", bucket_name)
        
        # Store bucket references for use by other stacks
        self.document_bucket = document_bucket
        self.logging_bucket = logging_bucket

        # Create or import the output bucket (if different from input bucket)
        if output_bucket_name and output_bucket_name != bucket_name:
            if create_output_bucket:
                output_bucket = s3.Bucket(self, "OutputBucket",
                    bucket_name=output_bucket_name,
                    versioned=True,
                    removal_policy=retention_policy,
                    auto_delete_objects=auto_delete_objects,
                    enforce_ssl=True,
                    encryption=s3.BucketEncryption.S3_MANAGED,
                    server_access_logs_bucket=logging_bucket,
                    server_access_logs_prefix="s3-output-access-logs/",
                    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                )
                for key, value in common_tags.items():
                    output_bucket.node.add_metadata(key, value)
            else:
                output_bucket = s3.Bucket.from_bucket_name(self, "OutputBucket", output_bucket_name)
            self.output_bucket = output_bucket
        else:
            self.output_bucket = document_bucket

        # Create DynamoDB table for document tracking
        document_table = dynamodb.Table(self, "DocumentTable",
            table_name=dynamodb_table_name or f"{bucket_name}-{environment}-documents",
            partition_key=dynamodb.Attribute(name="document_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=retention_policy,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED
        )
        
        # Apply tags to the document table
        for key, value in common_tags.items():
            document_table.node.add_metadata(key, value)
        
        # Add GSI for efficient job_id queries
        document_table.add_global_secondary_index(
            index_name="job-id-index",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING)
        )
        
        # Add GSI for efficient Salesforce Object ID queries
        document_table.add_global_secondary_index(
            index_name="salesforce-object-id-index",
            partition_key=dynamodb.Attribute(name="salesforce_object_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="document_id", type=dynamodb.AttributeType.STRING)
        )

        # Create counter table for document ID generation
        counter_table = dynamodb.Table(self, "CounterTable",
            table_name=dynamodb_counter_table_name or f"{bucket_name}-{environment}-counters",
            partition_key=dynamodb.Attribute(name="counter_name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=retention_policy,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED
        )
        
        # Apply tags to the counter table
        for key, value in common_tags.items():
            counter_table.node.add_metadata(key, value)

        # Create IAM role for Lambda functions with least-privilege permissions
        lambda_role = iam.Role(self, "LambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ],
            description="Execution role for Amazon S3-Amazon Bedrock integration Lambda functions"
        )
        
        # Apply tags to the IAM role
        for key, value in common_tags.items():
            lambda_role.node.add_metadata(key, value)

        # Add S3 permissions to the Lambda role
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3:GetObject",
                "s3:PutObject",
                "s3:ListBucket"
            ],
            resources=[
                document_bucket.bucket_arn,
                f"{document_bucket.bucket_arn}/*"
            ]
        ))

        # Add S3 permissions for output bucket if different from input bucket
        if output_bucket_name and output_bucket_name != bucket_name:
            lambda_role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket"
                ],
                resources=[
                    f"arn:aws:s3:::{output_bucket_name}",
                    f"arn:aws:s3:::{output_bucket_name}/*"
                ]
            ))

        # Add Amazon Bedrock Data Automation permissions (only if BDA processing is enabled)
        if enable_bda:
            # Extract account from BDA project ARN to support cross-account access
            bda_project_account = bda_project_arn.split(":")[4]

            # Build resource ARNs for all CRIS regions (scoped to exact profile and regions)
            bda_profile_resources = [bda_project_arn]
            for cris_region in bda_cris_regions:
                bda_profile_resources.append(
                    f"arn:{aws_partition}:bedrock:{cris_region}:{self.aws_account}:data-automation-profile/{bda_profile_name}"
                )
                if bda_project_account != self.aws_account:
                    bda_profile_resources.append(
                        f"arn:{aws_partition}:bedrock:{cris_region}:{bda_project_account}:data-automation-profile/{bda_profile_name}"
                    )

            lambda_role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeDataAutomationAsync",
                    "bedrock-data-automation-runtime:InvokeDataAutomationAsync"
                ],
                resources=bda_profile_resources
            ))

            # Add IAM PassRole permission for Amazon Bedrock Data Automation
            lambda_role.add_to_policy(iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{self.aws_account}:role/service-role/AmazonBedrockDataAutomationServiceRole*"]
            ))

        # Add DynamoDB permissions to the Lambda role (including GSI access)
        lambda_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:Query",
            ],
            resources=[
                document_table.table_arn,
                f"{document_table.table_arn}/index/*",  # GSI access
                counter_table.table_arn
            ]
        ))

        # Create Lambda layer for utils (lightweight)
        # Note: Layer version updates automatically when code changes
        utils_layer = lambda_.LayerVersion(self, "UtilsLayer",
            code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/utils-layer")),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Layer containing document utilities v2 - with Salesforce metadata support"
        )

        # Create Lambda function for invoking Amazon Bedrock Data Automation
        invoke_bda_log_group = logs.LogGroup(self, "InvokeBDALogGroup",
            log_group_name=f"/aws/lambda/InvokeBDAProject-{environment}",
            retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY
        )
        invoke_bda_function = lambda_.Function(self, "InvokeBDAFunction",
            function_name=f"InvokeBDAProject-{environment}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="invoke_bda_lambda.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/invoke-bda")),
            role=lambda_role,
            timeout=Duration.seconds(lambda_timeout),
            memory_size=lambda_memory_size,
            environment={
                "BDA_PROJECT_ARN": bda_project_arn,
                "BDA_STAGE": bda_stage,
                "BDA_PROFILE": default_bda_profile,
                "DOCUMENT_TABLE": document_table.table_name,
                "COUNTER_TABLE": counter_table.table_name,
                "ENVIRONMENT": environment,
                "ENABLE_BDA": str(enable_bda).lower(),
                "OUTPUT_BUCKET_NAME": output_bucket_name or bucket_name,
                "WRITE_SF_METADATA": str(write_sf_metadata).lower()
            },
            layers=[utils_layer],
            log_group=invoke_bda_log_group
        )
        
        # Apply tags to the Lambda function
        for key, value in common_tags.items():
            invoke_bda_function.node.add_metadata(key, value)

        # Create Lambda function for processing Amazon Bedrock Data Automation events (only if BDA enabled)
        if enable_bda:
            bda_event_processor_log_group = logs.LogGroup(self, "BDAEventProcessorLogGroup",
                log_group_name=f"/aws/lambda/BDAEventProcessor-{environment}",
                retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY
            )
            bda_event_processor_function = lambda_.Function(self, "BDAEventProcessorFunction",
                function_name=f"BDAEventProcessor-{environment}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="lambda_function.lambda_handler",
                code=lambda_.Code.from_asset(os.path.join(os.path.dirname(__file__), "lambda/bda-event-processor")),
                role=lambda_role,
                timeout=Duration.seconds(lambda_timeout),
                memory_size=lambda_memory_size,
                environment={
                    "DEBUG": "true" if environment == "dev" else "false",
                    "DOCUMENT_TABLE": document_table.table_name,
                    "ENVIRONMENT": environment,
                    "WRITE_SF_METADATA": str(write_sf_metadata).lower()
                },
                layers=[utils_layer],
                log_group=bda_event_processor_log_group
            )
            
            # Apply tags to the Lambda function
            for key, value in common_tags.items():
                bda_event_processor_function.node.add_metadata(key, value)

            # Create EventBridge rule to trigger Lambda when Amazon Bedrock Data Automation completes
            bedrock_data_automation_rule = events.Rule(self, "BedrockDataAutomationRule",
                rule_name=f"BedrockDataAutomationCompletionRule-{environment}",
                description="Rule to trigger Lambda when Amazon Bedrock Data Automation completes",
                event_pattern=events.EventPattern(
                    source=["aws.bedrock"],
                    detail_type=["Bedrock Data Automation Job Succeeded"]
                )
            )

            # Add the BDAEventProcessor Lambda as a target for the EventBridge rule
            bedrock_data_automation_rule.add_target(targets.LambdaFunction(bda_event_processor_function))

            # Add EventBridge permissions so the event processor can emit events
            lambda_role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "events:PutEvents"
                ],
                resources=[
                    f"arn:aws:events:{self.aws_region}:{self.aws_account}:event-bus/default"
                ]
            ))

        # Configure S3 event notifications to trigger the InvokeBDA Lambda for each file type
        for file_type in processed_file_types:
            document_bucket.add_event_notification(
                s3.EventType.OBJECT_CREATED,
                s3n.LambdaDestination(invoke_bda_function),
                s3.NotificationKeyFilter(prefix=s3_trigger_prefix, suffix=file_type)
            )

        # Output values
        from aws_cdk import CfnOutput
        CfnOutput(self, "S3BucketName",
            description="Name of the S3 bucket for documents and extracted regions",
            value=document_bucket.bucket_name
        )
        
        CfnOutput(self, "LoggingBucketName",
            description="Name of the S3 bucket for logs (access logs, exported CloudWatch logs)",
            value=logging_bucket.bucket_name
        )

        CfnOutput(self, "InvokeBDAFunctionName",
            description="Name of the Lambda function that invokes Bedrock Data Automation",
            value=invoke_bda_function.function_name
        )

        CfnOutput(self, "InvokeBDAFunctionArn",
            description="ARN of the Lambda function that invokes Bedrock Data Automation",
            value=invoke_bda_function.function_arn
        )
        
        if enable_bda:
            CfnOutput(self, "BDAEventProcessorFunctionName",
                description="Name of the Lambda function that processes Bedrock Data Automation events",
                value=bda_event_processor_function.function_name
            )

            CfnOutput(self, "BDAEventProcessorFunctionArn",
                description="ARN of the Lambda function that processes Bedrock Data Automation events",
                value=bda_event_processor_function.function_arn
            )
        
        if enable_bda:
            CfnOutput(self, "EventBridgeRuleName",
                description="Name of the EventBridge rule",
                value=bedrock_data_automation_rule.rule_name
            )

        CfnOutput(self, "BDAEnabled",
            description="Whether Amazon Bedrock Data Automation processing is enabled",
            value=str(enable_bda)
        )

        CfnOutput(self, "DocumentUploadPath",
            description="Path to upload documents for automatic processing",
            value=f"s3://{document_bucket.bucket_name}/input/"
        )

        CfnOutput(self, "DocumentTableName",
            description="Name of the DynamoDB table storing document records",
            value=document_table.table_name
        )

        # CDK Nag suppressions for acceptable findings
        NagSuppressions.add_resource_suppressions(
            construct=lambda_role,
            suppressions=[
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWS managed policy AWSLambdaBasicExecutionRole is acceptable for Lambda execution",
                    "appliesTo": ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
                }
            ]
        )

        # Suppress Lambda runtime warnings (Python 3.12 is latest available)
        lambda_functions_for_nag = [invoke_bda_function]
        if enable_bda:
            lambda_functions_for_nag.append(bda_event_processor_function)
        for func in lambda_functions_for_nag:
            NagSuppressions.add_resource_suppressions(
                construct=func,
                suppressions=[
                    {
                        "id": "AwsSolutions-L1",
                        "reason": "Python 3.12 is the latest available runtime version"
                    }
                ]
            )

        # Suppress bucket notifications handler (CDK managed resource)
        # Note: These suppressions only apply when CDK Nag is enabled
        try:
            NagSuppressions.add_resource_suppressions_by_path(
                stack=self,
                path=f"/{construct_id}/BucketNotificationsHandler050a0587b7544547bf325f094a3db834/Role/Resource",
                suppressions=[
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": "CDK managed resource for S3 bucket notifications",
                        "appliesTo": ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
                    }
                ]
            )

            NagSuppressions.add_resource_suppressions_by_path(
                stack=self,
                path=f"/{construct_id}/BucketNotificationsHandler050a0587b7544547bf325f094a3db834/Role/DefaultPolicy/Resource",
                suppressions=[
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": "CDK managed resource for S3 bucket notifications requires wildcard permissions",
                        "appliesTo": ["Resource::*"]
                    }
                ]
            )
        except Exception:
            pass  # Suppression paths may not exist in all configurations

        # Suppress wildcard permissions that are necessary for the application
        iam_suppressions = [
            {
                "id": "AwsSolutions-IAM5",
                "reason": "S3 bucket wildcard permissions required for document processing",
                "appliesTo": ["Resource::<DocumentBucketAE41E5A9.Arn>/*"]
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "DynamoDB Global Secondary Index (GSI) access requires wildcard permissions for index queries",
                "appliesTo": ["Resource::<DocumentTable9FE6D880.Arn>/index/*"]
            }
        ]
        if enable_bda:
            iam_suppressions.append({
                "id": "AwsSolutions-IAM5", 
                "reason": "Amazon Bedrock Data Automation requires wildcard permissions for profiles and projects",
                "appliesTo": [
                    "Resource::arn:aws:bedrock:*:<AWS::AccountId>:data-automation-profile/*",
                    "Resource::arn:aws:bedrock:us-east-1:<AWS::AccountId>:data-automation-project/*",
                    "Resource::arn:aws:iam::<AWS::AccountId>:role/service-role/AmazonBedrockDataAutomationServiceRole*"
                ]
            })
        NagSuppressions.add_resource_suppressions_by_path(
            stack=self,
            path=f"/{construct_id}/LambdaExecutionRole/DefaultPolicy/Resource",
            suppressions=iam_suppressions
        )

    def _validate_configuration(self, bucket_name: str, bda_project_arn: str, processed_file_types: list, enable_bda: bool = True):
        """Validate deployment configuration parameters"""
        
        # Validate bucket name
        if not bucket_name or len(bucket_name) < 3:
            raise ValueError("Bucket name must be at least 3 characters long")
        
        # Validate BDA project ARN format (only required when BDA is enabled)
        if enable_bda:
            if not bda_project_arn or not bda_project_arn.startswith("arn:aws:bedrock:"):
                raise ValueError("BDA project ARN must be a valid Amazon Bedrock ARN (required when enable-bda is true)")
        
        # Validate file types
        if not processed_file_types or len(processed_file_types) == 0:
            raise ValueError("At least one file type must be specified for processing")
        
        for file_type in processed_file_types:
            if not file_type.startswith("."):
                raise ValueError(f"File type '{file_type}' must start with a dot (e.g., '.pdf')")
        
        print(f"✅ Configuration validation passed:")
        print(f"   - Bucket: {bucket_name}")
        print(f"   - BDA enabled: {enable_bda}")
        if enable_bda:
            print(f"   - BDA Project: {bda_project_arn}")
        print(f"   - File types: {', '.join(processed_file_types)}")