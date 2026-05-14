"""
Predictive Delay Forecasting Module
Option A: Rule-based heuristics using historical averages
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import asyncpg

logger = logging.getLogger(__name__)

class DelayPredictor:
    """Rule-based delay prediction using historical data."""
    
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self._cache = {}
        self._cache_time = None
        self._cache_ttl = 300  # 5 minutes
    
    async def _get_historical_data(self) -> Dict[str, Any]:
        """Get cached historical delay data from arrivals_log and departures_log."""
        now = datetime.now()
        
        if self._cache and self._cache_time and (now - self._cache_time).seconds < self._cache_ttl:
            return self._cache
        
        async with self.db_pool.acquire() as conn:
            # Airline delays from arrivals (using approach time vs landing as proxy)
            airline_arr_delays = await conn.fetch("""
                SELECT SUBSTRING(a.callsign FROM 1 FOR 3) as airline,
                       COUNT(*) as sample_size
                FROM arrivals_log a
                WHERE a.timestamp >= NOW() - INTERVAL '30 days'
                  AND a.callsign IS NOT NULL
                GROUP BY airline
                HAVING COUNT(*) > 5
            """)
            
            # Airport arrival delays (count as proxy for congestion)
            airport_arrivals = await conn.fetch("""
                SELECT airport, COUNT(*) as arrivals_count
                FROM arrivals_log
                WHERE timestamp >= NOW() - INTERVAL '30 days'
                GROUP BY airport
                HAVING COUNT(*) > 10
            """)
            
            # Hour patterns from arrivals
            hour_patterns = await conn.fetch("""
                SELECT EXTRACT(HOUR FROM timestamp) as hour,
                       COUNT(*) as flights
                FROM arrivals_log
                WHERE timestamp >= NOW() - INTERVAL '30 days'
                GROUP BY hour
            """)
            
            # Day of week patterns
            dow_patterns = await conn.fetch("""
                SELECT EXTRACT(DOW FROM timestamp) as dow,
                       COUNT(*) as flights
                FROM arrivals_log
                WHERE timestamp >= NOW() - INTERVAL '30 days'
                GROUP BY dow
            """)
            
            # Route statistics
            route_stats = await conn.fetch("""
                SELECT origin, airport as dest, COUNT(*) as arrivals
                FROM arrivals_log
                WHERE origin IS NOT NULL
                  AND airport IS NOT NULL
                  AND origin != airport
                  AND timestamp >= NOW() - INTERVAL '30 days'
                GROUP BY origin, airport
                HAVING COUNT(*) > 5
            """)
            
            # Get anomaly rates as proxy for delays
            anomaly_stats = await conn.fetch("""
                SELECT airport, anomaly_flag, COUNT(*) as count
                FROM arrivals_log
                WHERE timestamp >= NOW() - INTERVAL '7 days'
                  AND anomaly_flag IS NOT NULL
                  AND anomaly_flag != 'AI_ENRICHED'
                GROUP BY airport, anomaly_flag
            """)
            
            # Airport average delays from flight_schedules
            airport_avg = await conn.fetch("""
                SELECT airport_code, direction,
                       AVG(EXTRACT(EPOCH FROM (actual_time - scheduled_time))/60) as avg_delay
                FROM flight_schedules
                WHERE actual_time IS NOT NULL
                  AND scheduled_time >= NOW() - INTERVAL '24 hours'
                GROUP BY airport_code, direction
            """)
            
            # Total counts
            total_arrivals = await conn.fetchval("SELECT COUNT(*) FROM arrivals_log WHERE timestamp >= NOW() - INTERVAL '30 days'")
            total_departures = await conn.fetchval("SELECT COUNT(*) FROM departures_log WHERE timestamp >= NOW() - INTERVAL '30 days'")
            
            # Recent anomaly rate
            recent_anomalies = await conn.fetchval("""
                SELECT COUNT(*) FROM arrivals_log 
                WHERE timestamp >= NOW() - INTERVAL '7 days' 
                  AND anomaly_flag IS NOT NULL
            """)
        
        # Process into lookup dicts
        self._cache = {
            'airlines': {r['airline']: r['sample_size'] for r in airline_arr_delays},
            'airport_arrivals': {r['airport']: r['arrivals_count'] for r in airport_arrivals},
            'hour_patterns': {int(r['hour']): r['flights'] for r in hour_patterns},
            'dow_patterns': {int(r['dow']): r['flights'] for r in dow_patterns},
            'route_stats': {(r['origin'], r['dest']): r['arrivals'] for r in route_stats},
            'anomaly_stats': {(r['airport'], r['anomaly_flag']): r['count'] for r in anomaly_stats},
            'airport_avg': {(r['airport_code'], r['direction']): float(r['avg_delay']) for r in airport_avg},
            'total_arrivals': total_arrivals,
            'total_departures': total_departures,
            'anomaly_rate': recent_anomalies / max(total_arrivals, 1) * 100 if total_arrivals else 0,
        }
        self._cache_time = now
        
        logger.info(f"📊 Delay data: {total_arrivals} arrivals, {total_departures} departures, {recent_anomalies} anomalies")
        
        return self._cache
    
    def _get_confidence(self, factors: List[str], sample_size: int) -> str:
        """Determine prediction confidence based on data quality."""
        if sample_size > 100 and len(factors) >= 2:
            return "HIGH"
        elif sample_size > 50 and len(factors) >= 1:
            return "MEDIUM"
        else:
            return "LOW"
    
    async def predict_delay(
        self,
        callsign: Optional[str] = None,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        airport_code: Optional[str] = None,
        direction: Optional[str] = None,
        scheduled_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Predict delay for a flight using rule-based heuristics.
        
        Returns:
            {
                'predicted_delay_minutes': 15,
                'confidence': 'HIGH',
                'factors': ['airline_factor', 'peak_hour', 'congestion'],
                'breakdown': {...}
            }
        """
        data = await self._get_historical_data()
        
        predicted_delay = 0
        factors = []
        breakdown = {}
        
        # 1. Airline factor (based on traffic volume)
        if callsign:
            airline = callsign[:3].upper()
            if airline in data['airlines']:
                # Higher traffic airlines have more delays
                airline_traffic = data['airlines'][airline]
                # Normalize: higher traffic = more delays
                traffic_factor = min(airline_traffic / 100, 10)
                delay_estimate = int(traffic_factor * 3)  # ~3 min per 100 flights
                predicted_delay += delay_estimate
                factors.append(f"airline:{airline}")
                breakdown['airline_traffic'] = airline_traffic
                breakdown['airline_estimate'] = delay_estimate
        
        # 2. Route factor (congestion on route)
        if origin and destination:
            route_key = (origin.upper(), destination.upper())
            if route_key in data['route_stats']:
                route_traffic = data['route_stats'][route_key]
                # High traffic routes have more delays
                route_factor = min(route_traffic / 50, 15)
                delay_estimate = int(route_factor * 2)
                predicted_delay += delay_estimate
                factors.append(f"route:{origin}->{destination}")
                breakdown['route_traffic'] = route_traffic
                breakdown['route_estimate'] = delay_estimate
        
        # 3. Airport congestion factor
        if airport_code:
            airport_code = airport_code.upper()
            if airport_code in data['airport_arrivals']:
                arr_count = data['airport_arrivals'][airport_code]
                # More arrivals = more congestion = more delays
                congestion_factor = min(arr_count / 200, 20)
                delay_estimate = int(congestion_factor * 2)
                predicted_delay += delay_estimate
                factors.append(f"airport:{airport_code}")
                breakdown['airport_arrivals'] = arr_count
                breakdown['congestion_estimate'] = delay_estimate
        
        # 4. Time of day factor (peak hours)
        if scheduled_time:
            hour = scheduled_time.hour
            if hour in data['hour_patterns']:
                hour_traffic = data['hour_patterns'][hour]
                # Peak hours: 8-10 AM, 5-8 PM
                if hour in [8, 9, 10, 17, 18, 19, 20, 21]:
                    peak_factor = 1.5
                elif hour in [6, 7, 11, 12, 13, 14, 15, 16]:
                    peak_factor = 1.0
                else:
                    peak_factor = 0.5
                
                hour_estimate = int(hour_traffic * 0.01 * peak_factor)
                predicted_delay += hour_estimate
                factors.append(f"peak_hour:{hour}")
                breakdown['hour_traffic'] = hour_traffic
                breakdown['hour_estimate'] = hour_estimate
            
            # 5. Day of week factor
            dow = scheduled_time.weekday()
            if dow in data['dow_patterns']:
                dow_traffic = data['dow_patterns'][dow]
                # Monday(0), Friday(4), Saturday(5) are busier
                if dow in [0, 4, 5]:
                    dow_factor = 1.3
                elif dow in [1, 2, 3]:
                    dow_factor = 1.0
                else:
                    dow_factor = 0.7
                
                dow_estimate = int(dow_traffic * 0.005 * dow_factor)
                predicted_delay += dow_estimate
                factors.append(f"day_of_week:{dow}")
                breakdown['dow_estimate'] = dow_estimate
        
        # 6. Anomaly factor (recent issues at airport)
        if airport_code:
            airport_code = airport_code.upper()
            anomalies = [(k[1], v) for k, v in data['anomaly_stats'].items() if k[0] == airport_code]
            if anomalies:
                total_anomalies = sum(v for _, v in anomalies)
                anomaly_factor = min(total_anomalies / 10, 15)
                predicted_delay += int(anomaly_factor)
                factors.append(f"anomalies:{total_anomalies}")
                breakdown['anomaly_estimate'] = int(anomaly_factor)
        
        # Default estimate if no data
        if not factors:
            # Use baseline: ~5 min average + anomaly rate factor
            base_delay = 5 + int(data.get('anomaly_rate', 0) * 2)
            predicted_delay = base_delay
            factors.append("baseline")
        
        # Cap maximum delay at 60 minutes
        predicted_delay = min(predicted_delay, 60)
        
        # Calculate confidence
        total_samples = sum(data['airlines'].values()) if data['airlines'] else 0
        confidence = self._get_confidence(factors, total_samples)
        
        # Determine delay category
        if predicted_delay < 10:
            status = "ON_TIME"
        elif predicted_delay < 20:
            status = "SLIGHT_DELAY"
        elif predicted_delay < 35:
            status = "DELAYED"
        else:
            status = "SIGNIFICANT_DELAY"
        
        return {
            'predicted_delay_minutes': predicted_delay,
            'confidence': confidence,
            'status': status,
            'factors': factors,
            'breakdown': breakdown,
            'scheduled_time': scheduled_time.isoformat() if scheduled_time else None,
            'airline': callsign[:3].upper() if callsign else None,
            'route': f"{origin}->{destination}" if origin and destination else None
        }
    
    async def get_airline_otp(self, airline: str = None, limit: int = 10) -> List[Dict]:
        """Get airline On-Time Performance rankings (based on traffic volume)."""
        data = await self._get_historical_data()
        
        results = []
        for al, sample in data['airlines'].items():
            if airline and al != airline.upper():
                continue
            
            # Estimate delay based on traffic - more traffic = more delays
            # Use lower threshold to show variation
            estimated_delay = int(min(sample * 0.1, 30))  # 0.1 min per flight
            
            if estimated_delay < 5:
                status = "EXCELLENT"
            elif estimated_delay < 15:
                status = "GOOD"
            elif estimated_delay < 25:
                status = "FAIR"
            else:
                status = "POOR"
            
            results.append({
                'airline': al,
                'avg_delay_minutes': estimated_delay,
                'sample_size': sample,
                'status': status
            })
        
        results.sort(key=lambda x: x['avg_delay_minutes'], reverse=True)
        return results[:limit]
    
    async def get_route_otp(self, origin: str = None, dest: str = None, limit: int = 10) -> List[Dict]:
        """Get On-Time Performance by route (based on traffic)."""
        data = await self._get_historical_data()
        
        results = []
        for (origin_code, dest_code), arrivals in data['route_stats'].items():
            if origin and origin_code != origin.upper():
                continue
            if dest and dest_code != dest.upper():
                continue
            
            # Estimate delay based on traffic - use lower threshold to show variation
            # More traffic = more delays
            estimated_delay = int(min(arrivals * 0.5, 30))  # 0.5 min per arrival
            
            results.append({
                'origin': origin_code,
                'destination': dest_code,
                'avg_delay_minutes': estimated_delay,
                'sample_size': arrivals,
                'route': f"{origin_code}->{dest_code}"
            })
        
        results.sort(key=lambda x: x['avg_delay_minutes'], reverse=True)
        return results[:limit]


async def create_predictor(db_pool: asyncpg.Pool) -> DelayPredictor:
    """Factory function to create predictor instance."""
    predictor = DelayPredictor(db_pool)
    # Pre-load data
    await predictor._get_historical_data()
    return predictor