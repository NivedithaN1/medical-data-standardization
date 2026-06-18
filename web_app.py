import os
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# Local imports
from mappings import TEST_MAPPING, UNIT_MAPPING
from normalizer import normalize_test_name, normalize_unit, init_gemini
from parser import process_json_data
from excel_writer import write_excel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web_app")

app = FastAPI(title="Clinical Normalisation Dashboard")

# Sorted list of unique canonical targets for the dropdown overrides
CANONICAL_TESTS = sorted(list(set(TEST_MAPPING.values())))

class ExcelRequest(BaseModel):
    records: List[Dict[str, Any]]

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    
    # Return placeholder if index.html is missing (though we will write it next)
    return HTMLResponse("<h1>Frontend Source Missing</h1>", status_code=404)


@app.post("/api/process")
async def process_files(
    files: List[UploadFile] = File(...),
    gemini_key: str = Form(None)
):
    """
    Accepts raw JSON patient report files, normalizes them, and returns raw parsed data.
    """
    if gemini_key and gemini_key.strip():
        log.info("Updating Gemini API Key from UI input")
        init_gemini(gemini_key.strip())
        
    all_records = []
    
    for upload in files:
        try:
            content = await upload.read()
            doc = json.loads(content.decode("utf-8"))
            records = process_json_data(doc, upload.filename)
            all_records.extend(records)
        except Exception as e:
            log.error(f"Error parsing uploaded file {upload.filename}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid JSON in file {upload.filename}")
            
    return {
        "success": True,
        "records": all_records,
        "canonical_tests": CANONICAL_TESTS
    }


@app.post("/api/generate-excel")
async def generate_excel(payload: ExcelRequest):
    """
    Takes records (potentially with manual user overrides) and builds the styled Excel workbook.
    """
    if not payload.records:
        raise HTTPException(status_code=400, detail="No records provided to export")
        
    try:
        # Create a temp file to write Excel sheet
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            
        write_excel(payload.records, tmp_path)
        
        return FileResponse(
            path=tmp_path,
            filename="standardized_clinical_records.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        log.error(f"Failed to generate Excel download: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Excel generation failed: {e}")
