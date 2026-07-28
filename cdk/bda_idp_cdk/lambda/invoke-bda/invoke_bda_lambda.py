import json
import os
import boto3
import logging
import uuid
from urllib.parse import unquote_plus

from document_id_generator import DocumentIdGenerator

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3_client = boto3.client('s3')

def _init_bedrock_client():
    """Lazy-init the BDA runtime client only when needed."""
    return boto3.client('bedrock-data-automation-runtime', region_name='us-east-1')

def lambda_handler(event, context):
    """
    Lambda function handler that generates document ID and invokes Bedrock Data Automation.
    """
    try:
        logger.info(f"Received event: {json.dumps(event)}")
        
        # Initialize document ID generator
        doc_generator = DocumentIdGenerator(
            os.environ['COUNTER_TABLE'],
            os.environ['DOCUMENT_TABLE']
        )
        
        # Get configuration from environment
        enable_bda = os.environ.get('ENABLE_BDA', 'true').lower() == 'true'
        project_arn = os.environ.get('BDA_PROJECT_ARN')
        stage = os.environ.get('BDA_STAGE', 'LIVE')
        profile_arn = os.environ.get('BDA_PROFILE')
        
        # Extract S3 path from event
        if 'Records' in event:
            s3_record = event['Records'][0]['s3']
            bucket_name = s3_record['bucket']['name']
            object_key = unquote_plus(s3_record['object']['key'])
            s3_path = f"s3://{bucket_name}/{object_key}"
        elif 's3Path' in event:
            s3_path = event['s3Path']
            bucket_name, object_key = parse_s3_path(s3_path)
            if 'dataAutomationProjectArn' in event:
                project_arn = event['dataAutomationProjectArn']
            if 'stage' in event:
                stage = event['stage']
            if 'dataAutomationProfileArn' in event:
                profile_arn = event['dataAutomationProfileArn']
        else:
            raise ValueError("Invalid event structure: missing S3 information")
        
        if enable_bda and not project_arn:
            raise ValueError("Data Automation Project ARN not provided")
        
        # Extract Salesforce metadata from S3 object metadata or path
        salesforce_object_type = None
        salesforce_object_id = None
        
        # First, try to parse from S3 path structure: __sfdcroot__/ObjectType/RecordId/filename
        try:
            path_parts = object_key.split('/')
            if len(path_parts) >= 3 and path_parts[0] == '__sfdcroot__':
                salesforce_object_type = path_parts[1]
                salesforce_object_id = path_parts[2]
                # Validate extracted path components to prevent path traversal
                if salesforce_object_type and (not salesforce_object_type.isalnum() or len(salesforce_object_type) > 100):
                    logger.warning(f"Invalid salesforce_object_type from path: {salesforce_object_type}")
                    salesforce_object_type = None
                if salesforce_object_id and (len(salesforce_object_id) > 18 or '..' in salesforce_object_id):
                    logger.warning(f"Invalid salesforce_object_id from path: {salesforce_object_id}")
                    salesforce_object_id = None
                if salesforce_object_type and salesforce_object_id:
                    logger.info(f"Extracted Salesforce metadata from path - Object Type: {salesforce_object_type}, Record ID: {salesforce_object_id}")
        except Exception as e:
            logger.warning(f"Could not parse Salesforce metadata from path: {str(e)}")
        
        # If not found in path, try S3 object metadata as fallback
        if not (salesforce_object_type and salesforce_object_id):
            try:
                # Get object metadata
                head_response = s3_client.head_object(Bucket=bucket_name, Key=object_key)
                metadata = head_response.get('Metadata', {})
                
                # Extract Salesforce metadata if present
                salesforce_object_type = metadata.get('salesforce-object-type')
                salesforce_object_id = metadata.get('salesforce-object-id')
                
                if salesforce_object_type and salesforce_object_id:
                    logger.info(f"Extracted Salesforce metadata from S3 metadata - Object Type: {salesforce_object_type}, Record ID: {salesforce_object_id}")
                else:
                    logger.info("No Salesforce metadata found in S3 object metadata or path")
                    
            except Exception as e:
                logger.warning(f"Could not retrieve S3 object metadata: {str(e)}")
                # Continue processing even if metadata retrieval fails
        
        # Generate document ID and store record
        document_id = doc_generator.generate_document_id()
        filename = os.path.basename(object_key)
        
        doc_generator.store_document_record(
            document_id, 
            bucket_name, 
            object_key, 
            filename,
            salesforce_object_type=salesforce_object_type,
            salesforce_object_id=salesforce_object_id
        )
        logger.info(f"Generated document ID: {document_id} for {filename}")
        
        # Write metadata.json alongside the input file immediately
        # (no need to wait for BDA to complete — all Salesforce metadata is available now)
        write_sf_metadata = os.environ.get('WRITE_SF_METADATA', 'true').lower() == 'true'
        if write_sf_metadata and (salesforce_object_type or salesforce_object_id):
            try:
                sf_metadata = {
                    'metadataAttributes': {
                        'salesforce_object_id': salesforce_object_id,
                        'salesforce_object_type': salesforce_object_type
                    }
                }
                metadata_key = f'{object_key}.metadata.json'
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=metadata_key,
                    Body=json.dumps(sf_metadata, indent=2),
                    ContentType='application/json'
                )
                logger.info(f"Wrote metadata.json to {bucket_name}/{metadata_key}")
            except Exception as e:
                logger.warning(f"Failed to write metadata.json: {str(e)}")
                # Non-fatal — continue with BDA invocation

        # If BDA is not enabled, register the document and return without invoking BDA
        if not enable_bda:
            doc_generator.update_document_status(document_id, 'registered')
            logger.info(f"BDA is disabled — skipping BDA invocation for document {document_id}")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Document registered (BDA disabled)',
                    'documentId': document_id,
                    's3Path': s3_path,
                    'enableBda': enable_bda,
                    'salesforceObjectType': salesforce_object_type,
                    'salesforceObjectId': salesforce_object_id
                })
            }

        # Create standard output path (no docid in path)
        output_bucket = os.environ.get('OUTPUT_BUCKET_NAME', bucket_name)
        output_path = f"s3://{output_bucket}/output"
        
        # Prepare BDA request
        request = {
            "clientToken": str(uuid.uuid4()),
            "inputConfiguration": {"s3Uri": s3_path},
            "outputConfiguration": {"s3Uri": output_path},
            "dataAutomationConfiguration": {
                "dataAutomationProjectArn": project_arn,
                "stage": stage
            },
            "notificationConfiguration": {
                "eventBridgeConfiguration": {"eventBridgeEnabled": True}
            }
        }
        
        if profile_arn:
            request["dataAutomationProfileArn"] = profile_arn
        
        # Invoke Bedrock Data Automation
        logger.info(f"Invoking BDA for document {document_id}")
        bedrock_data_client = _init_bedrock_client()
        response = bedrock_data_client.invoke_data_automation_async(**request)
        
        invocation_arn = response.get('invocationArn')
        job_id = invocation_arn.split('/')[-1] if invocation_arn else None
        
        # Update document record with job ID and invocation ARN
        doc_generator.update_document_status(
            document_id, 
            'processing', 
            job_id=job_id,
            invocation_arn=invocation_arn
        )
        
        logger.info(f"Successfully invoked BDA job: {job_id} for document: {document_id}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': f'Successfully invoked BDA job: {job_id}',
                'documentId': document_id,
                'jobId': job_id,
                's3Path': s3_path,
                'salesforceObjectType': salesforce_object_type,
                'salesforceObjectId': salesforce_object_id
            })
        }
        
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'message': f'Error: {str(e)}'})
        }

def parse_s3_path(s3_path):
    """Parse S3 path into bucket and key"""
    if not s3_path.startswith('s3://'):
        raise ValueError(f"Invalid S3 path: {s3_path}")
    
    path_without_prefix = s3_path[5:]
    parts = path_without_prefix.split('/', 1)
    
    if len(parts) < 2:
        raise ValueError(f"Invalid S3 path format: {s3_path}")
    
    return parts[0], unquote_plus(parts[1])
