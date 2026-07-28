import boto3
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger()

class DocumentIdGenerator:
    def __init__(self, counter_table_name, document_table_name):
        self.dynamodb = boto3.resource('dynamodb')
        self.counter_table = self.dynamodb.Table(counter_table_name)
        self.document_table = self.dynamodb.Table(document_table_name)
    
    def generate_document_id(self):
        """Generate next 9-digit document ID"""
        try:
            response = self.counter_table.update_item(
                Key={'counter_name': 'document_id'},
                UpdateExpression='ADD #val :inc',
                ExpressionAttributeNames={'#val': 'value'},
                ExpressionAttributeValues={':inc': 1},
                ReturnValues='UPDATED_NEW'
            )
            counter_value = int(response['Attributes']['value'])
            return f"{counter_value:09d}"
        except ClientError as e:
            if e.response['Error']['Code'] == 'ValidationException':
                # Counter doesn't exist, create it
                self.counter_table.put_item(Item={'counter_name': 'document_id', 'value': 1})
                return "000000001"
            raise
    
    def store_document_record(self, document_id, s3_bucket, s3_key, filename, **kwargs):
        """Store document record in DynamoDB with optional Salesforce metadata"""
        item = {
            'document_id': document_id,
            's3_bucket': s3_bucket,
            's3_key': s3_key,
            'filename': filename,
            'status': 'uploaded'
        }
        
        # Add optional fields like salesforce_object_type and salesforce_object_id
        for key, value in kwargs.items():
            if value is not None:  # Only add non-None values
                item[key] = value
        
        self.document_table.put_item(Item=item)
    
    def get_document_record(self, document_id):
        """Get document record by ID"""
        response = self.document_table.get_item(Key={'document_id': document_id})
        return response.get('Item')
    
    def get_document_by_job_id(self, job_id):
        """Get document record by job ID using GSI"""
        response = self.document_table.query(
            IndexName='job-id-index',
            KeyConditionExpression='job_id = :job_id',
            ExpressionAttributeValues={':job_id': job_id}
        )
        items = response.get('Items', [])
        return items[0] if items else None
    
    def get_documents_by_salesforce_object(self, salesforce_object_id):
        """Get all document records for a Salesforce object ID using GSI"""
        response = self.document_table.query(
            IndexName='salesforce-object-id-index',
            KeyConditionExpression='salesforce_object_id = :sf_id',
            ExpressionAttributeValues={':sf_id': salesforce_object_id}
        )
        return response.get('Items', [])
    
    def update_document_status(self, document_id, status, **kwargs):
        """Update document status and additional fields"""
        update_expression = "SET #status = :status"
        expression_values = {':status': status}
        expression_names = {'#status': 'status'}
        
        for key, value in kwargs.items():
            update_expression += f", #{key} = :{key}"
            expression_names[f'#{key}'] = key
            expression_values[f':{key}'] = value
        
        self.document_table.update_item(
            Key={'document_id': document_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values
        )
