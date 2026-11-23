from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import List, Optional
import re
import secrets
import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import json
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Message

app = FastAPI()
security = HTTPBasic()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env
load_dotenv()

# ---- CORS MIDDLEWARE ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- MIDDLEWARE FOR REQUEST/RESPONSE LOGGING ----
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Read and store the request body
        body = await request.body()
        
        # Store body in request state for exception handler
        request.state.body = body.decode("utf-8") if body else ""
        
        # Create a new request with the body so downstream handlers can read it
        async def receive() -> Message:
            return {"type": "http.request", "body": body}
        
        request._receive = receive
        
        # Process request
        response = await call_next(request)
        
        return response


# Add middleware to app
app.add_middleware(RequestLoggingMiddleware)


# ---- CUSTOM EXCEPTION HANDLER FOR VALIDATION ERRORS ----
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Get body from request state (set by middleware)
    body_str = getattr(request.state, "body", "(unable to read body)")
    
    # Log validation error details
    errors = exc.errors()
    logger.error(f"Validation error - Request body: {body_str}")
    logger.error(f"Validation errors: {json.dumps(errors, indent=2)}")
    
    # Return default 422 response format
    return JSONResponse(
        status_code=422,
        content={"detail": errors}
    )


# ---- BASIC AUTH USERNAME & PASSWORD ----
USERNAME = os.getenv("BASIC_AUTH_USERNAME")
PASSWORD = os.getenv("BASIC_AUTH_PASSWORD")

if not USERNAME or not PASSWORD:
    logger.warning("BASIC_AUTH_USERNAME or BASIC_AUTH_PASSWORD not set in environment")

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, USERNAME) if USERNAME else False
    correct_password = secrets.compare_digest(credentials.password, PASSWORD) if PASSWORD else False

    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return credentials.username


# ---- GOOGLE SHEETS CONNECTION ----
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

if not all([GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_NAME, GOOGLE_SHEET_ID]):
    logger.error("Missing required Google Sheets environment variables")

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    creds = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=scope
    )
    client = gspread.authorize(creds)
    sheet = client.open(GOOGLE_SHEET_NAME).sheet1
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets connection: {str(e)}")
    client = None
    sheet = None


class PhoneRequest(BaseModel):
    phone_number: str = Field(alias="phoneNumber")
    variable_keys: Optional[List[str]] = Field(default=None, alias="variableKeys")

    model_config = {
        "populate_by_name": True
    }

    @validator('phone_number')
    def validate_phone_number(cls, v):
        """Validate that phone number contains at least some digits"""
        if not v or not re.search(r'\d', v):
            raise ValueError('Phone number must contain at least one digit')
        return v


# ---- CORE LOOKUP LOGIC ----
def lookup_contact_by_phone(phone_number: str, variable_keys: Optional[List[str]] = None):
    """
    Shared helper that returns the response array for a given phone number.
    Always returns a JSON-array-friendly list of {variable_key, variable_value}.
    Can return any field from the Excel sheet based on the requested variable_keys.
    """
    if not client or not sheet:
        logger.error("Google Sheets client not initialized")
        return build_response({}, variable_keys)
    
    try:
        client_inst = gspread.authorize(creds)
        sheet_inst = client_inst.open_by_key(GOOGLE_SHEET_ID).sheet1
        records = sheet_inst.get_all_records()
    except Exception as e:
        logger.error(f"Error fetching records from Google Sheets: {str(e)}")
        return build_response({}, variable_keys)

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
        if target_phone in get_phone_from_row(row):
            return build_response(row, variable_keys)

    # Phone not found: return requested variables with empty values
    return build_response({}, variable_keys)


# ---- PROTECTED ENDPOINTS ----

@app.get("/data")
def get_data(phone_number: str, variable_keys: Optional[str] = None, user: str = Depends(authenticate)):
    """
    GET endpoint to lookup contact data by phone number.
    Returns a JSON array of {variable_key, variable_value} pairs.
    
    Query parameters:
    - phone_number: The phone number to lookup (required)
    - variable_keys: Comma-separated list of fields to return (optional)
    """
    # Parse variable_keys if provided
    keys_list = None
    if variable_keys:
        keys_list = [k.strip() for k in variable_keys.split(',')]
    
    # Pretty-print incoming request
    logger.info(f"GET /data - phone_number: {phone_number}, variable_keys: {keys_list}")
    
    response = lookup_contact_by_phone(phone_number, keys_list)
    
    # Pretty-print response
    logger.info(f"GET /data response: {json.dumps(response, indent=2)}")
    
    return response


@app.post("/data")
def post_data(payload: PhoneRequest, user: str = Depends(authenticate)):
    """
    POST endpoint to lookup contact data by phone number.
    Same functionality as GET /data but for POST requests.
    Always returns a JSON array of {\"variable_key\", \"variable_value\"}.
    """
    # Pretty-print incoming request
    logger.info(f"POST /data - Request body: {json.dumps(payload.dict(), indent=2)}")

    response = lookup_contact_by_phone(payload.phone_number, payload.variable_keys)

    # Pretty-print response
    logger.info(f"POST /data response: {json.dumps(response, indent=2)}")

    return response


@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "ok"}
