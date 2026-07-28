import json
import boto3
import os
import logging
from urllib.parse import unquote_plus
from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError

from document_id_generator import DocumentIdGenerator

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize S3 and EventBridge clients with error handling
try:
    s3_client = boto3.client('s3')
    events_client = boto3.client('events')
except NoCredentialsError:
    logger.error("AWS credentials not found")
    raise
except Exception as e:
    logger.error(f"Failed to initialize AWS clients: {str(e)}")
    raise

# Use the default event bus
EVENT_BUS_NAME = 'default'


def get_json_data(bucket_name, key): 
    """
    Retrieves JSON data from an S3 object.
    Args:
        bucket_name (str): The name of the S3 bucket.
        key (str): The key (path) of the S3 object.
    Returns:
        dict: The parsed JSON data.
    Raises:
        ValueError: If bucket_name or key is invalid
        ClientError: If S3 operation fails
        json.JSONDecodeError: If content is not valid JSON
    """
    if not bucket_name or not isinstance(bucket_name, str):
        raise ValueError("bucket_name must be a non-empty string")
    if not key or not isinstance(key, str):
        raise ValueError("key must be a non-empty string")
    
    logger.info(f'Retrieving {bucket_name}/{key}')
    
    try:
        response = s3_client.get_object(
            Bucket=bucket_name,
            Key=key
        )
        
        # Read the content
        content = response['Body'].read().decode('utf-8')
        
        # Parse JSON content
        return json.loads(content)
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchBucket':
            logger.error(f"Bucket {bucket_name} does not exist")
        elif error_code == 'NoSuchKey':
            logger.error(f"Key {key} does not exist in bucket {bucket_name}")
        elif error_code == 'AccessDenied':
            logger.error(f"Access denied to {bucket_name}/{key}")
        else:
            logger.error(f"S3 error retrieving {bucket_name}/{key}: {str(e)}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON content in {bucket_name}/{key}: {str(e)}")
        raise
    except UnicodeDecodeError as e:
        logger.error(f"Unable to decode content from {bucket_name}/{key}: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving {bucket_name}/{key}: {str(e)}")
        raise

def flatten_bda_metadata(data):
    """
    Specific flattener for BDA job metadata structure
    Args:
        data (dict): The BDA metadata dictionary to flatten
    Returns:
        dict: Flattened metadata dictionary
    Raises:
        ValueError: If data is not a valid dictionary
    """
    if not isinstance(data, dict):
        raise ValueError("Input data must be a dictionary")
    
    logger.info(f'Flattening data with keys: {list(data.keys())}')
    
    try:
        flattened = {
            'job_id': data.get('job_id'),
            'job_status': data.get('job_status'),
            'semantic_modality': data.get('semantic_modality')
        }
        
        # Handle output_metadata array
        output_metadata = data.get('output_metadata', [])
        if not isinstance(output_metadata, list):
            logger.warning("output_metadata is not a list, treating as empty")
            output_metadata = []
        
        for i, metadata in enumerate(output_metadata):
            if not isinstance(metadata, dict):
                logger.warning(f"output_metadata[{i}] is not a dictionary, skipping")
                continue
                
            prefix = f'output_metadata_{i}'
            
            flattened[f'{prefix}_asset_id'] = metadata.get('asset_id')
            
            # Handle asset_input_path
            asset_input = metadata.get('asset_input_path', {})
            if isinstance(asset_input, dict):
                flattened[f'{prefix}_input_s3_bucket'] = asset_input.get('s3_bucket')
                flattened[f'{prefix}_input_s3_key'] = asset_input.get('s3_key')
            else:
                logger.warning(f"asset_input_path in output_metadata[{i}] is not a dictionary")
            
            # Handle segment_metadata array
            segments = metadata.get('segment_metadata', [])
            if not isinstance(segments, list):
                logger.warning(f"segment_metadata in output_metadata[{i}] is not a list")
                segments = []
                
            for j, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    logger.warning(f"segment_metadata[{j}] in output_metadata[{i}] is not a dictionary, skipping")
                    continue
                    
                seg_prefix = f'{prefix}_segment_{j}'
                
                # Safely extract output paths with fallback handling
                standard_output_path = segment.get('standard_output_path')
                custom_output_path = segment.get('custom_output_path')
                
                # Only add paths if they exist and are valid strings
                if standard_output_path and isinstance(standard_output_path, str):
                    flattened[f'{seg_prefix}_standard_output_path'] = standard_output_path
                else:
                    logger.debug(f"standard_output_path missing or invalid in segment {j} of output_metadata[{i}]")
                
                if custom_output_path and isinstance(custom_output_path, str):
                    flattened[f'{seg_prefix}_custom_output_path'] = custom_output_path
                else:
                    logger.debug(f"custom_output_path missing or invalid in segment {j} of output_metadata[{i}]")
                
                # Add a combined output path field that prioritizes custom over standard
                output_path = custom_output_path if (custom_output_path and isinstance(custom_output_path, str)) else standard_output_path
                if output_path and isinstance(output_path, str):
                    flattened[f'{seg_prefix}_output_path'] = output_path
                    flattened[f'{seg_prefix}_output_path_type'] = 'custom' if custom_output_path else 'standard'
                else:
                    logger.warning(f"No valid output path found in segment {j} of output_metadata[{i}]")
                
                # Safely extract custom_output_status
                custom_output_status = segment.get('custom_output_status')
                if custom_output_status is not None:
                    flattened[f'{seg_prefix}_custom_output_status'] = custom_output_status
        
        return flattened
        
    except Exception as e:
        logger.error(f"Error flattening BDA metadata: {str(e)}")
        raise
def map_bda_output_keys(flattened_data):
    """
    Maps flattened BDA output keys to custom field names.
    
    Args:
        flattened_data (dict): The flattened BDA metadata dictionary
        
    Returns:
        dict: Dictionary with mapped key names
    Raises:
        ValueError: If flattened_data is not a dictionary
    """
    if not isinstance(flattened_data, dict):
        raise ValueError("flattened_data must be a dictionary")
    
    try:
        # Define your key mappings here
        key_mappings = {
            # Root level fields
            'document_id': 'documentID__c',
            # 'job_status': 'processing_status',
            # 'semantic_modality': 'document_type',
            
            # Asset metadata mappings
            # 'output_metadata_0_asset_id': 'primary_asset_id',
            # 'output_metadata_0_input_s3_bucket': 'asset_input_path__c',
            'input_s3_path': 'asset_input_path__c',
            
            # Segment metadata mappings
            # 'output_metadata_0_segment_0_standard_output_path': 'standard_result_location',
            'output_metadata_0_segment_0_custom_output_path': 'custom_output_path__c',
            # 'output_metadata_0_segment_0_custom_output_status': 'processing_result_status',
            
            # Add more mappings as needed...
            # 'original_key': 'new_key_name',
        }
        
        # Apply the mappings
        mapped_data = {}
        for original_key, value in flattened_data.items():
            if original_key in key_mappings:
                mapped_key = key_mappings[original_key]
                mapped_data[mapped_key] = value
            # Keys not in key_mappings are excluded
        
        return mapped_data
        
    except Exception as e:
        logger.error(f"Error mapping BDA output keys: {str(e)}")
        raise

def lambda_handler(event, context):
    """
    Lambda function that processes Bedrock Data Automation job completion events.
    Reads data from the S3 bucket specified in the output_s3_location and
    sends the processed data to an EventBridge event bus.
    
    Args:
        event (dict): The EventBridge event
        context (object): Lambda context
        
    Returns:
        dict: Response with processing status
    """
    logger.info(f"Received event: {json.dumps(event, default=str)}")
    
    try:
        # Validate input event structure
        if not isinstance(event, dict):
            raise ValueError("Event must be a dictionary")
        
        # Extract details from the event
        detail = event.get('detail', {})
        if not isinstance(detail, dict):
            raise ValueError("Event detail must be a dictionary")
        
        # Extract S3 information from the output_s3_location
        output_location = detail.get('output_s3_location', {})
        if not isinstance(output_location, dict):
            raise ValueError("output_s3_location must be a dictionary")
        
        bucket_name = output_location.get('s3_bucket')
        object_key = output_location.get('name')
        
        if not bucket_name or not object_key:
            error_msg = f"Invalid or missing S3 output location: {output_location}"
            logger.error(error_msg)
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid or missing S3 output location', 'details': output_location})
            }
        
        # Validate S3 location format
        if not isinstance(bucket_name, str) or not isinstance(object_key, str):
            error_msg = "S3 bucket name and object key must be strings"
            logger.error(error_msg)
            return {
                'statusCode': 400,
                'body': json.dumps({'error': error_msg})
            }
        
        # Go up one parent directory by removing the last component of the path
        path_components = object_key.split('/')
        if len(path_components) > 1:
            # Remove the last component
            path_components.pop()
            # Join the remaining components to form the parent directory path
            parent_prefix = '/'.join(path_components)
            # Add trailing slash if not present
            if not parent_prefix.endswith('/'):
                parent_prefix += '/'
        else:
            # If there's only one component or none, use empty string (root)
            parent_prefix = ""
        
        logger.info(f"Original prefix: {object_key}")
        logger.info(f"Parent prefix: {parent_prefix}")
        
        # List objects in the parent prefix with error handling
        try:
            response = s3_client.list_objects_v2(
                Bucket=bucket_name,
                Prefix=parent_prefix
            )
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'NoSuchBucket':
                error_msg = f"Bucket {bucket_name} does not exist"
            elif error_code == 'AccessDenied':
                error_msg = f"Access denied to bucket {bucket_name}"
            else:
                error_msg = f"S3 error listing objects in {bucket_name}/{parent_prefix}: {str(e)}"
            
            logger.error(error_msg)
            return {
                'statusCode': 404 if error_code == 'NoSuchBucket' else 403,
                'body': json.dumps({'error': error_msg})
            }
        
        if 'Contents' not in response or len(response['Contents']) == 0:
            error_msg = f"No objects found in {bucket_name}/{parent_prefix}"
            logger.error(error_msg)
            return {
                'statusCode': 404,
                'body': json.dumps({'error': 'No output files found', 'location': f"{bucket_name}/{parent_prefix}"})
            }
        
        # Find the job_metadata.json file specifically
        job_metadata_key = None
        for obj in response['Contents']:
            if obj['Key'].endswith('job_metadata.json'):
                job_metadata_key = obj['Key']
                break
        
        if not job_metadata_key:
            error_msg = f"job_metadata.json file not found in {bucket_name}/{parent_prefix}"
            logger.error(error_msg)
            available_files = [obj['Key'] for obj in response['Contents']]
            return {
                'statusCode': 404,
                'body': json.dumps({
                    'error': 'job_metadata.json file not found',
                    'location': f"{bucket_name}/{parent_prefix}",
                    'available_files': available_files
                })
            }
        
        logger.info(f"Found job_metadata.json file: {job_metadata_key}")
        
        # Get the job_metadata.json object from S3
        try:
            data = get_json_data(bucket_name, job_metadata_key)
        except Exception as e:
            logger.error(f"Failed to retrieve or parse job metadata: {str(e)}")
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'error': 'Failed to retrieve job metadata',
                    'details': str(e),
                    'file': f"{bucket_name}/{job_metadata_key}"
                })
            }
        
        # Apply metadata to S3 with error handling - removed (write-bda-metadata feature removed)

        # Process the data as needed
        logger.info(f"Successfully processed data from {bucket_name}/{job_metadata_key}")
        
        # Extract metadata from the event with validation
        job_id = detail.get('job_id', 'unknown')
        job_status = detail.get('job_status', 'unknown')
        input_s3_object = detail.get('input_s3_object', {})
        
        if not isinstance(input_s3_object, dict):
            logger.warning("input_s3_object is not a dictionary, using default values")
            input_s3_object = {}
        
        input_file = input_s3_object.get('name', 'unknown')
        
        # Get document ID using job_id from DynamoDB
        job_id = detail.get('job_id', 'unknown')
        doc_id = "unknown"
        
        if job_id != "unknown":
            try:
                doc_generator = DocumentIdGenerator(
                    os.environ.get('COUNTER_TABLE', ''),
                    os.environ['DOCUMENT_TABLE']
                )
                # Find document by job_id
                document = doc_generator.get_document_by_job_id(job_id)
                if document:
                    doc_id = document['document_id']
                    
                    # Collect only result.json files (filter out other files)
                    output_file_keys = []
                    for obj in response['Contents']:
                        key = obj['Key']
                        # Only include files that end with result.json
                        if key.endswith('result.json'):
                            output_file_keys.append(key)
                    
                    # Update document status with comprehensive output information
                    doc_generator.update_document_status(
                        doc_id, 
                        'completed',
                        job_id=job_id,
                        job_status=job_status,
                        bda_output_path=f"s3://{bucket_name}/{parent_prefix}",
                        job_metadata_file=f"s3://{bucket_name}/{job_metadata_key}",
                        output_bucket=bucket_name,
                        output_file_keys=output_file_keys,
                        total_output_files=len(output_file_keys)
                    )
                    logger.info(f"Updated document {doc_id} with {len(output_file_keys)} result.json files in bucket {bucket_name}")

                    # Write metadata sidecar files alongside each result.json for KB metadata filtering
                    write_sf_metadata = os.environ.get('WRITE_SF_METADATA', 'true').lower() == 'true'
                    salesforce_object_id = document.get('salesforce_object_id', '')
                    salesforce_object_type = document.get('salesforce_object_type', '')
                    if write_sf_metadata and (salesforce_object_id or salesforce_object_type):
                        sf_metadata = {
                            'metadataAttributes': {
                                'salesforce_object_id': salesforce_object_id,
                                'salesforce_object_type': salesforce_object_type
                            }
                        }
                        for result_key in output_file_keys:
                            try:
                                metadata_key = f'{result_key}.metadata.json'
                                s3_client.put_object(
                                    Bucket=bucket_name,
                                    Key=metadata_key,
                                    Body=json.dumps(sf_metadata, indent=2),
                                    ContentType='application/json'
                                )
                                logger.info(f"Wrote metadata sidecar: {bucket_name}/{metadata_key}")
                            except Exception as e:
                                logger.warning(f"Failed to write metadata sidecar for {result_key}: {str(e)}")
                else:
                    logger.warning(f"No document found for job_id: {job_id}")
            except Exception as e:
                logger.error(f"Failed to lookup/update document: {str(e)}")
        
        # Flatten BDA metadata with error handling
        try:
            bda_output = flatten_bda_metadata(data)
        except Exception as e:
            logger.error(f"Failed to flatten BDA metadata: {str(e)}")
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'error': 'Failed to flatten BDA metadata',
                    'details': str(e)
                })
            }
        
        # Safely construct input S3 path
        try:
            input_s3_bucket = bda_output.get('output_metadata_0_input_s3_bucket', '')
            input_s3_key = bda_output.get('output_metadata_0_input_s3_key', '')
            
            if input_s3_bucket and input_s3_key:
                bda_output['input_s3_path'] = f's3://{input_s3_bucket}{input_s3_key}'
            else:
                logger.warning("Missing S3 bucket or key information for input path construction")
                bda_output['input_s3_path'] = 'unknown'
        except Exception as e:
            logger.error(f"Error constructing input S3 path: {str(e)}")
            bda_output['input_s3_path'] = 'unknown'
        
        logger.info(f'Flattened BDA metadata: {bda_output}')

        # Prepare the event to send to EventBridge
        event_detail = {
            'document_id': doc_id
        }
        event_detail.update(bda_output)
        
        # Map BDA output keys with error handling
        try:
            event_detail = map_bda_output_keys(event_detail)
        except Exception as e:
            logger.error(f"Failed to map BDA output keys: {str(e)}")
            # Continue with unmapped keys if mapping fails
        
        logger.info(f"Final event detail: {event_detail}")

        # Send the event to EventBridge with error handling
        try:
            eventbridge_response = events_client.put_events(
                Entries=[
                    {
                        'Source': 'custom.bda-event-processor',
                        'DetailType': 'BDA Processing Complete',
                        'Detail': json.dumps(event_detail, default=str),
                        'EventBusName': EVENT_BUS_NAME
                    }
                ]
            )
            
            # Check for failed entries
            if eventbridge_response.get('FailedEntryCount', 0) > 0:
                failed_entries = eventbridge_response.get('Entries', [])
                error_msg = f"EventBridge put_events had {eventbridge_response['FailedEntryCount']} failed entries"
                logger.error(f"{error_msg}: {failed_entries}")
                return {
                    'statusCode': 500,
                    'body': json.dumps({
                        'error': error_msg,
                        'failed_entries': failed_entries
                    })
                }
            
            logger.info(f"Successfully sent event to EventBridge: {eventbridge_response}")
            
        except ClientError as e:
            error_msg = f"EventBridge error: {str(e)}"
            logger.error(error_msg)
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'error': 'Failed to send event to EventBridge',
                    'details': str(e)
                })
            }
        except Exception as e:
            error_msg = f"Unexpected error sending event to EventBridge: {str(e)}"
            logger.error(error_msg)
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'error': 'Unexpected error sending event to EventBridge',
                    'details': str(e)
                })
            }
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully processed BDA output and sent to EventBridge',
                'jobId': job_id,
                'jobStatus': job_status,
                'documentId': doc_id,
                'inputFile': input_file,
                'outputFile': job_metadata_key,
                'eventBusName': EVENT_BUS_NAME,
                'eventBridgeResponse': {
                    'ResponseMetadata': eventbridge_response.get('ResponseMetadata', {}),
                    'FailedEntryCount': eventbridge_response.get('FailedEntryCount', 0)
                }
            })
        }
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return {
            'statusCode': 400,
            'body': json.dumps({
                'error': 'Validation error',
                'details': str(e)
            })
        }
    except Exception as e:
        logger.error(f"Unexpected error processing event: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Unexpected error processing event',
                'details': str(e)
            })
        }
