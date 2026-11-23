from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
import re
import secrets
import os
import threading
import time
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import json
import logging

app = FastAPI()
security = HTTPBasic()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env
load_dotenv()


# ---- CUSTOM EXCEPTION HANDLER FOR VALIDATION ERRORS ----
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()

    # Log only where and why validation failed, never the submitted values (PII)
    safe_errors = [{"loc": e.get("loc"), "type": e.get("type"), "msg": e.get("msg")} for e in errors]
    logger.error(f"Validation errors: {json.dumps(safe_errors, default=str)}")

    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(errors)}
    )


# ---- BASIC AUTH USERNAME & PASSWORD ----
USERNAME = os.getenv("BASIC_AUTH_USERNAME")
PASSWORD = os.getenv("BASIC_AUTH_PASSWORD")

if not USERNAME or not PASSWORD:
    raise RuntimeError("BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD must be set")

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username.encode("utf-8"), USERNAME.encode("utf-8"))
    correct_password = secrets.compare_digest(credentials.password.encode("utf-8"), PASSWORD.encode("utf-8"))

    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return credentials.username


# ---- GOOGLE SHEETS CONNECTION ----
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
# How long sheet rows are reused before re-reading from Google (seconds)
SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "60"))

scope = [
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]

creds = Credentials.from_service_account_file(
    GOOGLE_SERVICE_ACCOUNT_FILE,
    scopes=scope
)

client = gspread.authorize(creds)

_records_cache: List[dict] = []
_records_fetched_at: float = 0.0
_records_lock = threading.Lock()


def get_sheet_records() -> List[dict]:
    """
    Return all sheet rows, re-reading from Google at most once per SHEET_CACHE_TTL_SECONDS.
    If Google fails, serve the last good copy; with no copy yet, raise 503.
    """
    global _records_cache, _records_fetched_at

    with _records_lock:
        if _records_fetched_at and time.monotonic() - _records_fetched_at < SHEET_CACHE_TTL_SECONDS:
            return _records_cache

        try:
            _records_cache = client.open_by_key(GOOGLE_SHEET_ID).sheet1.get_all_records()
            _records_fetched_at = time.monotonic()
        except Exception:
            logger.exception("Failed to read Google Sheet")
            if not _records_fetched_at:
                raise HTTPException(status_code=503, detail="Contact data temporarily unavailable")
            logger.warning("Serving stale sheet data")

        return _records_cache


class PhoneRequest(BaseModel):
    phone_number: str = Field(alias="phoneNumber")
    variable_keys: Optional[List[str]] = Field(default=None, alias="variableKeys")

    model_config = {
        "populate_by_name": True
    }


# ---- CORE LOOKUP LOGIC ----
def lookup_contact_by_phone(phone_number: str, variable_keys: Optional[List[str]] = None):
    """
    Shared helper that returns the response array for a given phone number.
    Always returns a JSON-array-friendly list of {variable_key, variable_value}.
    Can return any field from the Excel sheet based on the requested variable_keys.
    """
    records = get_sheet_records()

    # Normalise phone number to last 10 digits (ignore +, spaces, country code, etc.)
    def normalize_phone(value) -> str:
        digits = re.sub(r"\D", "", str(value))
        # Use last 10 digits for matching (typical local number length)
        return digits[-10:] if len(digits) >= 10 else digits

    target_phone = normalize_phone(phone_number)

    def build_response(row: dict, variable_keys: Optional[List[str]] = None) -> list[dict]:
        """
        Build response from row data. Supports any field from the Excel sheet.
        Special handling for 'contact_name' which concatenates First Name and Last Name.
        """
        # Handle special case: contact_name is a concatenation of First Name and Last Name
        if row:
            full_name = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            # Add contact_name to the row data if it doesn't exist
            row_data = row.copy()
            row_data["contact_name"] = full_name
        else:
            row_data = {}

        # If variable_keys are provided, return only those keys
        if variable_keys:
            keys_to_return = variable_keys
        else:
            # If no keys specified, return all available fields from the row
            keys_to_return = list(row_data.keys())

        # Build response with requested keys
        response = []
        for key in keys_to_return:
            value = row_data.get(key, "")
            response.append({
                "variable_key": key,
                "variable_value": str(value) if value is not None else ""
            })

        return response

    # All possible phone column header names across the 5 phone slots
    phone_column_candidates = [
        # Slot 1
        ["Phone_Number", "Phone Number", "phone_number", "Phone"],
        # Slot 2
        ["Phone_Number_2", "Phone_Number2", "Phone Number 2", "Phone2"],
        # Slot 3
        ["Phone_Number_3", "Phone_Number3", "Phone Number 3", "Phone3"],
        # Slot 4
        ["Phone_Number_4", "Phone_Number4", "Phone Number 4", "Phone4"],
        # Slot 5
        ["Phone_Number_5", "Phone_Number5", "Phone Number 5", "Phone5"],
    ]

    def get_phone_from_row(row: dict) -> list[str]:
        """Return all non-empty normalised phone values found in the row."""
        values = []
        for candidates in phone_column_candidates:
            raw = next((row[k] for k in candidates if k in row and row[k]), None)
            if raw:
                values.append(normalize_phone(raw))
        return values

    for row in records:
        if target_phone and target_phone in get_phone_from_row(row):
            return build_response(row, variable_keys)

    # Phone not found: return requested variables with empty values
    return build_response({}, variable_keys)


# ---- PROTECTED ENDPOINTS ----



@app.post("/data")
def post_data(payload: PhoneRequest, user: str = Depends(authenticate)):
    """
    Look up a contact by phone number.
    Always returns a JSON array of {\"variable_key\", \"variable_value\"}.
    """
    response = lookup_contact_by_phone(payload.phone_number, payload.variable_keys)

    # Log only a masked phone and whether it matched, never the contact data (PII)
    matched = any(item["variable_value"] for item in response)
    logger.info(f"Lookup for phone ***{payload.phone_number[-4:]}: matched={matched}")

    return response
