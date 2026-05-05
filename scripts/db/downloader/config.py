#!/usr/bin/env python3
"""
Minimal config for schedule-downloader.
Provides airline mappings and basic configuration.
"""

import os

AIRLINES_FILE = os.environ.get(
    "AIRLINES_FILE", 
    "/opt/bharatradar/flight_radar/data/airlines.csv"
)

MISSING_AIRPORTS_IN_AVIONIO = {
    'VEAY': 'AYJ', 'VEDO': 'DGH', 'VIJW': 'DXN', 'VIDX': 'HDO',
    'VEHO': 'HGI', 'VIAH': 'HRH', 'VAHS': 'HSR', 'VOKU': 'KJB',
    'VANM': 'NMI', 'VARW': 'REW', 'VOSR': 'SDW'
}