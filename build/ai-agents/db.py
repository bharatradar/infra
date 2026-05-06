# db.py
# AI Agents - Database operations
import os
import logging
import asyncpg
import re
from datetime import datetime
from config import Config

logger = logging.getLogger(__name__)

class AsyncDatabaseManager:
    def __init__(self, pool):
        self.pool = pool
        self._iata_to_icao_map = None 
        self._icao_to_iata_airline_map = None 

    async def _resolve_icao(self, code):
        if not code: 
            return code
        code = str(code).strip().upper()
        if code == 'UNK':
            return None  
        if len(code) == 3:
            for icao, data in getattr(Config, 'TARGET_AIRPORTS', {}).items():
                if data.get('iata', '').upper() == code:
                    return icao
            if self._iata_to_icao_map is None:
                self._iata_to_icao_map = await self._load_iata_to_icao_map()
            return self._iata_to_icao_map.get(code, code)
        return code

    async def _load_iata_to_icao_map(self):
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT iata, icao FROM airports WHERE iata IS NOT NULL")
                return {r['iata'].upper(): r['icao'].upper() for r in rows}
        except Exception as e:
            logger.error(f"Failed to load IATA-ICAO map: {e}")
            return {}

    async def update_arrival_broadcast(self, row_id, hex_id, origin, anomaly_flag=None, original_value=None, ai_reasoning=None, confidence_score=1.0, callsign=None):
        try:
            hex_id = hex_id.upper() if hex_id else None
            callsign = callsign.upper() if callsign else None
            origin = await self._resolve_icao(origin)
            
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if anomaly_flag:
                        await conn.execute("UPDATE arrivals_log SET origin = $1, anomaly_flag = $3 WHERE id = $2", origin, row_id, anomaly_flag)
                        await conn.execute("UPDATE ground_ops SET origin = $1 WHERE hex_id = $2", origin, hex_id)
                        await conn.execute("UPDATE flight_events SET origin = $1, anomaly_flag = $3 WHERE hex_id = $2 AND event_type='LANDED'", origin, hex_id, anomaly_flag)
                        
                        if anomaly_flag == 'AI_ENRICHED':
                            await conn.execute("""
                                INSERT INTO ai_enrichment_audit 
                                (target_table, record_id, hex_id, callsign, original_value, ai_inferred_value, ai_reasoning, confidence_score)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            """, 'arrivals_log', row_id, hex_id, callsign, original_value, origin, ai_reasoning, confidence_score)
                    else:
                        await conn.execute("UPDATE arrivals_log SET origin = $1 WHERE id = $2", origin, row_id)
                        await conn.execute("UPDATE ground_ops SET origin = $1 WHERE hex_id = $2", origin, hex_id)
                        await conn.execute("UPDATE flight_events SET origin = $1 WHERE hex_id = $2 AND event_type='LANDED'", origin, hex_id)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] update_arrival_broadcast failed: {e}")

    async def log_ai_insight(self, insight_type, trigger_event, insight_text, target_airport=None):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ai_insights_log 
                    (insight_type, trigger_event, insight_text, target_airport)
                    VALUES ($1, $2, $3, $4)
                """, insight_type, trigger_event, insight_text, target_airport)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] log_ai_insight failed: {e}")