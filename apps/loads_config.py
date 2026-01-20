"""
loads_config.py - Configuration for load scheduling

All configuration is encapsulated in Python classes.
Edit this file directly to configure devices and settings.

STRICT REQUIREMENT: No pbr.py dependencies - this module must be reusable
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


class ScheduleMode(Enum):
    """
    Schedule mode determines how device runtime is calculated
    
    PERIOD: Run for fixed hours per period during cheapest slots
        - Requires: desired_on_hours, period_hours
        - Optional: min_price_rank, max_price_rank (to filter slot selection)
        - Behavior: Divides day into periods, finds N cheapest slots per period
        - Total runtime = desired_on_hours × (24 / period_hours)
        - Pricing window: 22:00 to 22:00 (matches Estonian electricity market)
        - Example 1: period_hours=24, desired_on_hours=4 → 4 hours total (cheapest 4 from 22:00-22:00)
        - Example 2: period_hours=12, desired_on_hours=2 → 4 hours total (2 cheapest from 22:00-10:00 + 2 from 10:00-22:00)
        - Example 3: period_hours=6, desired_on_hours=1 → 4 hours total (1 cheapest per 6-hour period)
    
    THRESHOLD: Run whenever price is below threshold
        - Requires: max_price_rank
        - Behavior: Runs all slots where price rank <= threshold
        - Runtime varies by day (could be 2h or 20h depending on prices)
        - Example: Run dehumidifier when price is in cheapest 30%
    """
    PERIOD = "period"
    THRESHOLD = "threshold"
    
    @property
    def description(self) -> str:
        """Get human-readable description"""
        descriptions = {
            ScheduleMode.PERIOD: "Fixed hours during cheapest slots",
            ScheduleMode.THRESHOLD: "Variable hours when price below threshold"
        }
        return descriptions[self]


@dataclass
class LoadDevice:
    """Configuration for a single load device"""
    name: str
    entity_id: str
    shelly_ip: str
    estimated_power: int
    scheduling_enabled: bool = True
    schedule_mode: ScheduleMode = ScheduleMode.PERIOD
    desired_on_hours: Optional[int] = None  # Hours to run PER PERIOD (not total per day)
    period_hours: int = 24  # Period length in hours (24, 12, 6, etc.) - day divided into 24/period_hours periods
    min_price_rank: Optional[int] = None
    max_price_rank: Optional[int] = None
    weather_adjustment: bool = False
    heating_curve: float = 0.0  # -4.0 to +8.0, affects weather calculation
    power_factor: float = 0.5  # For heating curve calculation
    inverted_logic: bool = False
    always_on_hours: Optional[str] = None  # "9-11,13-15,20-22" - Always ON during these hours
    always_off_hours: Optional[str] = None  # "6-8,12-14,18-20" - Always OFF during these hours (overrides cheap slots)
    always_on_price: Optional[float] = None  # Price threshold (cents/kWh) - Always ON if price is below this
    
    # Energy Recovery (Comfort Maintenance)
    energy_debt: int = 0  # Minutes of scheduled time lost due to overrides
    max_energy_debt: int = 180  # Maximum debt to accumulate (minutes)
    recovery_window_hours: int = 4  # Try to recover debt within next N hours
    max_recovery_price: Optional[float] = 50.0  # Max price (cents/kWh) for recovery slots
    
    # Runtime state
    scheduled_slots: List[bool] = field(default_factory=lambda: [False] * 96)
    schedule_ids: Dict[str, int] = field(default_factory=dict)
    
    def _parse_hour_ranges(self, hour_string: Optional[str]) -> List[int]:
        """
        Parse hour range string into list of hour numbers
        
        Format: "9-11,13-15,20-22" or "9,13,20"
        Returns: [9, 10, 13, 14, 20, 21] (end hour not included in ranges)
        """
        if not hour_string:
            return []
        
        hours = []
        for range_str in hour_string.split(','):
            range_str = range_str.strip()
            if '-' in range_str:
                start, end = map(int, range_str.split('-'))
                hours.extend(range(start, end))
            else:
                hours.append(int(range_str))
        
        return sorted(set(hours))  # Remove duplicates and sort
    
    def parse_always_on_hours(self) -> List[int]:
        """Parse always_on_hours string into list of hour numbers"""
        return self._parse_hour_ranges(self.always_on_hours)
    
    def parse_always_off_hours(self) -> List[int]:
        """Parse always_off_hours string into list of hour numbers"""
        return self._parse_hour_ranges(self.always_off_hours)


@dataclass
class GlobalConfig:
    """Global settings shared by all devices"""
    network_provider: str = "elektrilevi"  # elektrilevi | imatra | latvia
    electricity_package: str = "vork5"  # Elektrilevi: vork1|vork2|vork4|vork5, Imatra: partn24|partn24pl|partn12|partn12pl, Latvia: pamata1|special1
    country: str = "ee"  # ee | fi | lv | lt
    timezone: str = "Europe/Tallinn"
    latitude: float = 59.431
    longitude: float = 24.743
    schedule_time: str = "21:50"
    run_on_startup: bool = True  # Run calculation immediately when app starts
    shelly_delay_between_deletes: float = 1.0  # Seconds between delete operations
    shelly_delay_between_creates: float = 1.0  # Seconds between schedule creation operations


# =============================================================================
# CONFIGURATION - EDIT BELOW THIS LINE
# =============================================================================

GLOBAL_CONFIG = GlobalConfig(
    network_provider="elektrilevi",
    electricity_package="vork5",  # Change to your package: vork2, vork5, package1, package2
    country="ee",
    timezone="Europe/Tallinn",
    latitude=59.431,  # Overridden by secrets in apps.yaml
    longitude=24.743,  # Overridden by secrets in apps.yaml
    schedule_time="21:50",  # Fixed timezone handling - runs at exact time before 22:00 market window
    run_on_startup=True,  # Run calculation immediately when app starts
    shelly_delay_between_deletes=1.0,  # Delay between delete operations (seconds)
    shelly_delay_between_creates=1.0  # Delay between schedule creations (seconds)
)

DEVICES = [ 
    LoadDevice(
        name="Boiler",
        entity_id="switch.boiler",
        shelly_ip="10.1.107.6",
        estimated_power=2000,
        scheduling_enabled=True,
        schedule_mode=ScheduleMode.PERIOD,
        desired_on_hours=3,  # 2 hours PER PERIOD
        period_hours=24,  # 12-hour periods → 2 periods per day → 4 hours total (2×2)
        min_price_rank=None,
        max_price_rank=None,
        weather_adjustment=False,
        inverted_logic=False,
        always_on_hours=None,  # Always ON during these hours (in addition to period scheduling)
        always_off_hours="22-24",  # Always OFF during these hours (overrides cheap slots)
        always_on_price=7.0  # Always ON if price < always_on_price cents/kWh
    ),
    LoadDevice(
        name="Heating Big",
        entity_id="switch.heating",
        shelly_ip="10.1.107.5",
        estimated_power=6000,
        scheduling_enabled=True,
        schedule_mode=ScheduleMode.PERIOD,  # PERIOD mode for weather adjustment
        desired_on_hours=0,  # Baseline hours (will be adjusted by weather)
        max_price_rank=None,  # Not used in PERIOD mode
        weather_adjustment=True,
        heating_curve=-2.5,  # Adjust -4.0 to +8.0 to change sensitivity
        power_factor=0.55,  # Standard for 24h heating
        inverted_logic=False,
        always_on_hours=None,  # No forced ON hours
        always_off_hours=None,  # No forced OFF hours
        always_on_price=6.5  # Always ON if price < always_on_price cents/kWh
    ),
    # Add more devices as needed
]
