"""
Phase Balancer Rewrite (PBR) - Configuration Module

This module contains all configuration constants and settings for the PBR system.
"""

from typing import List, ClassVar, Dict, Literal, Optional


class Config:
    """Configuration class for PBR system settings with TypeScript-like class variables"""

    # Class variables (shared across all instances) - similar to TypeScript static properties
    debug_mode: ClassVar[bool] = False  # Set to False to enable command execution
    debug_logging: ClassVar[bool] = False  # Set to True to enable verbose DEBUG logs
    max_battery_power: ClassVar[int] = 5000  # Maximum battery charging/discharging power (W)

    # Testing overrides - set these to simulate different modes/sources without changing inverter
    qw_override_enabled: ClassVar[bool] = False  # Enable override for testing
    qw_override_mode: ClassVar[Optional[str]] = "frrup"  # Override value for qw_mode (e.g., 'timed', 'maximizeselfconsumption')
    qw_override_source: ClassVar[Optional[str]] = "kratt"  # Override value for qw_source (e.g., 'battery', 'grid', 'solar')
    qw_override_powerlimit: ClassVar[Optional[int]] = 2600  # Override value for qw_powerlimit (e.g., 5000, 0)

    # Battery SOC thresholds
    battery_soc_minimum_for_discharging: ClassVar[float] = 6.0  # Minimum SOC % for allowing discharging
    battery_soc_maximum_for_charging: ClassVar[float] = 100.0  # Maximum SOC % for forced charging
    battery_soc_minimum_constraint: ClassVar[float] = 10.0  # SOC % below which system switches to nomanagement mode
    
    # Huawei Solar device IDs for service calls
    discharge_device_id: ClassVar[str] = "52619225faaebb2615cccf45291f3a31"
    inverter_device_id: ClassVar[str] = "f4ba796b528448ef90b73d5c6ad497ef"
    
    # Tool cooldowns (seconds between commands)
    forced_charge_discharge_cooldown: ClassVar[float] = 5.0  # Forced charging/discharging/stop commands
    charging_adjustment_cooldown: ClassVar[float] = 3.0  # Charging rate limit adjustments
    export_limit_cooldown: ClassVar[float] = 3.0  # Export limit adjustments
    
    # Tool change thresholds
    minimum_charging_adjustment_watts: ClassVar[float] = 10.0  # Minimum change to trigger charging rate update
    minimum_export_limit_change_watts: ClassVar[float] = 200.0  # Minimum change to trigger export limit update
    
    # Export limitation
    max_feed_grid_power: ClassVar[int] = 8800  # Maximum power that can be exported to grid (W)

    # Power thresholds
    minimum_discharge_change_watts: ClassVar[float] = 10.0  # Minimum discharge change to trigger action
    minimum_discharge_reduction_watts: ClassVar[float] = 10.0  # Minimum discharge reduction to trigger action

    # Time zone and schedule settings
    timezone: ClassVar[str] = "Europe/Tallinn"  # Timezone for schedule calculations
    boiler_allowed_start_hour: ClassVar[int] = 7  # Start hour (inclusive) for boiler operation (7am)
    boiler_allowed_end_hour: ClassVar[int] = 22  # End hour (exclusive) for boiler operation (10pm)

    # Sensor data types for validation and formatting
    SensorType = Literal['numeric', 'string', 'boolean']

    # Individual sensor entity names with their expected data types
    phase_a_sensor: ClassVar[str] = "sensor.power_meter_phase_a_active_power"
    phase_b_sensor: ClassVar[str] = "sensor.power_meter_phase_b_active_power"
    phase_c_sensor: ClassVar[str] = "sensor.power_meter_phase_c_active_power"
    phases_sensor: ClassVar[List[str]] = [phase_a_sensor, phase_b_sensor, phase_c_sensor]  # For iteration
    power_meter_total_sensor: ClassVar[str] = "sensor.power_meter_active_power"  # Total grid flow (sum of phases)
    battery_soc_sensor: ClassVar[str] = "sensor.batteries_state_of_capacity"
    battery_power_sensor: ClassVar[str] = "sensor.batteries_charge_discharge_power"
    battery_charge_limit_sensor: ClassVar[str] = "number.batteries_maximum_charging_power"
    battery_discharge_limit_sensor: ClassVar[str] = "number.batteries_maximum_discharging_power"
    battery_forced_charge_sensor: ClassVar[str] = "sensor.batteries_forcible_charge"
    inverter_input_sensor: ClassVar[str] = "sensor.inverter_input_power"
    inverter_power_sensor: ClassVar[str] = "sensor.inverter_active_power"
    inverter_control_sensor: ClassVar[str] = "sensor.inverter_active_power_control"
    qw_mode_sensor: ClassVar[str] = "sensor.qw_mode"
    qw_source_sensor: ClassVar[str] = "sensor.qw_source"
    qw_powerlimit_sensor: ClassVar[str] = "sensor.qw_powerlimit"
    qw_peakshaving_sensor: ClassVar[str] = "sensor.qw_peakshaving"
    heating_switch: ClassVar[str] = "switch.heating"
    boiler_switch: ClassVar[str] = "switch.boiler"
    phase_target_input: ClassVar[str] = "input_number.phase_balancer_phase_target"
    phase_range_low_input: ClassVar[str] = "input_number.phase_balancer_range_low"
    phase_range_high_input: ClassVar[str] = "input_number.phase_balancer_range_high"

    # Sensor data type mappings for validation and safe formatting
    sensor_types: ClassVar[Dict[str, SensorType]] = {
        # Numeric sensors (can be formatted as floats)
        phase_a_sensor: 'numeric',
        phase_b_sensor: 'numeric',
        phase_c_sensor: 'numeric',
        power_meter_total_sensor: 'numeric',
        battery_soc_sensor: 'numeric',
        battery_power_sensor: 'numeric',
        battery_charge_limit_sensor: 'numeric',
        battery_discharge_limit_sensor: 'numeric',
        inverter_input_sensor: 'numeric',
        inverter_power_sensor: 'numeric',
        qw_powerlimit_sensor: 'numeric',
        qw_peakshaving_sensor: 'numeric',
        phase_target_input: 'numeric',
        phase_range_low_input: 'numeric',
        phase_range_high_input: 'numeric',

        # String sensors (status, mode values)
        battery_forced_charge_sensor: 'string',
        inverter_control_sensor: 'string',
        qw_mode_sensor: 'string',
        qw_source_sensor: 'string',

        # Boolean sensors (on/off switches)
        heating_switch: 'boolean',
        boiler_switch: 'boolean',
    }

    # Sensor entities to monitor (list for iteration)
    sensor_entities: ClassVar[List[str]] = [
        # Phase power sensors
        phase_a_sensor,
        phase_b_sensor,
        phase_c_sensor,
        power_meter_total_sensor,

        # Battery sensors
        battery_soc_sensor,
        battery_power_sensor,
        battery_charge_limit_sensor,
        battery_discharge_limit_sensor,
        battery_forced_charge_sensor,

        # Inverter sensors
        inverter_input_sensor,
        inverter_power_sensor,
        inverter_control_sensor,

        # QW system sensors
        qw_mode_sensor,
        qw_source_sensor,
        qw_powerlimit_sensor,
        qw_peakshaving_sensor,

        # Load sensors
        heating_switch,
        boiler_switch,

        # Phase balancer configuration inputs
        phase_target_input,
        phase_range_low_input,
        phase_range_high_input,
    ]

    # QW mode to internal mode mappings
    qw_mode_mappings: ClassVar[Dict[str, str]] = {
        'normal': 'normal',
        'limitexport': 'limitexport',
        'pvsell': 'pvsell',
        'nobattery': 'nobattery',
        'savebattery': 'savebattery',
        'buy': 'buy',
        'sell': 'sell',
        'frrup': 'frrup',
        'frrdown': 'frrdown'
    }

    # Valid QW sources
    valid_qw_sources: ClassVar[List[str]] = ['timer', 'notimer', 'optimizer', 'manual', 'kratt']

    # Mode-specific source validation
    # mFRR modes only accept 'kratt' source, all others accept standard sources
    mfrr_modes: ClassVar[List[str]] = ['frrup', 'frrdown']
    kratt_only_source: ClassVar[str] = 'kratt'

    # Fast trigger settings for early control loop activation
    fast_trigger_enabled: ClassVar[bool] = True  # Enable fast phase monitoring
    fast_trigger_threshold: ClassVar[float] = -300.0  # Watts (negative = import from grid)
    fast_trigger_minimum_interval: ClassVar[float] = 10.0  # Seconds between control loop executions
    fast_trigger_balancing_modes: ClassVar[List[str]] = ['normal', 'limitexport', 'pvsell']  # Modes where fast trigger is active
    log_triggers: ClassVar[bool] = True  # Log fast trigger events (for debugging)


# Create a global config instance (for backward compatibility)
config = Config()