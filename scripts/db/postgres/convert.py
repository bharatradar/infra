#!/usr/bin/env python3
"""
Convert db_reset.py to SQL seed files
Handles duplicates, tuple-style lat/lon, and multi-line runway entries
"""
import re

with open('/Users/Shared/flight_radar/db_reset.py') as f:
    content = f.read()

# === PROCESS ALL AIRPORTS with tuple handling ===
target_section = content[content.find('TARGET_AIRPORTS'):content.find('RUNWAY_DATA')]
new_section = content[content.find('# --- New Data'):content.find('RUNWAY_DATA')]
full_section = target_section + '\n' + new_section

unique_airports = {}
for line in full_section.split('\n'):
    if '# --- New Data' in line:
        continue
    
    match = re.match(r'\s+"([A-Z]{4})":\s+\{"iata":\s+"([^"]+)",.*?"city":\s+(\([^)]+\)|"[^"]+")', line)
    if match:
        icao = match.group(1)
        if icao in unique_airports:
            continue
        iata = match.group(2)
        city_raw = match.group(3)
        city = city_raw.strip('()') if city_raw.startswith('(') else city_raw.strip('"')
        
        name = re.search(r'"name":\s+"([^"]+)"', line)
        state = re.search(r'"state":\s+"([^"]+)"', line)
        region = re.search(r'"region":\s+"([^"]+)"', line)
        lat = re.search(r'"lat":\s+([0-9.]+)', line)
        lon = re.search(r'"lon":\s+([0-9.]+)', line)
        elev = re.search(r'"elev":\s+([0-9.]+)', line)
        type_ = re.search(r'"type":\s+"([^"]+)"', line)
        tz = re.search(r'"timezone":\s+"([^"]+)"', line)
        metro = re.search(r'"metro_connected":\s+(\w+)', line)
        
        if all([iata, name, city, state, region, lat, lon, elev, type_, tz]):
            unique_airports[icao] = {
                'iata': iata, 'name': name.group(1), 'city': city.strip('"'),
                'state': state.group(1), 'region': region.group(1),
                'lat': lat.group(1), 'lon': lon.group(1), 'elev': elev.group(1),
                'type': type_.group(1), 'timezone': tz.group(1),
                'metro': metro.group(1) == 'True' if metro else False
            }

print(f"Airports: {len(unique_airports)}")

# === RUNWAYS ===
runway_section = content[content.find('RUNWAY_DATA'):]
runway_airports = set(re.findall(r'"([A-Z]{4})":\s*\{["\n\s]*"mag_var_w"', runway_section))
print(f"Airports with runways: {len(runway_airports)}")

# Add airports from runways that aren't in our airport list
for icao in runway_airports:
    if icao not in unique_airports:
        air_start = full_section.find(f'"{icao}":')
        if air_start > 0:
            air_chunk = full_section[air_start:air_start+500]
            m = re.search(r'"iata":\s+"([^"]+)"', air_chunk)
            if m:
                name = re.search(r'"name":\s+"([^"]+)"', air_chunk)
                city = re.search(r'"city":\s+(?:\(([^)]+)\)|"([^"]+)")', air_chunk)
                state = re.search(r'"state":\s+"([^"]+)"', air_chunk)
                region = re.search(r'"region":\s+"([^"]+)"', air_chunk)
                lat = re.search(r'"lat":\s+([0-9.]+)', air_chunk)
                lon = re.search(r'"lon":\s+([0-9.]+)', air_chunk)
                elev = re.search(r'"elev":\s+([0-9.]+)', air_chunk)
                type_ = re.search(r'"type":\s+"([^"]+)"', air_chunk)
                tz = re.search(r'"timezone":\s+"([^"]+)"', air_chunk)
                metro = re.search(r'"metro_connected":\s+(\w+)', air_chunk)
                
                if all([m, name, city, state, region, lat, lon, elev, type_, tz]):
                    city_val = (city.group(1) or city.group(2) or '').strip('()"')
                    unique_airports[icao] = {
                        'iata': m.group(1), 'name': name.group(1), 'city': city_val,
                        'state': state.group(1), 'region': region.group(1),
                        'lat': lat.group(1), 'lon': lon.group(1), 'elev': elev.group(1),
                        'type': type_.group(1), 'timezone': tz.group(1),
                        'metro': metro.group(1) == 'True' if metro else False
                    }

print(f"Total airports: {len(unique_airports)}")

# Extract all runways
runways = []
for icao in runway_airports:
    start = runway_section.find(f'"{icao}":')
    if start == -1:
        continue
    chunk = runway_section[start:start+1500]
    
    for rm in re.findall(r'\{"id":\s*"([^"]+)",\s*"hdg1":\s*(\d+),\s*"name1":\s*"([^"]+)",\s*"hdg2":\s*(\d+),\s*"name2":\s*"([^"]+)",\s*"lat1":\s*([\d.]+),\s*"lon1":\s*([\d.]+),\s*"lat2":\s*([\d.]+),\s*"lon2":\s*([\d.]+),\s*"width_m":\s*(\d+)\}', chunk):
        runways.append({
            'airport_icao': icao,
            'runway_id': rm[0],
            'hdg1': rm[1], 'name1': rm[2],
            'hdg2': rm[3], 'name2': rm[4],
            'lat1': rm[5], 'lon1': rm[6],
            'lat2': rm[7], 'lon2': rm[8],
            'width_m': rm[9]
        })

print(f"Runways: {len(runways)}")

# Write airports
with open('/Users/Shared/bharatradar/infra/scripts/db/postgres/seed-airports.sql', 'w') as f:
    f.write(f'-- BharatRadar Airport Seed Data\n-- {len(unique_airports)} Indian airports\n\n')
    f.write('INSERT INTO airports (icao, iata, name, city, state, region, lat, lon, elev, type, timezone, hub_for, metro_connected, mag_var_w) VALUES\n')
    vals = []
    for icao, a in unique_airports.items():
        mt = 'true' if a['metro'] else 'false'
        name = a['name'].replace("'", "''")
        city = a['city'].replace("'", "''")
        vals.append(f"('{icao}', '{a['iata']}', E'{name[:60]}', E'{city[:30]}', E'{a['state']}', E'{a['region']}', {a['lat']}, {a['lon']}, {a['elev']}, E'{a['type']}', E'{a['timezone']}', ARRAY[]::TEXT[], {mt}, 0.5)")
    f.write(',\n'.join(vals) + ';\n')

# Write runways
with open('/Users/Shared/bharatradar/infra/scripts/db/postgres/seed-runways.sql', 'w') as f:
    f.write(f'-- BharatRadar Runway Seed Data\n-- {len(runways)} runways\n\n')
    f.write('INSERT INTO runways (airport_icao, runway_id, name1, hdg1, lat1, lon1, name2, hdg2, lat2, lon2, width_m) VALUES\n')
    vals = []
    for r in runways:
        vals.append(f"('{r['airport_icao']}', '{r['runway_id']}', E'{r['name1']}', {r['hdg1']}, {r['lat1']}, {r['lon1']}, E'{r['name2']}', {r['hdg2']}, {r['lat2']}, {r['lon2']}, {r['width_m']})")
    f.write(',\n'.join(vals) + ';\n')

print(f'Written: {len(unique_airports)} airports, {len(runways)} runways')