"""
Weather forecast integration for load scheduling

Fetches temperature forecasts from Open-Meteo API and calculates
required heating slots based on heating curve algorithm (ported from Shelly).

This module is standalone and reusable - no pbr.py dependencies.
"""

import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class WeatherForecast:
    """Weather forecast data for a specific period"""
    timestamp: datetime
    temperature: float  # "Feels like" temperature in °C
    avg_temperature: float  # Average for the period
    period_hours: int  # How many hours this forecast covers


class LoadsWeatherManager:
    """
    Manages weather forecast fetching and heating curve calculations
    
    No pbr.py dependencies - standalone reusable module
    """
    
    def __init__(self, latitude: float, longitude: float, timezone: str = "Europe/Tallinn"):
        """
        Initialize weather manager
        
        Args:
            latitude: Location latitude for weather API
            longitude: Location longitude for weather API
            timezone: Timezone for forecast times
        """
        self.latitude = latitude
        self.longitude = longitude
        self.timezone = timezone
        self._cache: Optional[Dict] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl_hours = 1  # Cache for 1 hour
        
    def fetch_forecast(self, hours: int = 24) -> Optional[WeatherForecast]:
        """
        Fetch temperature forecast from Open-Meteo API
        
        Args:
            hours: Number of hours to forecast (6, 12, or 24)
            
        Returns:
            WeatherForecast object or None if fetch fails
        """
        # Check cache
        if self._is_cache_valid():
            return self._get_cached_forecast(hours)
        
        try:
            # Open-Meteo API endpoint
            url = "https://api.open-meteo.com/v1/forecast"
            
            params = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "hourly": "apparent_temperature",  # "Feels like" temperature
                "timezone": self.timezone,
                "forecast_hours": hours
            }
            
            # Log forecast query
            query_url = f"{url}?hourly=apparent_temperature&timezone={self.timezone}&forecast_hours={hours}&latitude={self.latitude}&longitude={self.longitude}"
            print(f"Forecast query: {query_url}")
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # Parse response
            if "hourly" not in data or "apparent_temperature" not in data["hourly"]:
                return None
            
            temps = data["hourly"]["apparent_temperature"]
            times = data["hourly"]["time"]
            
            if not temps or len(temps) == 0:
                return None
            
            # Calculate average temperature for the period
            avg_temp = sum(temps) / len(temps)
            
            # Log forecast result
            print(f"We got weather forecast from Open-Meteo at {datetime.now()}")
            
            # Store in cache
            self._cache = {
                "temps": temps,
                "times": times,
                "avg_temp": avg_temp
            }
            self._cache_time = datetime.now()
            
            return WeatherForecast(
                timestamp=datetime.now(),
                temperature=temps[0],  # Current "feels like"
                avg_temperature=avg_temp,
                period_hours=hours
            )
            
        except Exception as e:
            # Log error but don't crash - caller will handle fallback
            print(f"Weather API fetch failed: {e}")
            return None
    
    def calculate_heating_slots(
        self,
        forecast_temp: float,
        heating_curve: int = 0,
        power_factor: float = 0.5,
        period_hours: int = 24,
        max_temp: float = 16.0
    ) -> int:
        """
        Calculate required heating slots based on temperature and heating curve
        
        This is the Shelly algorithm ported to Python:
        heating_time = ((maxT - tForecast) * (powerFactor - 1) + 
                       (maxT - tForecast + heatingCurve * 2 - 2))
        
        Args:
            forecast_temp: Forecast "feels like" temperature in °C
            heating_curve: Heating curve adjustment (-4 to +8)
            power_factor: Power factor (default 0.5)
            period_hours: Period to calculate for (6, 12, or 24)
            max_temp: Maximum temperature threshold (default 16°C)
            
        Returns:
            Number of 15-minute slots needed (0 if temp >= max_temp)
        """
        # If warmer than threshold, no heating needed
        if forecast_temp >= max_temp:
            return 0
        
        # Shelly heating curve algorithm
        temp_diff = max_temp - forecast_temp
        heating_time = (
            (temp_diff * (power_factor - 1)) + 
            (temp_diff + heating_curve * 2 - 2)
        )
        
        # Can't be negative
        heating_time = max(0, heating_time)
        
        # Divide by number of periods (if using 6h or 12h periods)
        if period_hours < 24:
            num_periods = 24 // period_hours
            heating_time = heating_time / num_periods
        
        # Convert hours to 15-minute slots (4 slots per hour)
        heating_slots = int(heating_time * 4)
        
        # Ensure we don't exceed the period
        max_slots = period_hours * 4
        heating_slots = min(heating_slots, max_slots)
        
        return heating_slots
    
    def get_heating_requirement(
        self,
        heating_curve: int = 0,
        power_factor: float = 0.5,
        period_hours: int = 24,
        min_slots: int = 0
    ) -> int:
        """
        Get heating slots required based on current forecast
        
        Args:
            heating_curve: Heating curve adjustment (-4 to +8)
            power_factor: Power factor (default 0.5)
            period_hours: Period to calculate for (6, 12, or 24)
            min_slots: Minimum slots to return (from config)
            
        Returns:
            Number of 15-minute slots needed, or min_slots if forecast fails
        """
        # Fetch forecast
        forecast = self.fetch_forecast(hours=period_hours)
        
        if forecast is None:
            # Fallback to minimum from config
            return min_slots
        
        # Calculate required slots
        required_slots = self.calculate_heating_slots(
            forecast_temp=forecast.avg_temperature,
            heating_curve=heating_curve,
            power_factor=power_factor,
            period_hours=period_hours
        )
        
        # Use at least the configured minimum
        return max(required_slots, min_slots)
    
    def _is_cache_valid(self) -> bool:
        """Check if cached forecast is still valid"""
        if self._cache is None or self._cache_time is None:
            return False
        
        age = datetime.now() - self._cache_time
        return age < timedelta(hours=self._cache_ttl_hours)
    
    def _get_cached_forecast(self, hours: int) -> Optional[WeatherForecast]:
        """Get forecast from cache if available"""
        if not self._is_cache_valid():
            return None
        
        return WeatherForecast(
            timestamp=self._cache_time,
            temperature=self._cache["temps"][0],
            avg_temperature=self._cache["avg_temp"],
            period_hours=hours
        )
