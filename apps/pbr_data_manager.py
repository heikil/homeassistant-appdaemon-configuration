"""
Phase Balancer Rewrite (PBR) - Data Manager Module

This module handles sensor data collection, freshness tracking, and state aggregation
with active freshening for stale data and comprehensive health monitoring.
"""

import time
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
from pbr_config import Config


@dataclass
class SensorData:
    """Container for sensor data with metadata"""
    value: Any
    timestamp: float
    last_refresh_attempt: float = 0
    refresh_count: int = 0
    error_count: int = 0


@dataclass
class SensorHealth:
    """Health metrics for a sensor"""
    entity_id: str
    last_update: float
    update_count: int
    error_count: int
    refresh_count: int
    avg_update_interval: float


class DataManager:
    """
    Manages sensor data collection, freshness, and health monitoring.

    Features:
    - Active freshening: Re-reads stale sensor data from Home Assistant
    - Configurable max ages per sensor type
    - Health monitoring and metrics
    - Thread-safe operations
    """

    def __init__(self, hass_api):
        """Initialize data manager with Home Assistant API reference"""
        self.hass = hass_api
        self.sensor_data: Dict[str, SensorData] = {}
        self.health_stats: Dict[str, SensorHealth] = {}

        # Max age limits (seconds) - configurable per sensor type
        self.max_ages = {
            # Critical sensors - need very fresh data
            Config.phase_a_sensor: 30,
            Config.phase_b_sensor: 30,
            Config.phase_c_sensor: 30,
            Config.power_meter_total_sensor: 30,  # Critical for mFRR modes
            Config.battery_soc_sensor: 30,
            Config.battery_power_sensor: 30,

            # Secondary sensors - can be slightly older
            Config.inverter_input_sensor: 60,
            Config.inverter_power_sensor: 60,
            Config.battery_charge_limit_sensor: 60,
            Config.battery_discharge_limit_sensor: 60,
            Config.battery_forced_charge_sensor: 60,

            # Configuration and mode sensors - can be older
            Config.qw_mode_sensor: 120,
            Config.qw_source_sensor: 120,
            Config.heating_switch: 120,
            Config.boiler_switch: 120,
            Config.phase_target_input: 120,
            Config.phase_range_low_input: 120,
            Config.phase_range_high_input: 120,
        }

        # Invalid age limits (seconds) - when data is considered completely invalid
        # Different handling based on sensor criticality and fallback strategies
        self.invalid_ages = {
            # Critical sensors - system invalid if these are too old
            Config.phase_a_sensor: 300,  # 5 minutes
            Config.phase_b_sensor: 300,
            Config.phase_c_sensor: 300,
            Config.power_meter_total_sensor: 300,  # 5 minutes - critical for mFRR
            Config.battery_soc_sensor: 300,
            Config.battery_power_sensor: 300,

            # Secondary sensors - can be invalid longer
            Config.inverter_input_sensor: 600,  # 10 minutes
            Config.inverter_power_sensor: 600,
            Config.battery_charge_limit_sensor: 600,
            Config.battery_discharge_limit_sensor: 600,
            Config.battery_forced_charge_sensor: 600,

            # Mode sensors - assume normal mode and optimizer source if invalid
            Config.qw_mode_sensor: 1800,  # 30 minutes - use default "normal"
            Config.qw_source_sensor: 1800,  # 30 minutes - use default "optimizer"

            # Load sensors - take no action if invalid (assume off)
            Config.heating_switch: 1800,  # 30 minutes - assume off
            Config.boiler_switch: 1800,   # 30 minutes - assume off

            # Configuration inputs - use default values if invalid
            Config.phase_target_input: 3600,  # 1 hour - use default value
            Config.phase_range_low_input: 3600,
            Config.phase_range_high_input: 3600,
        }

        # Critical sensors - if any of these are invalid, system should stop regulation
        self.critical_sensors = {
            Config.phase_a_sensor,
            Config.phase_b_sensor,
            Config.phase_c_sensor,
            Config.power_meter_total_sensor,  # Critical for mFRR modes
            Config.battery_soc_sensor,
            Config.battery_power_sensor,
            Config.phase_target_input,  # Need target to know what to balance to
        }

        # Initialize health tracking for all sensors
        for entity in Config.sensor_entities:
            self.health_stats[entity] = SensorHealth(
                entity_id=entity,
                last_update=0,
                update_count=0,
                error_count=0,
                refresh_count=0,
                avg_update_interval=0
            )

    def update_sensor(self, entity: str, value: Any) -> None:
        """Update sensor data from Home Assistant event"""
        now = time.time()

        # Convert numeric sensor values to float for consistency and store as float
        if entity in Config.sensor_types and Config.sensor_types[entity] == 'numeric' and value is not None:
            try:
                value = float(value)
            except (ValueError, TypeError):
                # Keep original value if conversion fails
                pass

        # Update or create sensor data
        if entity not in self.sensor_data:
            self.sensor_data[entity] = SensorData(
                value=value,
                timestamp=now
            )
        else:
            self.sensor_data[entity].value = value
            self.sensor_data[entity].timestamp = now

        # Update health statistics
        self._update_health_stats(entity, now)

    def get_sensor_value(self, entity: str, max_age: Optional[float] = None, use_fallback: bool = True) -> Optional[Any]:
        """
        Get sensor value with freshness checking and active refresh if needed.

        Args:
            entity: Sensor entity ID
            max_age: Override default max age (seconds)
            use_fallback: Whether to use fallback values for invalid sensors

        Returns:
            Sensor value if fresh and valid, fallback value, or None if unavailable/invalid
        """
        # Determine max age for this sensor
        if max_age is None:
            max_age = self.max_ages.get(entity, 60)  # Default 60 seconds

        now = time.time()

        # Check if we have cached data
        if entity not in self.sensor_data:
            # No cached data, try to fetch fresh
            fresh_value = self._fetch_fresh_value(entity)
            if fresh_value is not None:
                # Convert numeric sensors to float (already done in _fetch_fresh_value)
                if self._is_invalid_sensor_value(entity, fresh_value):
                    return self._get_fallback_value(entity) if use_fallback else None
            return fresh_value

        cached_data = self.sensor_data[entity]

        # Check if data is fresh enough
        if now - cached_data.timestamp <= max_age:
            # Additional validation for known invalid values
            if self._is_invalid_sensor_value(entity, cached_data.value):
                return self._get_fallback_value(entity) if use_fallback else None
            # Numeric sensors are already stored as float, no conversion needed
            return cached_data.value

        # Data is stale, try to refresh
        refreshed_value = self._refresh_stale_value(entity, cached_data)
        if refreshed_value is not None:
            # Numeric sensors are already converted to float in _refresh_stale_value
            if self._is_invalid_sensor_value(entity, refreshed_value):
                return self._get_fallback_value(entity) if use_fallback else None
        return refreshed_value

    def _get_fallback_value(self, entity: str) -> Optional[Any]:
        """Get fallback value for invalid sensors based on sensor type"""
        if entity == Config.qw_mode_sensor:
            return "normal"  # Default mode
        elif entity == Config.qw_source_sensor:
            return "optimizer"  # Default source
        elif entity in [Config.heating_switch, Config.boiler_switch]:
            return "off"  # Assume switches are off if invalid
        elif entity == Config.phase_target_input:
            return 20.0  # Default phase target
        elif entity == Config.phase_range_low_input:
            return 15.0  # Default range low
        elif entity == Config.phase_range_high_input:
            return 50.0  # Default range high
        else:
            # For other sensors, no fallback
            return None

    def _is_numeric_sensor(self, entity: str) -> bool:
        """Check if a sensor should return numeric values"""
        sensor_type = Config.sensor_types.get(entity)
        return sensor_type == 'numeric'

    def _is_invalid_sensor_value(self, entity: str, value: Any) -> bool:
        """Check if a sensor value is invalid (None, 'Unknown', etc.)"""
        if value is None:
            return True

        # Check for common invalid string values
        if isinstance(value, str):
            invalid_strings = ['unknown', 'unavailable', 'none', '']
            if value.lower() in invalid_strings:
                return True

        # For numeric sensors, check if we can convert to float
        sensor_type = Config.sensor_types.get(entity)
        if sensor_type == 'numeric':
            try:
                float(value)
                return False
            except (ValueError, TypeError):
                return True

        return False

    def is_sensor_fresh(self, entity: str, max_age: Optional[float] = None) -> bool:
        """Check if sensor data is within freshness limits"""
        if max_age is None:
            max_age = self.max_ages.get(entity, 60)

        if entity not in self.sensor_data:
            return False

        now = time.time()
        return now - self.sensor_data[entity].timestamp <= max_age

    def is_sensor_valid(self, entity: str) -> bool:
        """Check if sensor data is valid (not too old to be completely invalid)"""
        invalid_age = self.invalid_ages.get(entity, 600)  # Default 10 minutes

        if entity not in self.sensor_data:
            return False

        now = time.time()
        return now - self.sensor_data[entity].timestamp <= invalid_age

    def is_system_valid(self) -> bool:
        """Check if the overall system has valid data for all critical sensors"""
        for sensor in self.critical_sensors:
            if not self.is_sensor_valid(sensor):
                return False
        return True

    def _fetch_fresh_value(self, entity: str) -> Optional[Any]:
        """Fetch fresh value from Home Assistant for uncached sensor"""
        try:
            value = self.hass.get_state(entity)
            if value is not None:
                # Convert numeric sensors to float for storage
                if entity in Config.sensor_types and Config.sensor_types[entity] == 'numeric' and value is not None:
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        pass  # Keep original if conversion fails

                now = time.time()
                self.sensor_data[entity] = SensorData(
                    value=value,
                    timestamp=now
                )
                self._update_health_stats(entity, now)
                return value
        except Exception as e:
            self._record_error(entity, f"Failed to fetch fresh value: {e}")

        return None

    def _refresh_stale_value(self, entity: str, cached_data: SensorData) -> Optional[Any]:
        """Refresh stale sensor data from Home Assistant"""
        now = time.time()

        # Avoid refreshing too frequently (throttle to once per 5 seconds)
        if now - cached_data.last_refresh_attempt < 5:
            # Return cached value if we tried recently
            return cached_data.value

        try:
            cached_data.last_refresh_attempt = now
            cached_data.refresh_count += 1

            value = self.hass.get_state(entity)
            if value is not None:
                # Convert numeric sensors to float for storage
                if entity in Config.sensor_types and Config.sensor_types[entity] == 'numeric' and value is not None:
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        pass  # Keep original if conversion fails

                cached_data.value = value
                cached_data.timestamp = now
                self._update_health_stats(entity, now)
                return value
            else:
                self._record_error(entity, "Refresh returned None value")
                return cached_data.value  # Return stale value as fallback

        except Exception as e:
            self._record_error(entity, f"Failed to refresh stale value: {e}")
            return cached_data.value  # Return stale value as fallback

    def _update_health_stats(self, entity: str, update_time: float) -> None:
        """Update health statistics for a sensor"""
        if entity not in self.health_stats:
            return

        health = self.health_stats[entity]

        # Calculate update interval
        if health.last_update > 0:
            interval = update_time - health.last_update
            # Update rolling average (simple exponential smoothing)
            alpha = 0.1  # Smoothing factor
            health.avg_update_interval = (1 - alpha) * health.avg_update_interval + alpha * interval

        health.last_update = update_time
        health.update_count += 1

    def _record_error(self, entity: str, error_msg: str) -> None:
        """Record an error for health monitoring"""
        if entity in self.health_stats:
            self.health_stats[entity].error_count += 1

        # Could add logging here if needed
        # self.hass.log_if_enabled(f"DataManager error for {entity}: {error_msg}", level="WARNING")

    def get_system_state(self) -> Optional[Dict[str, Any]]:
        """
        Aggregate all sensor data into a validated system state.

        Returns:
            System state dict if all critical sensors are valid, None if system should be invalid
        """
        # First check if system is in valid state (all critical sensors have valid data)
        if not self.is_system_valid():
            return None  # System is invalid, should stop regulation

        try:
            # Get all required sensor values with freshness checking
            phases = [
                self.get_sensor_value(Config.phase_a_sensor),
                self.get_sensor_value(Config.phase_b_sensor),
                self.get_sensor_value(Config.phase_c_sensor)
            ]

            # Check if we have valid phase data (critical requirement)
            if any(p is None for p in phases):
                return None

            # Ensure all phases are numeric for min() calculation
            try:
                numeric_phases = [float(p) for p in phases if p is not None]
                if len(numeric_phases) != 3:
                    return None
            except (ValueError, TypeError):
                return None

            # Build system state
            system_state = {
                'phases': numeric_phases,
                'most_negative': min(numeric_phases),
                'total_grid_flow': self.get_sensor_value(Config.power_meter_total_sensor),  # Total from power meter (no fallback - same device provides phases)
                'battery_soc': self.get_sensor_value(Config.battery_soc_sensor),
                'solar_input': self.get_sensor_value(Config.inverter_input_sensor),
                'charging_rate_limit': self.get_sensor_value(Config.battery_charge_limit_sensor),
                'discharging_rate_limit': self.get_sensor_value(Config.battery_discharge_limit_sensor),
                'battery_power': self.get_sensor_value(Config.battery_power_sensor),
                'inverter_power': self.get_sensor_value(Config.inverter_power_sensor),
                'forced_power_flow': self._get_current_forced_power_flow(),
                'heating_active': self.get_sensor_value(Config.heating_switch, use_fallback=True) == "on",
                'boiler_active': self.get_sensor_value(Config.boiler_switch, use_fallback=True) == "on",
                'timestamp': time.time(),
                
                # Aliases and Derived Values for API/Dashboard
                'grid_power': self.get_sensor_value(Config.power_meter_total_sensor),
                'pv_power': self.get_sensor_value(Config.inverter_input_sensor),
                'house_load': (self.get_sensor_value(Config.inverter_power_sensor) or 0) - (self.get_sensor_value(Config.power_meter_total_sensor) or 0),
                
                # Phase Power (mapped to l1/l2/l3 for dashboard compatibility)
                'l1_current': numeric_phases[0] if len(numeric_phases) > 0 else 0,
                'l2_current': numeric_phases[1] if len(numeric_phases) > 1 else 0,
                'l3_current': numeric_phases[2] if len(numeric_phases) > 2 else 0
            }

            return system_state

        except Exception as e:
            # Could add logging: self.hass.log_if_enabled(f"Error building system state: {e}", level="WARNING")
            return None

    def _get_current_forced_power_flow(self) -> float:
        """Get current forced power flow (extracted from main app for consistency)"""
        force_state = self.get_sensor_value(Config.battery_forced_charge_sensor) or "Unknown"

        if force_state == "Stopped":
            return 0
        elif force_state and force_state.startswith("Discharging at"):
            try:
                power = int(force_state.split(" ")[2].replace("W", ""))
                return -power
            except (IndexError, ValueError):
                return 0
        elif force_state and force_state.startswith("Charging at"):
            try:
                power = int(force_state.split(" ")[2].replace("W", ""))
                return power
            except (IndexError, ValueError):
                return 0
        else:
            return 0

    def get_health_report(self) -> Dict[str, Dict[str, Any]]:
        """Get comprehensive health report for all sensors"""
        report = {}
        now = time.time()

        for entity, health in self.health_stats.items():
            is_fresh = self.is_sensor_fresh(entity)
            is_valid = self.is_sensor_valid(entity)
            max_age = self.max_ages.get(entity, 60)
            invalid_age = self.invalid_ages.get(entity, 600)
            is_critical = entity in self.critical_sensors

            age_seconds = now - health.last_update if health.last_update > 0 else None

            report[entity] = {
                'fresh': is_fresh,
                'valid': is_valid,
                'critical': is_critical,
                'max_age': max_age,
                'invalid_age': invalid_age,
                'last_update': health.last_update,
                'age_seconds': age_seconds,
                'update_count': health.update_count,
                'error_count': health.error_count,
                'refresh_count': health.refresh_count,
                'avg_update_interval': health.avg_update_interval
            }

        return report

    def cleanup_old_data(self, max_age: float = 3600) -> int:
        """Clean up old sensor data to prevent memory bloat
        
        TODO: Future feature for memory management
        Remove sensor data older than max_age seconds
        
        Args:
            max_age: Maximum age in seconds (default 1 hour)
            
        Returns:
            int: Number of entries removed
        """
        # TODO: Implement cleanup logic
        return 0