"""
Phase Balancer Rewrite (PBR) - Tools Module

This module implements the tool classes for controlling battery and inverter behavior.
Each tool encapsulates a specific control action (forced charging, forced discharging, etc.)

Tools follow a consistent pattern:
- execute() to perform the action
- stop() to halt the action (where applicable)
- Cooldown protection to prevent rapid command spam
- State checking to avoid redundant commands
- Bounds checking and validation
"""

import time
from typing import Optional, Any
from pbr_config import Config


def service_call_callback(result: Any) -> None:
    """
    Generic callback for non-blocking service calls.
    This callback allows service calls to return immediately without blocking.
    
    According to AppDaemon docs: "Specifying a callback when calling a service will 
    cause it to run in the background and return control to the app immediately."
    
    Args:
        result: Result from the service call (unused for fire-and-forget pattern)
    """
    # Fire-and-forget pattern - no action needed
    pass


def get_current_forced_power(hass_instance, config: Config) -> int:
    """
    Get current forced charging/discharging power from state sensor.
    Shared helper function used by both ForcedChargingTool and ForcedDischargingTool.
    
    Args:
        hass_instance: AppDaemon Hass instance
        config: Config instance
    
    Returns:
        int: Current power (positive for charging, negative for discharging, 0 if idle)
    """
    state = hass_instance.get_state(config.battery_forced_charge_sensor, default="Unknown")
    
    if state and state.startswith("Charging at"):
        try:
            power_str = state.split("Charging at ")[1].split("W")[0]
            return int(float(power_str))
        except (IndexError, ValueError):
            hass_instance.log(f"Failed to parse forced charging power from state: {state}", level="WARNING")
            
    elif state and state.startswith("Discharging at"):
        try:
            power_str = state.split("Discharging at ")[1].split("W")[0]
            return -int(float(power_str))  # Negative for discharging
        except (IndexError, ValueError):
            hass_instance.log(f"Failed to parse forced discharging power from state: {state}", level="WARNING")
            
    return 0


def is_forced_power_realized(hass_instance, data_manager, tolerance_percent: float = 0.85) -> bool:
    """
    Check if the last forced power command has been realized by the inverter.
    
    Compares forced_power_flow (what we commanded) with battery_power (what's actually happening).
    Blocks new forced power commands until inverter reaches target ±tolerance.
    
    This prevents command spam when inverter is slow to ramp up/down, which was causing
    hundreds of commands to queue up during the 2-minute ramp period.
    
    Args:
        hass_instance: AppDaemon Hass instance for logging
        data_manager: DataManager instance for system state
        tolerance_percent: Acceptable percentage of commanded power (default 0.85 = 85%)
    
    Returns:
        True if command is realized or no command active (safe to send new command)
        False if command is still ramping (block new commands)
    """
    system_state = data_manager.get_system_state()
    if not system_state:
        return True  # Fail-open: if we can't get state, allow command
    
    forced_power_flow = system_state.get('forced_power_flow', 0)
    battery_power = system_state.get('battery_power', 0)
    
    # If no forced power command active, always allow
    if forced_power_flow == 0:
        return True
    
    # Calculate tolerance as percentage of commanded power
    # Minimum tolerance of 200W to handle inverter response variance
    tolerance = max(200.0, abs(forced_power_flow) * (1.0 - tolerance_percent))
    
    # Check if battery_power has reached forced_power_flow target
    # Both are signed: negative = discharge, positive = charge
    difference = abs(battery_power - forced_power_flow)
    
    realized = difference <= tolerance
    
    # Log if not realized (for debugging inverter behavior)
    if not realized:
        hass_instance.log_if_enabled(
            f"Forced power not realized: commanded={forced_power_flow:.0f}W, "
            f"actual={battery_power:.0f}W, diff={difference:.0f}W > {tolerance:.0f}W tolerance ({tolerance_percent*100:.0f}%)"
        )
    
    return realized


class ForcedChargingTool:
    """
    Tool for forcing battery charging from grid at a specified power level.
    
    Uses huawei_solar/forcible_charge_soc service to charge battery from grid
    regardless of solar availability. Primary use case: Buy mode (cheap grid prices).
    """
    
    def __init__(self, hass_instance):
        """
        Initialize the Forced Charging tool.
        
        Args:
            hass_instance: AppDaemon Hass instance for service calls and state queries
        """
        self.hass = hass_instance
        self.config = Config()
        self.last_command_time = 0.0
        
    def execute(self, target_power: float, reason: str = "", mode_transition: bool = False) -> bool:
        """
        Execute forced battery charging at specified power.
        
        Args:
            target_power: Desired charging power (0-5000W). 0 stops charging.
            reason: Optional reason for logging
            mode_transition: If True, bypass realization check and cooldown (used for mode transitions)
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Check if previous forced power command has been realized
        # This prevents command spam when inverter is slow to respond
        # Skip this check during mode transitions to allow immediate execution
        if not mode_transition and not is_forced_power_realized(self.hass, self.hass.data_manager):
            self.hass.log_if_enabled(
                f"Forced charging skipped: Previous forced power command still ramping "
                f"(target {target_power:.0f}W blocked)"
            )
            return False
        
        # Round to integer
        target_power = round(target_power)
        
        # Bounds checking
        if target_power > self.config.max_battery_power:
            target_power = self.config.max_battery_power
        elif target_power < 0:
            target_power = 0
            
        # If power is 0, stop forced charging instead
        if target_power == 0:
            return self.stop(reason="Requested power is 0")
            
        # Get current forced charge state
        current_power = get_current_forced_power(self.hass, self.config)
        is_charging = self._is_forced_charging()
        
        # If already charging at this exact power, skip
        if is_charging and current_power == target_power:
            if not hasattr(self, 'last_same_log') or now - self.last_same_log > 300:
                self.hass.log_if_enabled(f"Already forced charging at {target_power}W, no change needed")
                self.last_same_log = now
            return False
            
        # Check cooldown
        if now - self.last_command_time < self.config.forced_charge_discharge_cooldown:
            # Exception for mode transitions (user-initiated)
            if mode_transition:
                self.hass.log_if_enabled("Overriding cooldown for mode transition")
            # Exception for initial charge activation
            elif not is_charging:
                self.hass.log_if_enabled("Overriding cooldown for initial forced charging activation")
            else:
                remaining = self.config.forced_charge_discharge_cooldown - (now - self.last_command_time)
                self.hass.log_if_enabled(f"Forced charging change skipped due to cooldown. Wait {remaining:.1f}s more.")
                return False
        
        # CRITICAL CHECK: Ensure charging_limit is sufficient for target_power
        # If charging_limit < target_power, the inverter won't be able to charge at target rate
        current_charge_limit = self.hass.get_state(self.config.battery_charge_limit_sensor, default=0)
        try:
            current_charge_limit = float(current_charge_limit)
        except (ValueError, TypeError):
            current_charge_limit = 0
        
        if current_charge_limit < target_power:
            # Need to increase charging limit first
            required_limit = max(target_power, self.config.max_battery_power)
            self.hass.log_if_enabled(
                f"Forced charging: Increasing charging limit from {current_charge_limit:.0f}W to "
                f"{required_limit:.0f}W to support {target_power:.0f}W charging"
            )
            self.hass.call_service(
                "number/set_value",
                entity_id=self.config.battery_charge_limit_sensor,
                value=required_limit,
                callback=service_call_callback
            )
                
        # Execute the command (async with callback)
        self.hass.call_service(
            "huawei_solar/forcible_charge_soc",
            target_soc=int(self.config.battery_soc_maximum_for_charging),
            power=str(target_power),
            device_id=self.config.discharge_device_id,  # Same device handles both charge and discharge
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        
    def stop(self, reason: str = "") -> bool:
        """
        Stop forced charging.
        
        Args:
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Check cooldown
        if now - self.last_command_time < self.config.forced_charge_discharge_cooldown:
            remaining = self.config.forced_charge_discharge_cooldown - (now - self.last_command_time)
            self.hass.log_if_enabled(f"Stop forced charging skipped due to cooldown. Wait {remaining:.1f}s more.")
            return False
            
        # Execute stop command (non-blocking)
        self.hass.call_service(
            "huawei_solar/stop_forcible_charge",
            device_id=self.config.discharge_device_id,
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        
    def _is_forced_charging(self) -> bool:
        """
        Check if battery is currently in forced charging mode.
        
        Returns:
            bool: True if forced charging is active
        """
        state = self.hass.get_state(self.config.battery_forced_charge_sensor, default="Unknown")
        return state and state.startswith("Charging at")


class ForcedDischargingTool:
    """
    Tool for forcing battery discharging at a specified power level.
    
    Uses huawei_solar/forcible_discharge_soc service to discharge battery
    to provide power to the system. Used for phase balancing and sell mode.
    """
    
    def __init__(self, hass_instance):
        """
        Initialize the Forced Discharging tool.
        
        Args:
            hass_instance: AppDaemon Hass instance for service calls and state queries
        """
        self.hass = hass_instance
        self.config = Config()
        self.last_command_time = 0.0
        # Track consecutive loops where previous forced discharge command not yet realized
        # After 3 consecutive non-realizations we will allow a new command anyway
        self.not_realized_count = 0
        
    def execute(self, target_power: float, emergency: bool = False, reason: str = "", mode_transition: bool = False) -> bool:
        """
        Execute forced discharging command.
        
        Args:
            target_power: Desired discharge power (0-5000W). 0 stops discharging.
            emergency: If True, bypass cooldown for critical response
            reason: Optional reason for logging
            mode_transition: If True, bypass realization check and cooldown (used for mode transitions)
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Check if previous forced power command has been realized
        # This prevents command spam when inverter is slow to respond
        # Skip this check during mode transitions to allow immediate execution
        realized = is_forced_power_realized(self.hass, self.hass.data_manager)
        if realized:
            # Reset counter when previous command has been realized or no active command
            self.not_realized_count = 0
        else:
            if not mode_transition:
                self.not_realized_count += 1
                if self.not_realized_count < 3 and not emergency:
                    # Still waiting, block new command
                    self.hass.log_if_enabled(
                        f"Forced discharging skipped: previous command still ramping (attempt {self.not_realized_count}/3)"
                    )
                    return False
                else:
                    # Allow command after 3 unsuccessful loops (or emergency override)
                    self.hass.log_if_enabled(
                        f"Forced discharging proceeding after {self.not_realized_count} blocked loops (fallback override)"
                    )
                    # Do not reset counter here; will reset only when realized
        
        # Round to integer
        target_power = round(target_power)
        
        # Bounds checking
        if target_power > self.config.max_battery_power:
            target_power = self.config.max_battery_power
        elif target_power < 0:
            target_power = 0
            
        # If power is 0, stop forced discharging instead
        if target_power == 0:
            return self.stop(reason="Requested power is 0")
            
        # Get current forced discharge state
        current_power = abs(get_current_forced_power(self.hass, self.config))  # Make positive for comparison
        is_discharging = self._is_forced_discharging()
        
        # If already discharging at this exact power, skip
        if is_discharging and current_power == target_power:
            if not hasattr(self, 'last_same_log') or now - self.last_same_log > 300:
                self.hass.log_if_enabled(f"Already forced discharging at {target_power}W, no change needed")
                self.last_same_log = now
            return False
            
        # Check cooldown
        if now - self.last_command_time < self.config.forced_charge_discharge_cooldown:
            # Exception for mode transitions (user-initiated)
            if mode_transition:
                self.hass.log_if_enabled("Overriding cooldown for mode transition")
            # Exception for initial discharge activation
            elif not is_discharging:
                self.hass.log_if_enabled("Overriding cooldown for initial forced discharging activation")
            # Exception for emergency override
            elif emergency:
                self.hass.log_if_enabled("EMERGENCY OVERRIDE: Bypassing cooldown for critical response")
            else:
                remaining = self.config.forced_charge_discharge_cooldown - (now - self.last_command_time)
                self.hass.log_if_enabled(f"Forced discharging change skipped due to cooldown. Wait {remaining:.1f}s more.")
                return False
        
        # CRITICAL CHECK: Ensure discharge_limit is sufficient for target_power
        # If discharge_limit < target_power, the inverter won't be able to discharge at target rate
        current_discharge_limit = self.hass.get_state(self.config.battery_discharge_limit_sensor, default=0)
        try:
            current_discharge_limit = float(current_discharge_limit)
        except (ValueError, TypeError):
            current_discharge_limit = 0
        
        if current_discharge_limit < target_power:
            # Need to increase discharge limit first
            required_limit = max(target_power, self.config.max_battery_power)
            self.hass.log_if_enabled(
                f"Forced discharging: Increasing discharge limit from {current_discharge_limit:.0f}W to "
                f"{required_limit:.0f}W to support {target_power:.0f}W discharging"
            )
            self.hass.call_service(
                "number/set_value",
                entity_id=self.config.battery_discharge_limit_sensor,
                value=required_limit,
                callback=service_call_callback
            )
                
        # Execute the command (async with callback)
        self.hass.call_service(
            "huawei_solar/forcible_discharge_soc",
            target_soc=int(self.config.battery_soc_minimum_for_discharging),
            power=str(target_power),
            device_id=self.config.discharge_device_id,
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        return True
        
    def stop(self, reason: str = "") -> bool:
        """
        Stop forced discharging.
        
        Args:
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Check cooldown
        if now - self.last_command_time < self.config.forced_charge_discharge_cooldown:
            remaining = self.config.forced_charge_discharge_cooldown - (now - self.last_command_time)
            self.hass.log_if_enabled(f"Stop forced discharging skipped due to cooldown. Wait {remaining:.1f}s more.")
            return False
            
        # Execute stop command (non-blocking)
        self.hass.call_service(
            "huawei_solar/stop_forcible_charge",
            device_id=self.config.discharge_device_id,
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        
    def _is_forced_discharging(self) -> bool:
        """
        Check if battery is currently in forced discharging mode.
        
        Returns:
            bool: True if forced discharging is active
        """
        state = self.hass.get_state(self.config.battery_forced_charge_sensor, default="Unknown")
        return state and state.startswith("Discharging at")


class ChargingAdjustmentTool:
    """
    Tool for adjusting battery charging rate limit from solar PV.
    
    Controls number.batteries_maximum_charging_power (0-5000W) to manage
    how much solar power flows into the battery. Used for phase balancing.
    
    Note: When exiting modes that use limited charging, this should be reset
    to maximum (5000W) to restore normal operation.
    """
    
    def __init__(self, hass_instance):
        """
        Initialize the Charging Adjustment tool.
        
        Args:
            hass_instance: AppDaemon Hass instance for service calls and state queries
        """
        self.hass = hass_instance
        self.config = Config()
        self.last_command_time = 0.0
        
    def execute(self, target_rate: float, reason: str = "") -> bool:
        """
        Set battery charging rate limit.
        
        Args:
            target_rate: Desired charging rate (0-5000W)
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Round to integer
        target_rate = round(target_rate)
        
        # Bounds checking
        if target_rate > self.config.max_battery_power:
            target_rate = self.config.max_battery_power
        elif target_rate < 0:
            target_rate = 0
            
        # Get current charging rate
        current_rate = self._get_current_charging_limit()
        
        # Check if change is significant enough
        change = abs(target_rate - current_rate)
        if change < self.config.minimum_charging_adjustment_watts:
            if not hasattr(self, 'last_small_change_log') or now - self.last_small_change_log > 300:
                self.hass.log_if_enabled(f"Charging rate change too small ({change:.0f}W < {self.config.minimum_charging_adjustment_watts}W), skipping update")
                self.last_small_change_log = now
            return False
            
        # If already at this rate, skip
        if current_rate == target_rate:
            if not hasattr(self, 'last_same_log') or now - self.last_same_log > 300:
                self.hass.log_if_enabled(f"Charging rate already at {target_rate}W, no change needed")
                self.last_same_log = now
            return False
            
        # Check cooldown
        if now - self.last_command_time < self.config.charging_adjustment_cooldown:
            remaining = self.config.charging_adjustment_cooldown - (now - self.last_command_time)
            self.hass.log_if_enabled(f"Charging adjustment skipped due to cooldown. Wait {remaining:.1f}s more.")
            return False
            
        # Execute the command (non-blocking)
        self.hass.call_service(
            "number/set_value",
            entity_id=self.config.battery_charge_limit_sensor,
            value=target_rate,
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        
    def reset_to_maximum(self, reason: str = "") -> bool:
        """
        Reset charging rate to maximum (5000W).
        
        Should be called when exiting modes that use limited charging
        to restore normal operation.
        
        Args:
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        return self.execute(
            target_rate=self.config.max_battery_power,
            reason=f"Reset to maximum" + (f" - {reason}" if reason else "")
        )
        
    def _get_current_charging_limit(self) -> float:
        """
        Get current battery charging rate limit.
        
        Returns:
            float: Current charging rate limit in watts
        """
        try:
            return float(self.hass.get_state(self.config.battery_charge_limit_sensor)) or 0.0
        except (TypeError, ValueError):
            self.hass.log_if_enabled(f"Failed to read charging limit from {self.config.battery_charge_limit_sensor}", level="WARNING")
            return 0.0


class ExportLimitationTool:
    """
    Tool for controlling inverter export limit to grid.
    
    Uses huawei_solar services to limit how much power the inverter exports.
    Primary use case: limitexport mode - reduce export during low-price periods
    to avoid selling solar at unprofitable rates.
    
    Note: Export limitation is NOT for emergency phase balancing. When phases
    go negative, charging adjustment should be used instead to free up power.
    """
    
    def __init__(self, hass_instance):
        """
        Initialize the Export Limitation tool.
        
        Args:
            hass_instance: AppDaemon Hass instance for service calls and state queries
        """
        self.hass = hass_instance
        self.config = Config()
        self.last_command_time = 0.0
        
    def execute(self, target_limit: float, reason: str = "") -> bool:
        """
        Set inverter export limit.
        
        Automatically chooses between set_maximum_feed_grid_power and
        reset_maximum_feed_grid_power based on target value.
        
        Args:
            target_limit: Desired export limit (0-8800W). 8800W removes limit.
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Round to integer
        target_limit = round(target_limit)
        
        # Bounds checking
        if target_limit > self.config.max_feed_grid_power:
            target_limit = self.config.max_feed_grid_power
        elif target_limit < 0:
            target_limit = 0
            
        # Get current export limit
        current_limit, current_mode = self._get_current_export_limit()
        
        # Check if already at target
        if current_mode == "unlimited" and target_limit >= self.config.max_feed_grid_power:
            if not hasattr(self, 'last_same_log') or now - self.last_same_log > 300:
                self.hass.log_if_enabled("Export limit already at maximum (unlimited), no change needed")
                self.last_same_log = now
            return False
            
        if current_mode == "limited" and current_limit == target_limit:
            if not hasattr(self, 'last_same_log') or now - self.last_same_log > 300:
                self.hass.log_if_enabled(f"Export limit already at {target_limit}W, no change needed")
                self.last_same_log = now
            return False
            
        # Check if change is significant enough (only for limited->limited transitions)
        if current_mode == "limited" and target_limit < self.config.max_feed_grid_power:
            change = abs(target_limit - current_limit)
            if change < self.config.minimum_export_limit_change_watts:
                if not hasattr(self, 'last_small_change_log') or now - self.last_small_change_log > 300:
                    self.hass.log_if_enabled(f"Export limit change too small ({change:.0f}W < {self.config.minimum_export_limit_change_watts}W), skipping update")
                    self.last_small_change_log = now
                return False
                
        # Check cooldown
        if now - self.last_command_time < self.config.export_limit_cooldown:
            remaining = self.config.export_limit_cooldown - (now - self.last_command_time)
            self.hass.log_if_enabled(f"Export limit adjustment skipped due to cooldown. Wait {remaining:.1f}s more.")
            return False
            
        # Execute the command - choose service based on target
        if target_limit >= self.config.max_feed_grid_power:
            # Use reset for maximum/unlimited (non-blocking)
            self.hass.call_service(
                "huawei_solar/reset_maximum_feed_grid_power",
                device_id=self.config.inverter_device_id,
                callback=service_call_callback
            )
            log_msg = f"Reset export limit to maximum (unlimited)"
        else:
            # Use set for specific limit (non-blocking)
            self.hass.call_service(
                "huawei_solar/set_maximum_feed_grid_power",
                power=str(target_limit),
                device_id=self.config.inverter_device_id,
                callback=service_call_callback
            )
            log_msg = f"Set export limit to {target_limit}W"
            if current_mode == "limited":
                log_msg += f" (was {current_limit}W)"
            else:
                log_msg += f" (was {current_mode})"
                
        self.last_command_time = now
        return True
        
    def reset_to_maximum(self, reason: str = "") -> bool:
        """
        Reset export limit to maximum (unlimited).
        
        Should be called when exiting limitexport mode to restore
        full export capability.
        
        Args:
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        return self.execute(
            target_limit=self.config.max_feed_grid_power,
            reason=f"Reset to maximum" + (f" - {reason}" if reason else "")
        )
        
    def _get_current_export_limit(self) -> tuple[float, str]:
        """
        Get current export limit and mode from inverter.
        
        Returns:
            tuple: (limit_watts, mode) where mode is "limited", "unlimited", or "zero"
                   For "unlimited" mode, limit_watts is max_feed_grid_power
                   For "zero" mode, limit_watts is 0
        """
        state = self.hass.get_state(self.config.inverter_control_sensor, default="Unknown")
        
        # Parse the state - it can be a number or a status string
        if state == "Unknown" or state is None:
            self.hass.log_if_enabled(f"Failed to read export limit from {self.config.inverter_control_sensor}", level="WARNING")
            return (0.0, "unknown")
            
        # Try to parse as a number first (limited mode)
        try:
            limit = float(state)
            if limit == 0:
                return (0.0, "zero")
            elif limit >= self.config.max_feed_grid_power:
                return (float(self.config.max_feed_grid_power), "unlimited")
            else:
                return (limit, "limited")
        except (TypeError, ValueError):
            # It's a string status
            state_lower = str(state).lower()
            if "unlimited" in state_lower or "maximum" in state_lower:
                return (float(self.config.max_feed_grid_power), "unlimited")
            elif "zero" in state_lower or state == "0":
                return (0.0, "zero")
            else:
                # Try to extract a number from the string
                import re
                match = re.search(r'(\d+)', str(state))
                if match:
                    return (float(match.group(1)), "limited")
                    
        # Default fallback
        self.hass.log_if_enabled(f"Could not parse export limit state: {state}", level="WARNING")
        return (float(self.config.max_feed_grid_power), "unlimited")


class DischargeLimitationTool:
    """
    Tool for controlling battery discharge rate limit.
    
    Adjusts the maximum discharge power allowed from the battery.
    Primary use case: FRRDOWN mode - limit discharge to force more grid import.
    
    Similar to ChargingAdjustmentTool but controls discharge instead of charging.
    """
    
    def __init__(self, hass_instance):
        """
        Initialize the Discharge Limitation tool.
        
        Args:
            hass_instance: AppDaemon Hass instance for service calls and state queries
        """
        self.hass = hass_instance
        self.config = Config()
        self.last_command_time = 0.0
        
    def execute(self, target_rate: float, reason: str = "") -> bool:
        """
        Set battery discharge rate limit.
        
        Args:
            target_rate: Desired discharge limit (0-5000W)
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        import time
        now = time.time()
        
        # Round to integer
        target_rate = round(target_rate)
        
        # Bounds checking
        if target_rate > self.config.max_battery_power:
            target_rate = self.config.max_battery_power
        elif target_rate < 0:
            target_rate = 0
            
        # Get current discharge limit
        current_limit = self._get_current_discharge_limit()
        
        # Check if change is significant enough
        change = abs(target_rate - current_limit)
        if change < self.config.minimum_charging_adjustment_watts:
            self.hass.log_if_enabled(f"Discharge limit change too small ({change:.0f}W < {self.config.minimum_charging_adjustment_watts}W), skipping update")
            return False
            
        # Check cooldown
        if now - self.last_command_time < self.config.charging_adjustment_cooldown:
            remaining = self.config.charging_adjustment_cooldown - (now - self.last_command_time)
            self.hass.log_if_enabled(f"Discharge limit adjustment skipped due to cooldown. Wait {remaining:.1f}s more.")
            return False
            
        # Execute the command (non-blocking)
        self.hass.call_service(
            "number/set_value",
            entity_id=self.config.battery_discharge_limit_sensor,
            value=target_rate,
            callback=service_call_callback
        )
        
        self.last_command_time = now
        return True
        
    def reset_to_maximum(self, reason: str = "") -> bool:
        """
        Reset discharge rate to maximum (5000W).
        
        Should be called when exiting modes that use limited discharge
        to restore normal operation.
        
        Args:
            reason: Optional reason for logging
            
        Returns:
            bool: True if command was executed, False if skipped
        """
        return self.execute(
            target_rate=self.config.max_battery_power,
            reason=f"Reset to maximum" + (f" - {reason}" if reason else "")
        )
        
    def _get_current_discharge_limit(self) -> float:
        """
        Get current battery discharge rate limit.
        
        Returns:
            float: Current discharge rate limit in watts
        """
        try:
            return float(self.hass.get_state(self.config.battery_discharge_limit_sensor)) or 0.0
        except (TypeError, ValueError):
            self.hass.log_if_enabled(f"Failed to read discharge limit from {self.config.battery_discharge_limit_sensor}", level="WARNING")
            return 0.0

