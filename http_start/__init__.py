import azure.functions as func
import logging
import json
import os
import uuid
import requests

def main(req: func.HttpRequest) -> func.HttpResponse:
    blob_name = req.route_params.get('blob_name')
    
    if not blob_name:
        return func.HttpResponse("Please provide blob_name in URL", status_code=400)
    
    try:
        # Import durable functions
        import azure.durable_functions as df
        
        # Create client from the request
        client = df.DurableOrchestrationClient(req)
        
        # Start the orchestration
        instance_id = client.start_new(
            "pdf_analyzer", 
            None, 
            {
                "container_name": "pdfs",
                "blob_name": blob_name
            }
        )
        
        logging.info(f"Started orchestration with ID = '{instance_id}'.")
        
        return func.HttpResponse(
            json.dumps({
                "instance_id": instance_id,
                "status": "started",
                "blob_name": blob_name
            }),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        logging.error(f"Error starting orchestration: {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500
        )
