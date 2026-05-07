'''
import csv
import os
import logging
from config import Config

logger = logging.getLogger(__name__)

# Use absolute path relative to this file, or fallback to config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

AIRPORT_MAP = {}
AIRLINE_MAP = {}

# Combined priority aliases - using the more comprehensive set from the second block
MANUAL_ALIASES = {
    "pune": "VAPO", "mumbai": "VABB", "bangalore": "VOBL", "hyderabad": "VOHS",
    "delhi": "VIDP", "chennai": "VOMM", "kolkata": "VECC", "ahmedabad": "VAAH",
    "pune airport": "VAPO", "mumbai airport": "VABB", "delhi airport": "VIDP"
}

def load_maps():
    """Load airport and airline mappings from CSV files."""
    try:
        # 1. Load Manual Aliases (Highest Priority)
        for name, icao in MANUAL_ALIASES.items():
            AIRPORT_MAP[name.lower()] = icao.upper()
            
        # 2. Load Airports CSV (City/IATA to ICAO)
        ap_path = os.path.join(DATA_DIR, "airports.csv")
        if os.path.exists(ap_path):
            with open(ap_path, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    city = row.get('Location', '').strip().lower()
                    iata = row.get('IATA', '').strip().lower()
                    icao = row.get('ICAO', '').strip().upper()
                    if icao:
                        if city and city not in AIRPORT_MAP: 
                            AIRPORT_MAP[city] = icao
                        if iata and iata not in AIRPORT_MAP: 
                            AIRPORT_MAP[iata] = icao

        # 3. Load Airlines CSV (Mapping to ICAO Code, e.g., 'IGO', 'AIC')
        al_path = os.path.join(DATA_DIR, "airlines.csv")
        if os.path.exists(al_path):
            with open(al_path, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    icao = row.get('ICAO', '').strip().upper()
                    name = row.get('Name', '').strip().lower()
                    # We use ICAO here (3 letters)
                    if icao and len(icao) == 3 and name:
                        AIRLINE_MAP[name.split()[0]] = icao
                        
        logger.info(f"Loaded {len(AIRPORT_MAP)} airport mappings and {len(AIRLINE_MAP)} airline mappings")
    except Exception as e:
        logger.error(f"Failed to load mapping files: {e}")

def resolve_airport(text):
    """Converts extracted text to ICAO (e.g., 'delhi' -> 'VIDP')."""
    if not text:
        return text
    clean = str(text).strip().lower()
    return AIRPORT_MAP.get(clean, text.upper())

def resolve_callsign(text):
    """Converts extracted text to ICAO callsign (e.g., 'Indigo 123' -> 'IGO123')."""
    if not text:
        return text
    clean = str(text).strip().lower()
    # Check if a known airline name is in the text
    for name, icao in AIRLINE_MAP.items():
        if name in clean:
            num = ''.join(filter(str.isdigit, clean))
            return f"{icao}{num}"
    
    # Fallback: if user typed '6E123', the backend or a secondary mapper 
    # should handle IATA-to-ICAO. For this resolver, we return cleaned upper.
    return clean.upper().replace(" ", "")

# Initialize mappings on import
load_maps()


import csv
import os

DATA_DIR = "../raga_flight_status/data/"
AIRPORT_MAP = {}
AIRLINE_MAP = {}

# Priority aliases from AviationSemanticRouter in bot_db.py
MANUAL_ALIASES = {
    "pune": "VAPO", "mumbai": "VABB", "bangalore": "VOBL", "hyderabad": "VOHS",
    "delhi": "VIDP", "chennai": "VOMM", "kolkata": "VECC", "ahmedabad": "VAAH",
    "pune airport": "VAPO", "mumbai airport": "VABB", "delhi airport": "VIDP"
}

def load_maps():
    # 1. Load Manual Aliases (Highest Priority)
    for name, icao in MANUAL_ALIASES.items():
        AIRPORT_MAP[name.lower()] = icao.upper()
        
    # 2. Load Airports CSV (City to ICAO)
    ap_path = os.path.join(DATA_DIR, "airports.csv")
    if os.path.exists(ap_path):
        with open(ap_path, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                city = row.get('Location', '').strip().lower()
                icao = row.get('ICAO', '').strip().upper()
                if icao and city and city not in AIRPORT_MAP:
                    AIRPORT_MAP[city] = icao

    # 3. Load Airlines CSV (Mapping to 3-letter ICAO, e.g., IGO, AIC)
    al_path = os.path.join(DATA_DIR, "airlines.csv")
    if os.path.exists(al_path):
        with open(al_path, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                icao = row.get('ICAO', '').strip().upper()
                name = row.get('Name', '').strip().lower()
                if icao and len(icao) == 3 and name:
                    AIRLINE_MAP[name.split()[0]] = icao

def resolve_airport(text):
    """Converts extracted text to ICAO Airport Code."""
    clean = str(text).strip().lower()
    return AIRPORT_MAP.get(clean, text.upper())

def resolve_callsign(text):
    """Converts extracted text to ICAO Airline Code + Number (e.g., IGO412)."""
    clean = str(text).strip().lower()
    for name, icao in AIRLINE_MAP.items():
        if name in clean:
            num = ''.join(filter(str.isdigit, clean))
            return f"{icao}{num}"
    return clean.upper().replace(" ", "")

load_maps()
'''


import csv
import os
import re

DATA_DIR = "../raga_flight_status/data/"
AIRPORT_MAP = {}
AIRLINE_MAP = {}

MANUAL_ALIASES = {
    "pune": "VAPO", "mumbai": "VABB", "bangalore": "VOBL", "hyderabad": "VOHS",
    "delhi": "VIDP", "chennai": "VOMM", "kolkata": "VECC", "ahmedabad": "VAAH",
    "pune airport": "VAPO", "mumbai airport": "VABB", "delhi airport": "VIDP"
}

def load_maps():
    for name, icao in MANUAL_ALIASES.items():
        AIRPORT_MAP[name.lower()] = icao.upper()
        
    ap_path = os.path.join(DATA_DIR, "airports.csv")
    if os.path.exists(ap_path):
        with open(ap_path, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                city = row.get('Location', '').strip().lower()
                icao = row.get('ICAO', '').strip().upper()
                if icao and city and city not in AIRPORT_MAP:
                    AIRPORT_MAP[city] = icao

    al_path = os.path.join(DATA_DIR, "airlines.csv")
    if os.path.exists(al_path):
        with open(al_path, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                icao = row.get('ICAO', '').strip().upper()
                name = row.get('Name', '').strip().lower()
                if icao and len(icao) == 3 and name:
                    # Map the first distinct word of the airline
                    AIRLINE_MAP[name.split()[0]] = icao

def resolve_airport(text):
    clean = str(text).strip().lower()
    return AIRPORT_MAP.get(clean, text.upper())

def resolve_callsign(text):
    clean = str(text).strip().lower()
    
    # Check for IATA/ICAO format first (e.g., IGO123, AI101)
    # If it's already a solid block of letters and numbers with no spaces, leave it alone
    if re.match(r'^[a-z]{2,3}\d{1,4}$', clean):
        return text.upper().replace(" ", "")

    # Use Regex Word Boundaries to prevent substring corruption
    for name, icao in AIRLINE_MAP.items():
        if re.search(r'\b' + re.escape(name) + r'\b', clean):
            num = ''.join(filter(str.isdigit, clean))
            return f"{icao}{num}"
            
    return clean.upper().replace(" ", "")

load_maps()