import azure.functions as func
import logging
import json
import os
import io
import re
import datetime
from typing import Dict, Any
from azure.durable_functions import Blueprint
from azure.storage.blob import BlobServiceClient
from azure.data.tables import TableServiceClient, TableEntity
import PyPDF2
import pdfplumber

app = func.FunctionApp()
bp = Blueprint()

# Constants
SENSITIVE_PATTERNS = {
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    'phone': r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',
    'url': r'https?://[^\s]+',
    'date': r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b'
}

# -----------------------------------------------------------------------------
# HTTP Trigger: Get results
# -----------------------------------------------------------------------------
@bp.route(route="get_results/{blob_name}")
async def get_results(req: func.HttpRequest) -> func.HttpResponse:
    blob_name = req.route_params.get('blob_name')
    
    if not blob_name:
        return func.HttpResponse("Please provide blob_name in URL", status_code=400)
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        table_service = TableServiceClient.from_connection_string(connection_string)
        table_client = table_service.get_table_client("PDFAnalysisResults")
        
        query = f"PartitionKey eq 'PDFAnalysis' and blob_name eq '{blob_name}'"
        entities = list(table_client.query_entities(query))
        
        if not entities:
            return func.HttpResponse(f"No results found for blob: {blob_name}", status_code=404)
        
        sorted_entities = sorted(
            entities,
            key=lambda x: x.get('analysis_time', ''),
            reverse=True
        )
        latest = sorted_entities[0]
        report = json.loads(latest["report"])
        
        return func.HttpResponse(
            json.dumps({
                "blob_name": blob_name,
                "analysis_time": latest["analysis_time"],
                "report": report
            }, indent=2),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        logging.error(f"Failed to retrieve results: {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500
        )

# -----------------------------------------------------------------------------
# Blob Trigger
# -----------------------------------------------------------------------------
@bp.blob_trigger(arg_name="myblob", path="pdfs/{name}", connection="AzureWebJobsStorage")
async def blob_trigger_pdf_analysis(myblob: func.InputStream) -> None:
    name = myblob.name.split('/')[-1]
    logging.info(f"Blob trigger: {name}")
    
    if not name.lower().endswith('.pdf'):
        return
    
    try:
        import requests
        hostname = os.environ.get('WEBSITE_HOSTNAME', 'localhost:7071')
        url = f"https://{hostname}/api/start_pdf_analysis/{name}"
        response = requests.post(url)
        
        if response.status_code == 200:
            logging.info(f"Started orchestration for {name}")
        else:
            logging.error(f"Failed to start orchestration: {response.text}")
    except Exception as e:
        logging.error(f"Error: {str(e)}")

# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------
@bp.orchestration_trigger(context_name="context")
def pdf_analyzer(context):
    input_data = context.get_input()
    container_name = input_data["container_name"]
    blob_name = input_data["blob_name"]
    
    logging.info(f"Orchestrator started: {blob_name}")
    
    try:
        tasks = [
            context.call_activity("extract_text", {"container_name": container_name, "blob_name": blob_name}),
            context.call_activity("extract_metadata", {"container_name": container_name, "blob_name": blob_name}),
            context.call_activity("analyze_statistics", {"container_name": container_name, "blob_name": blob_name}),
            context.call_activity("detect_sensitive_data", {"container_name": container_name, "blob_name": blob_name})
        ]
        
        results = yield context.task_all(tasks)
        text_content, metadata, statistics, sensitive_data = results
        
        report = yield context.call_activity("create_report", {
            "blob_name": blob_name,
            "text_content": text_content,
            "metadata": metadata,
            "statistics": statistics,
            "sensitive_data": sensitive_data,
            "analysis_time": datetime.datetime.utcnow().isoformat()
        })
        
        yield context.call_activity("store_results", {
            "blob_name": blob_name,
            "report": report
        })
        
        return {"blob_name": blob_name, "status": "completed"}
    except Exception as e:
        logging.error(f"Orchestrator failed: {str(e)}")
        return {"blob_name": blob_name, "status": "failed", "error": str(e)}

# -----------------------------------------------------------------------------
# Activity: Extract text
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def extract_text(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    container_name = input["container_name"]
    
    logging.info(f"Extracting text from {blob_name}")
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)
        blob_data = blob_client.download_blob().readall()
        
        text_content = ""
        total_pages = 0
        
        with io.BytesIO(blob_data) as pdf_file:
            with pdfplumber.open(pdf_file) as pdf:
                total_pages = len(pdf.pages)
                for page_num, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text() or ""
                    text_content += f"\n--- Page {page_num} ---\n{page_text}"
        
        return {"blob_name": blob_name, "text": text_content, "total_pages": total_pages}
    except Exception as e:
        logging.error(f"Text extraction failed: {str(e)}")
        return {"blob_name": blob_name, "text": "", "total_pages": 0, "error": str(e)}

# -----------------------------------------------------------------------------
# Activity: Extract metadata
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def extract_metadata(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    container_name = input["container_name"]
    
    logging.info(f"Extracting metadata from {blob_name}")
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)
        blob_data = blob_client.download_blob().readall()
        
        metadata = {
            "blob_name": blob_name,
            "file_size": len(blob_data),
            "upload_time": datetime.datetime.utcnow().isoformat()
        }
        
        with io.BytesIO(blob_data) as pdf_file:
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            if pdf_reader.metadata:
                doc_info = pdf_reader.metadata
                metadata["title"] = str(doc_info.get('/Title', ''))
                metadata["author"] = str(doc_info.get('/Author', ''))
                metadata["subject"] = str(doc_info.get('/Subject', ''))
                metadata["creator"] = str(doc_info.get('/Creator', ''))
                metadata["producer"] = str(doc_info.get('/Producer', ''))
                metadata["creation_date"] = str(doc_info.get('/CreationDate', ''))
                metadata["modification_date"] = str(doc_info.get('/ModDate', ''))
            metadata["total_pages"] = len(pdf_reader.pages)
        
        return metadata
    except Exception as e:
        logging.error(f"Metadata extraction failed: {str(e)}")
        return {"blob_name": blob_name, "error": str(e)}

# -----------------------------------------------------------------------------
# Activity: Analyze statistics
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def analyze_statistics(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    container_name = input["container_name"]
    
    logging.info(f"Analyzing statistics for {blob_name}")
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)
        blob_data = blob_client.download_blob().readall()
        
        total_words = 0
        total_pages = 0
        page_word_counts = []
        
        with io.BytesIO(blob_data) as pdf_file:
            with pdfplumber.open(pdf_file) as pdf:
                total_pages = len(pdf.pages)
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    words = page_text.split()
                    word_count = len(words)
                    total_words += word_count
                    page_word_counts.append(word_count)
        
        avg_words_per_page = total_words / total_pages if total_pages > 0 else 0
        estimated_reading_time_minutes = total_words / 200
        
        return {
            "blob_name": blob_name,
            "total_pages": total_pages,
            "total_words": total_words,
            "avg_words_per_page": avg_words_per_page,
            "estimated_reading_time": f"{estimated_reading_time_minutes:.1f} minutes",
            "page_word_counts": page_word_counts
        }
    except Exception as e:
        logging.error(f"Statistics analysis failed: {str(e)}")
        return {"blob_name": blob_name, "error": str(e)}

# -----------------------------------------------------------------------------
# Activity: Detect sensitive data
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def detect_sensitive_data(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    container_name = input["container_name"]
    
    logging.info(f"Detecting sensitive data in {blob_name}")
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)
        blob_data = blob_client.download_blob().readall()
        
        text_content = ""
        with io.BytesIO(blob_data) as pdf_file:
            with pdfplumber.open(pdf_file) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    text_content += page_text + "\n"
        
        sensitive_data = {
            "emails": [],
            "phone_numbers": [],
            "urls": [],
            "dates": []
        }
        
        for pattern_name, pattern in SENSITIVE_PATTERNS.items():
            matches = re.findall(pattern, text_content)
            if pattern_name == 'email':
                sensitive_data["emails"] = list(set(matches))
            elif pattern_name == 'phone':
                sensitive_data["phone_numbers"] = list(set(matches))
            elif pattern_name == 'url':
                sensitive_data["urls"] = list(set(matches))
            elif pattern_name == 'date':
                sensitive_data["dates"] = list(set(matches))
        
        return {
            "blob_name": blob_name,
            "sensitive_data": sensitive_data,
            "total_findings": sum(len(v) for v in sensitive_data.values())
        }
    except Exception as e:
        logging.error(f"Sensitive data detection failed: {str(e)}")
        return {"blob_name": blob_name, "sensitive_data": {}, "total_findings": 0, "error": str(e)}

# -----------------------------------------------------------------------------
# Activity: Create report
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def create_report(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    logging.info(f"Creating report for {blob_name}")
    
    text_content = input.get("text_content", {})
    text_preview = text_content.get("text", "")[:500]
    if len(text_content.get("text", "")) > 500:
        text_preview += "..."
    
    return {
        "blob_name": blob_name,
        "analysis_time": input.get("analysis_time", datetime.datetime.utcnow().isoformat()),
        "text_analysis": {
            "total_pages": text_content.get("total_pages", 0),
            "preview": text_preview
        },
        "metadata": input.get("metadata", {}),
        "statistics": input.get("statistics", {}),
        "sensitive_data": input.get("sensitive_data", {}),
        "summary": {
            "total_pages": input.get("statistics", {}).get("total_pages", 0),
            "total_words": input.get("statistics", {}).get("total_words", 0),
            "sensitive_findings": input.get("sensitive_data", {}).get("total_findings", 0),
            "estimated_reading_time": input.get("statistics", {}).get("estimated_reading_time", "Unknown")
        }
    }

# -----------------------------------------------------------------------------
# Activity: Store results
# -----------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def store_results(input: Dict[str, Any]) -> Dict[str, Any]:
    blob_name = input["blob_name"]
    report = input["report"]
    
    logging.info(f"Storing results for {blob_name}")
    
    try:
        connection_string = os.environ["AzureWebJobsStorage"]
        table_service = TableServiceClient.from_connection_string(connection_string)
        
        table_name = "PDFAnalysisResults"
        try:
            table_service.create_table(table_name)
            logging.info(f"Created table: {table_name}")
        except Exception as e:
            logging.info(f"Table {table_name} already exists")
        
        table_client = table_service.get_table_client(table_name)
        
        entity = TableEntity()
        entity["PartitionKey"] = "PDFAnalysis"
        entity["RowKey"] = f"{blob_name}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        entity["blob_name"] = blob_name
        entity["analysis_time"] = report["analysis_time"]
        entity["report"] = json.dumps(report)
        entity["total_pages"] = report["summary"]["total_pages"]
        entity["total_words"] = report["summary"]["total_words"]
        entity["sensitive_findings"] = report["summary"]["sensitive_findings"]
        
        metadata = report.get("metadata", {})
        entity["author"] = metadata.get("author", "Unknown")
        entity["title"] = metadata.get("title", "Unknown")
        entity["file_size"] = metadata.get("file_size", 0)
        
        table_client.create_entity(entity)
        
        logging.info(f"Results stored successfully for {blob_name}")
        return {"blob_name": blob_name, "status": "stored"}
    except Exception as e:
        logging.error(f"Failed to store results: {str(e)}")
        return {"blob_name": blob_name, "status": "failed", "error": str(e)}

app.register_blueprint(bp)
