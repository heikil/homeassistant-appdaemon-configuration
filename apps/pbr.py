"""
Phase Balancer Rewrite (PBR) - Read-Only Logging Version

This is the initial read-only implementation that subscribes to sensors,
logs their updates, calculates desired state, and logs proposed actions
without executing any commands.

Used to verify sensor subscriptions and data flow before implementing
command execution logic.
"""

import time
import appdaemon.plugins.hass.hassapi as hass
from typing import Dict, Any, Optional
from pbr_config import Config
from pbr_modes import ModeManager
from pbr_data_manager import DataManager
from pbr_state import StateEngine
from pbr_tools import ForcedChargingTool, ForcedDischargingTool, ChargingAdjustmentTool, ExportLimitationTool, DischargeLimitationTool
from pbr_load_switching_tool import LoadSwitchingTool
from loads_config import DEVICES
from pbr_action_executor import ActionExecutor
from pbr_actions import ChargingAdjustmentAction, DischargeLimitationAction, ForcedChargingAction, ForcedDischargingAction, ExportLimitationAction
from pbr_fast_trigger import FastPhaseTrigger




class PhaseBalancerRewrite(hass.Hass):
    """
    Phase Balancer Rewrite - Read-Only Logging Version

    Implements sensor subscriptions, data logging, and desired state calculation
    without command execution for initial testing and verification.
    """

    # Config will be assigned to self.config in initialize() for TypeScript-like access

    # Sensor data storage (now handled by data_manager, keeping for backward compatibility)
    sensor_data: Dict[str, Dict[str, Any]] = {}
    last_sensor_log: Dict[str, float] = {}

    def initialize(self):
        """Initialize the phase balancer"""
        # Logging toggle setup - initialize state FIRST (before any logging calls)
        self.LOGGING_ENABLED = self.get_state("input_boolean.phase_balancer_logging") == "on"
        
        # Assign Config class to instance for TypeScript-like access
        self.config = Config

        # Track last control loop execution for fast trigger coordination
        self.last_control_loop_execution = 0.0

        # Initialize data manager
        self.data_manager = DataManager(self)

        # Initialize state engine
        self.state_engine = StateEngine(self.data_manager)

        # Initialize heating state tracking for discharge limit restoration
        self.previous_heating_active = None

        # Initialize tools
        self.tools = {
            'forced_charging': ForcedChargingTool(self),
            'forced_discharging': ForcedDischargingTool(self),
            'charging_adjustment': ChargingAdjustmentTool(self),
            'export_limitation': ExportLimitationTool(self),
            'discharge_limitation': DischargeLimitationTool(self),
            'load_switching': LoadSwitchingTool(self, DEVICES)
        }

        # Initialize mode manager (instance with tools for transitions)
        self.mode_manager: Optional[ModeManager] = None  # Will be created after first mode detection

        # Initialize action executor
        self.action_executor = ActionExecutor(self, self.tools)

        # Initialize fast phase trigger (if enabled)
        self.fast_trigger: Optional[FastPhaseTrigger] = None
        if self.config.fast_trigger_enabled:
            self.fast_trigger = FastPhaseTrigger(
                hass_instance=self,
                trigger_callback=self._triggered_control_loop,
                get_mode_callback=self._get_current_mode_for_fast_trigger,
                config=self.config,
                get_last_execution_callback=lambda: self.last_control_loop_execution,
                get_soc_callback=lambda: self.data_manager.get_sensor_value(self.config.battery_soc_sensor),
                get_heating_active_callback=lambda: (self.get_current_system_state() or {}).get('heating_active', False),
                log_if_enabled_callback=self.log_if_enabled
            )

        self.log_if_enabled("Phase Balancer Rewrite - Initializing...")

        # On reload, cancel any previously-registered AppDaemon handles to avoid duplicates
        if hasattr(self, "_ad_handles"):
            for typ, handle in list(self._ad_handles):
                try:
                    if typ == "listen_state":
                        self.cancel_listen_state(handle)
                    elif typ in ("timer", "run_every", "run_in", "run_at"):
                        self.cancel_timer(handle)
                except Exception as e:
                    self.log_if_enabled(f"Failed to cancel handle {typ}: {e}", level="WARNING")
            self._ad_handles = []

        # Initialize handle tracking for this instance
        self._ad_handles = []

        # Register logging toggle listener after cleanup
        try:
            h = self.listen_state(self.on_logging_toggle, "input_boolean.phase_balancer_logging")
            self._ad_handles.append(("listen_state", h))
        except Exception:
            self.log_if_enabled("Failed to register logging toggle listener", level="WARNING")

        # Initialize sensor data through data manager
        sensors_found = 0
        sensors_none = []
        for entity in self.config.sensor_entities:
            # Get current value from Home Assistant (data manager will cache it)
            current_value = self.data_manager.get_sensor_value(entity)

            # Log initial sensor values
            if current_value is not None:
                sensors_found += 1
                sensor_name = entity.split('.')[-1]
                try:
                    # Try to format as number for numeric sensors
                    numeric_value = float(current_value)
                    self.log_if_enabled(f"INITIAL {sensor_name} = {numeric_value:.1f}")
                except (ValueError, TypeError):
                    # For non-numeric sensors
                    self.log_if_enabled(f"INITIAL {sensor_name} = {current_value}")
            else:
                sensor_name = entity.split('.')[-1]
                self.log_if_enabled(f"INITIAL {sensor_name} = None (no current value)")
                sensors_none.append(sensor_name)

        self.log_if_enabled(f"SENSORS: Found {sensors_found}/{len(self.config.sensor_entities)} entities at startup")
        if sensors_none:
            self.log_if_enabled(f"SENSORS WITH None VALUES: {', '.join(sensors_none)}", level="WARNING")

        # Apply initial heating protection if heating is active at startup
        heating_state = self.get_state(self.config.heating_switch)
        if heating_state == "on":
            # Apply initial heating protection - set discharge limit to 0W
            from pbr_actions import DischargeLimitationAction
            protection_action = DischargeLimitationAction(
                target_limit=0,
                reason="Initial heating protection: heating active at startup"
            )
            self.action_executor.execute_actions([protection_action], "startup")
            self.log_if_enabled("INITIAL HEATING PROTECTION: discharge limit set to 0W")
            self.previous_heating_active = True
        else:
            self.previous_heating_active = False

        # Subscribe ONLY to configuration inputs and mode changes (not power/battery sensors)
        # Power/battery sensors are read synchronously every 10s in control loop
        config_subscriptions = [
            self.config.phase_target_input,
            self.config.phase_range_low_input,
            self.config.phase_range_high_input,
            self.config.qw_mode_sensor,
            self.config.qw_source_sensor,
            "input_boolean.appdaemon_actions",  # Enable/disable actions
        ]
        
        subscription_count = 0
        for entity in config_subscriptions:
            try:
                current_value = self.get_state(entity)
                if current_value is not None:
                    h = self.listen_state(self.on_config_update, entity)
                    self._ad_handles.append(("listen_state", h))
                    subscription_count += 1
                    self.log_if_enabled(f"Subscribed to config: {entity.split('.')[-1]}")
                else:
                    self.log_if_enabled(f"WARNING: Config entity {entity} not found", level="WARNING")
            except Exception as e:
                self.log_if_enabled(f"ERROR: Failed to subscribe to {entity}: {e}", level="ERROR")
        
        self.log_if_enabled(f"Subscribed to {subscription_count} configuration entities (power sensors read synchronously)")

        # Set up periodic logging (every 30 seconds)
        t = self.run_every(self.log_system_state, "now+30", 30)
        self._ad_handles.append(("timer", t))

        # Set up periodic state calculation (every 10 seconds)
        # Run immediately on startup, then every 10 seconds
        self.run_in(self.calculate_and_log_desired_state, 1)  # Initial run after 1 second
        t = self.run_every(self.calculate_and_log_desired_state, "now+10", 10)
        self._ad_handles.append(("timer", t))

        # Subscribe fast trigger to phase sensors (if enabled)
        if self.fast_trigger:
            self.fast_trigger.subscribe()

        if self.config.debug_mode:
            self.log_if_enabled("Phase Balancer Rewrite initialized in DEBUG/READ-ONLY mode")
        else:
            self.log_if_enabled("Phase Balancer Rewrite initialized in ACTIVE mode")

        # Log successful initialization
        qw_mode = self.data_manager.get_sensor_value(self.config.qw_mode_sensor, use_fallback=True)
        qw_source = self.data_manager.get_sensor_value(self.config.qw_source_sensor, use_fallback=True)
        qw_powerlimit = self.data_manager.get_sensor_value(self.config.qw_powerlimit_sensor, use_fallback=True)
        current_mode = self.determine_current_mode(qw_mode, qw_source, qw_powerlimit)
        if current_mode:
            self.log_if_enabled(f"System initialized in mode: {current_mode}")

    def on_config_update(self, entity, attribute, old, new, kwargs):
        """Handle configuration or mode changes (not power sensors)"""
        sensor_name = entity.split('.')[-1]
        
        # Handle actions toggle specially
        if entity == "input_boolean.appdaemon_actions":
            if old == "on" and new == "off":
                # Actions just turned OFF - reset to safe state
                self.log("AppDaemon actions DISABLED - resetting to safe state")
                self._reset_to_safe_state()
            elif old == "off" and new == "on":
                self.log("AppDaemon actions ENABLED")
            return
        
        # Log other config changes
        self.log_if_enabled(f"Config updated: {sensor_name} = {old} → {new}")
        
        # If mode/source changed, may need to trigger mode transition
        if entity in [self.config.qw_mode_sensor, self.config.qw_source_sensor]:
            self.log_if_enabled(f"Mode/source change detected, will re-evaluate on next control loop")
    
    def _reset_to_safe_state(self):
        """Reset system to safe state (equivalent to normal mode) when actions are disabled"""
        # Ensure ModeManager exists (created lazily in control loop)
        if self.mode_manager is None:
            self.mode_manager = ModeManager(self, self.tools)

        # Apply normal mode initial state for consistency
        self.mode_manager._apply_mode_initial_state('normal')

        self.log("Safe state reset complete - restored normal mode initial state")

    def log_system_state(self, kwargs=None):
        """Log comprehensive system state - disabled, info now in 10s cycle log"""
        # System state details now included in calculate_and_log_desired_state every 10s
        # This 30s log is redundant and creates spam
        pass

    def _triggered_control_loop(self, source="unknown"):
        """Central entry point for all control loop triggers (periodic timer and fast trigger)."""
        # Update execution timestamp
        self.last_control_loop_execution = time.time()
        
        # Log trigger source if enabled
        if self.config.log_triggers:
            self.log_if_enabled(f"Control loop triggered by: {source}")
        
        # If this trigger came from the fast-trigger subsystem, avoid running
        # the control loop when heating is active. This prevents the fast
        # trigger from attempting to adjust the battery while a large
        # uncontrolled heating load is drawing power.
        if source and str(source).startswith("fast_trigger"):
            try:
                # Use current system state for heating flag (cached by DataManager)
                system_state = self.get_current_system_state()
                heating_active = False
                if system_state:
                    heating_active = bool(system_state.get('heating_active', False))
                else:
                    # Fall back to direct entity read if DataManager unavailable
                    heating_state = self.get_state(self.config.heating_switch)
                    heating_active = (heating_state == "on")

                if heating_active:
                    if self.config.log_triggers:
                        self.log_if_enabled(f"FAST TRIGGER IGNORED: heating active, skip trigger ({source})")
                    return
            except Exception:
                # In case of error reading heating state, proceed with control loop
                pass

        # Call actual control loop
        self.calculate_and_log_desired_state()

    def calculate_and_log_desired_state(self, kwargs=None):
        """Calculate desired state and log proposed actions.
        
        Reads all sensor values synchronously at start of each cycle for consistency.
        """
        try:
            # Check if actions are enabled first - if not, skip all calculations
            actions_enabled = self.get_state("input_boolean.appdaemon_actions") == "on"
            if not actions_enabled:
                # Actions disabled - skip everything, don't even read sensors or calculate
                if not hasattr(self, '_actions_disabled_logged') or not self._actions_disabled_logged:
                    self.log_if_enabled("Actions disabled via input_boolean.appdaemon_actions - control loop paused")
                    self._actions_disabled_logged = True
                return
            
            # Actions enabled - reset flag
            self._actions_disabled_logged = False
            
            # Read all sensor values synchronously from Home Assistant
            # This ensures all values are from the same moment in time
            for entity in self.config.sensor_entities:
                current_value = self.get_state(entity)
                if current_value is not None:
                    self.data_manager.update_sensor(entity, current_value)
            
            # Get current system state
            system_state = self.get_current_system_state()
            if not system_state:
                return

            # Determine operating mode (with fallbacks for invalid sensors)
            qw_mode = self.data_manager.get_sensor_value(self.config.qw_mode_sensor, use_fallback=True)
            qw_source = self.data_manager.get_sensor_value(self.config.qw_source_sensor, use_fallback=True)
            qw_powerlimit = self.data_manager.get_sensor_value(self.config.qw_powerlimit_sensor, use_fallback=True)
            
            # Apply testing overrides if enabled (BEFORE mode determination)
            if self.config.qw_override_enabled:
                # Log override once when first enabled or when values change
                if not hasattr(self, '_override_logged'):
                    self._override_logged = {}
                
                if self.config.qw_override_mode is not None:
                    qw_mode = self.config.qw_override_mode
                    if self._override_logged.get('mode') != qw_mode:
                        self.log_if_enabled(f"QW OVERRIDE: Using manual mode '{qw_mode}'", level="WARNING")
                        self._override_logged['mode'] = qw_mode
                if self.config.qw_override_source is not None:
                    qw_source = self.config.qw_override_source
                    if self._override_logged.get('source') != qw_source:
                        self.log_if_enabled(f"QW OVERRIDE: Using manual source '{qw_source}'", level="WARNING")
                        self._override_logged['source'] = qw_source
                if self.config.qw_override_powerlimit is not None:
                    qw_powerlimit = self.config.qw_override_powerlimit
                    # Update data_manager so state engine sees the override value
                    self.data_manager.update_sensor(self.config.qw_powerlimit_sensor, qw_powerlimit)
                    if self._override_logged.get('powerlimit') != qw_powerlimit:
                        self.log_if_enabled(f"QW OVERRIDE: Using manual powerlimit {qw_powerlimit}W", level="WARNING")
                        self._override_logged['powerlimit'] = qw_powerlimit
            
            # Determine current mode (after overrides applied)
            current_mode = self.determine_current_mode(qw_mode, qw_source, qw_powerlimit)
            if current_mode is None:
                return
            
            # Update fast trigger subscription based on SOC
            if self.fast_trigger:
                self.fast_trigger.update_subscription()

            # Check for heating state change and restore discharge limit if heating turned off
            heating_active = system_state.get('heating_active', False)
            if self.previous_heating_active is True and not heating_active:
                # Heating just turned OFF - restore discharge limit to mode initial
                initial_state = ModeManager.mode_initial_states.get(current_mode, {})
                discharge_setting = initial_state.get('discharge_limit', 'maximum')

                if discharge_setting == 'maximum':
                    target_limit = self.config.max_battery_power
                    reason = f"Heating off: restore discharge limit to maximum ({target_limit}W)"
                elif discharge_setting == 'zero':
                    target_limit = 0
                    reason = f"Heating off: restore discharge limit to zero (0W)"
                else:
                    # Fallback to maximum if unknown
                    target_limit = self.config.max_battery_power
                    reason = f"Heating off: restore discharge limit to maximum ({target_limit}W)"

                # Execute restoration
                from pbr_actions import DischargeLimitationAction
                restoration_action = DischargeLimitationAction(
                    target_limit=target_limit,
                    reason=reason
                )
                self.action_executor.execute_actions([restoration_action], current_mode)
                self.log_if_enabled(f"DISCHARGE LIMIT RESTORED: {reason}")

            # Update heating state tracking
            self.previous_heating_active = heating_active

            # HEATING PROTECTION: When heating/boiler is ON, protect battery from discharge
            # Exception: Buy mode can still charge battery (cheap hours overlap with heating)
            if self._apply_heating_protection(system_state, current_mode):
                # Heating protection applied - skip balancing
                return

            # Initialize mode manager on first run
            if self.mode_manager is None:
                self.mode_manager = ModeManager(self, self.tools)
            
            # Handle mode transitions (handles initial mode and changes automatically)
            source = qw_source if qw_source is not None else "unknown"
            self.mode_manager.handle_mode_change(current_mode, source)

            # Calculate desired state
            desired_state = self.state_engine.calculate_desired_state(system_state, current_mode)

            if desired_state is None:
                # Missing required data (e.g., target_phase), skip balancing
                return

            # Check system validity - if invalid, enter unknown mode behavior
            if not self.data_manager.is_system_valid():
                self.log_if_enabled("SYSTEM INVALID: Critical sensor data too old, entering safe mode")
                # Could set mode to 'unknown' or 'nomanagement' here
                return

            # Log desired state and decisions
            self.log_desired_state_decisions(system_state, desired_state, current_mode)

            # Determine tool sequence for current mode (bidirectional based on surplus/deficit)
            # Calculate surplus/deficit state from desired state
            # For mFRR modes, the sign convention is different:
            # - FRRDOWN: positive battery_flow_change = need more import (DEFICIT behavior)
            # - FRRUP: positive battery_flow_change = need more export (SURPLUS behavior)
            # - Normal modes: positive battery_flow_change = surplus (more charging available)
            if current_mode in ['frrdown', 'frrup']:
                # For mFRR modes, invert the surplus logic
                # FRRDOWN with positive flow_change needs deficit tools (not reversed)
                # FRRUP with positive flow_change needs surplus tools (reversed)
                surplus_state = (current_mode == 'frrup' and desired_state.energy_flow.battery_flow_change > 0) or \
                                (current_mode == 'frrdown' and desired_state.energy_flow.battery_flow_change < 0)
            else:
                surplus_state = desired_state.energy_flow.battery_flow_change > 0
            tool_sequence = self.get_mode_tool_sequence(current_mode, surplus_state)

            # Calculate what tools would be used (simulated in debug mode)
            proposed_actions = self.calculate_proposed_actions(system_state, desired_state, tool_sequence, current_mode)

            # Log or execute actions based on debug mode
            if self.config.debug_mode:
                # Read-only mode - just log
                if proposed_actions:
                    self.log_proposed_actions(proposed_actions, current_mode)
            else:
                # Active mode - execute actions
                if proposed_actions:
                    self.action_executor.execute_actions(proposed_actions, current_mode)

        except Exception as e:
            self.log_if_enabled(f"Error calculating desired state: {e}", level="WARNING")

    def get_current_system_state(self):
        """Get current system state from data manager"""
        return self.data_manager.get_system_state()

    # _get_current_forced_power_flow moved to DataManager for better encapsulation

    def _apply_heating_protection(self, system_state, mode):
        """
        Apply heating protection: Set discharge limit to 0W when heating is ON.
        
        IMPORTANT: Boiler does NOT block battery discharge - only heating does.
        Boiler is a manageable load (phase balancing can work around it).
        Heating is uncontrolled and draws too much power to risk battery discharge.
        
        Protection applies to normal and limitexport modes only.
        Buy mode is ALLOWED to charge battery (cheap hours overlap with heating).
        Other modes (sell, frrup, frrdown) bypass protection.
        
        Returns:
            True if protection was applied and balancing should be skipped
            False if no protection needed (continue normal operation)
        """
        heating_active = system_state.get('heating_active', False)
        boiler_active = system_state.get('boiler_active', False)
        
        # ONLY heating blocks discharge - boiler is OK (phase balancing handles it)
        if not heating_active:
            return False
        
        # Heating is ON
        load_name = 'heating'

        # Modes where we STILL want to run balancing/charging logic while heating is on.
        # These modes require the ability to charge (e.g., buy or frequency regulation importing).
        # We also include 'frrup' here so that LoadSwitchingTool can run to turn OFF heating,
        # even though battery discharge will be blocked by protection.
        charge_allowed_modes = ['buy', 'frrdown', 'frrup', 'savebattery', 'nobattery', 'normal']  # Extend list if needed: e.g. ['buy', 'frrdown']

        if mode in charge_allowed_modes:
            current_discharge_limit = system_state.get('discharging_rate_limit', 0)
            if current_discharge_limit > 0:
                self.log_if_enabled(f"HEATING PROTECTION: {load_name} ON - limiting discharge to 0W (mode={mode}, charging still allowed)")
                from pbr_actions import DischargeLimitationAction
                protection_action = DischargeLimitationAction(
                    target_limit=0,
                    reason=f"Heating protection: {load_name} active (allow charging)"
                )
                self.action_executor.execute_actions([protection_action], mode)
            else:
                self.log_if_enabled(f"HEATING PROTECTION: {load_name} ON - discharge already blocked (mode={mode}, charging allowed)")
            
            # Stop forced discharge if active (it shouldn't be running during heating protection)
            current_forced_flow = system_state.get('forced_power_flow', 0)
            if current_forced_flow < 0: # Negative = discharging
                self.log_if_enabled(f"HEATING PROTECTION: {load_name} ON - stopping active forced discharge (mode={mode})")
                self.tools['forced_discharging'].stop(reason=f"Heating protection: {load_name} active")
                # Update local system state
                system_state['forced_power_flow'] = 0
            
            # Update local system state to reflect that discharge is blocked (even if we didn't just change it)
            # This ensures subsequent logic knows discharge capacity is 0
            system_state['discharging_rate_limit'] = 0

            # Do NOT skip balancing for these modes so charging / other tools can proceed
            return False

        # Modes where we ENFORCE discharge block AND SKIP balancing entirely (battery protection priority)
        if mode in ['normal', 'limitexport', 'pvsell', 'nobattery', 'savebattery', 'sell', 'frrup']:
            current_discharge_limit = system_state.get('discharging_rate_limit', 0)
            
            # Set discharge limit to 0W if not already
            if current_discharge_limit > 0:
                self.log_if_enabled(f"HEATING PROTECTION: {load_name} ON - setting discharge limit to 0W (mode={mode})")
                # Execute discharge limitation action
                from pbr_actions import DischargeLimitationAction
                protection_action = DischargeLimitationAction(
                    target_limit=0,
                    reason=f"Heating protection: {load_name} active"
                )
                self.action_executor.execute_actions([protection_action], mode)
            
            # Stop forced discharge if active (it shouldn't be running during heating protection)
            current_forced_flow = system_state.get('forced_power_flow', 0)
            if current_forced_flow < 0: # Negative = discharging
                self.log_if_enabled(f"HEATING PROTECTION: {load_name} ON - stopping active forced discharge (mode={mode})")
                self.tools['forced_discharging'].stop(reason=f"Heating protection: {load_name} active")

            # Skip all balancing - no phase balancing while heating is on
            return True
        
        # Unknown mode - allow normal operation
        return False

    def get_mode_tool_sequence(self, mode, surplus=False):
        """Get tool execution sequence for the given mode"""
        return ModeManager.get_tool_sequence(mode, surplus)

    def calculate_proposed_actions(self, system_state, desired_state, tool_sequence, mode):
        """
        Calculate what actions would be taken based on desired state.
        
        Tool sequence is already reversed for surplus/deficit by ModeManager.
        Each tool handles both increase and decrease operations bidirectionally.
        """
        actions = []
        battery_flow_change = desired_state.energy_flow.battery_flow_change
        
        # Sign convention for tool handlers:
        # Positive flow_change = surplus (reduce discharge/increase charging)
        # Negative flow_change = deficit (increase discharge/reduce charging)
        #
        # For FRRDOWN, the battery_flow_change sign is inverted:
        # - Positive = need more import (DEFICIT) -> negate to make negative for handlers
        # - Negative = need less import (SURPLUS) -> negate to make positive for handlers
        original_battery_flow_change = battery_flow_change
        if mode == 'frrdown':
            battery_flow_change = -battery_flow_change
            self.log_if_enabled(f"DEBUG FRRDOWN: original={original_battery_flow_change:.0f}W, negated={battery_flow_change:.0f}W", level="DEBUG")

        # Calculate PV headroom for PV-aware logic
        pv_available = system_state['solar_input']
        current_charging = max(0, system_state['battery_power'])  # Positive = charging
        pv_headroom = pv_available - current_charging

        for tool_name in tool_sequence:
            if abs(battery_flow_change) < 1.0:  # Close enough to target
                break

            if tool_name == 'charging_adjustment':
                action = self._handle_charging_adjustment(system_state, battery_flow_change, pv_headroom, mode)
                if action:
                    self.log_if_enabled(f"DEBUG charging_adjustment output: action={action.get('action')}, remaining={action.get('remaining', 0):.0f}W", level="DEBUG")
                    if action['action'] is not None:
                        actions.append(action['action'])
                    battery_flow_change = action.get('remaining', battery_flow_change)

            elif tool_name == 'forced_charging':
                # Proceed with forced charging
                action = self._handle_forced_charging(system_state, battery_flow_change, mode)
                if action:
                    self.log_if_enabled(f"DEBUG forced_charging output: action={action.get('action')}, remaining={action.get('remaining', 0):.0f}W", level="DEBUG")
                    if action['action'] is not None:
                        actions.append(action['action'])
                    battery_flow_change = action.get('remaining', battery_flow_change)

            elif tool_name == 'forced_discharging':
                # Proceed with forced discharge
                action = self._handle_forced_discharging(system_state, battery_flow_change, mode)
                if action:
                    if action['action'] is not None:
                        actions.append(action['action'])
                    battery_flow_change = action.get('remaining', battery_flow_change)

            elif tool_name == 'export_limitation':
                action = self._handle_export_limitation(battery_flow_change, mode)
                if action:
                    actions.append(action)
                    battery_flow_change = 0  # Export limitation satisfies remaining need

            elif tool_name == 'discharge_limitation':
                # flow_change is already inverted for FRRDOWN in main loop
                flow_for_discharge = battery_flow_change
                self.log_if_enabled(f"DEBUG discharge_limitation input: mode={mode}, battery_flow_change={battery_flow_change:.0f}W, flow_for_discharge={flow_for_discharge:.0f}W", level="DEBUG")
                action = self._handle_discharge_limitation(system_state, flow_for_discharge)
                if action:
                    self.log_if_enabled(f"DEBUG discharge_limitation output: action={action.get('action')}, remaining={action.get('remaining', 0):.0f}W", level="DEBUG")
                    if action['action'] is not None:
                        actions.append(action['action'])
                        self.log_if_enabled(f"DEBUG discharge_limitation: action={action['action'].description()}, remaining={action.get('remaining', battery_flow_change):.1f}W", level="DEBUG")
                    # CRITICAL: In FRRDOWN, need to convert 'remaining' back to negated form
                    battery_flow_change = remaining_from_tool
                else:
                    self.log_if_enabled(f"DEBUG discharge_limitation: returned None", level="DEBUG")

            elif tool_name == 'load_switching':
                # For FRRDOWN, load_switching needs the original positive value (positive = need more import)
                # For FRRUP, load_switching needs negative value (negative = need more export)
                # NOTE: battery_flow_change is already negated for frrdown in the main loop!
                # So we simply pass battery_flow_change directly.
                flow_for_switching = battery_flow_change
                
                # Calculate available battery capacity to handle overshoot
                # Charge capacity: How much MORE can we charge? (Limit - Current)
                # Note: Battery power is positive for charging, negative for discharging
                # Example: Limit 5000, Current -2000. Cap = 5000 - (-2000) = 7000.
                limit_charge = system_state.get('charging_rate_limit', self.config.max_battery_power)
                current_power = system_state.get('battery_power', 0)
                available_charge_capacity = limit_charge - current_power
                
                # Discharge capacity: How much MORE can we discharge? (Current + Limit)
                # Example: Limit 5000, Current 2000. Cap = 2000 + 5000 = 7000.
                limit_discharge = system_state.get('discharging_rate_limit', self.config.max_battery_power)
                available_discharge_capacity = limit_discharge + current_power
                
                action = self.tools['load_switching'].get_proposed_action(
                    flow_for_switching, 
                    mode, 
                    available_charge_capacity=available_charge_capacity,
                    available_discharge_capacity=available_discharge_capacity,
                    reason=f"Load switching for {mode}"
                )
                
                if action['action']:
                    actions.append(action['action'])
                    
                    remaining_from_tool = action.get('remaining', flow_for_switching)
                    battery_flow_change = remaining_from_tool

        return actions

    def _handle_charging_adjustment(self, system_state, flow_change, pv_headroom, mode='normal'):
        """
        Handle charging adjustment tool logic - bidirectional.
        
        Positive flow_change = increase charging (surplus)
        Negative flow_change = reduce charging to free up power (deficit)
        
        In deficit mode: Calculate charging limit based on actual current battery power.
        Set limit = max(0, current_battery_power - energy_deficit)
        
        FRRDOWN mode: Skip in deficit - forced_charging needs max limit maintained
        """
        current_charge_limit = system_state['charging_rate_limit']
        current_forced_flow = system_state['forced_power_flow']
        
        # FRRDOWN mode: Don't reduce charging limit in deficit (forced_charging needs max limit)
        # FRRUP mode: Don't reduce charging limit when discharging (allow opportunistic charging)
        if mode == 'frrdown' and flow_change < 0:
            # FRRDOWN deficit: Keep charging limit at maximum for forced_charging
            if current_charge_limit < self.config.max_battery_power:
                return {
                    'action': ChargingAdjustmentAction(
                        target_rate=self.config.max_battery_power,
                        reason=f"FRRDOWN deficit: maintain max charging limit for forced charging"
                    ),
                    'remaining': flow_change
                }
            return {'action': None, 'remaining': flow_change}
        
        if flow_change > 0:
            # SURPLUS: Too much export, increase charging to absorb excess
            # (or in normal modes: excess PV available, increase charging)
            
            # CRITICAL: Do not increase charging while forced discharge is active
            # Wait for forced discharge to be canceled (reach 0W) first
            if current_forced_flow < 0:  # Negative = discharging
                self.log_if_enabled(f"DEBUG charging_adjustment surplus: skipping, forced discharge active ({current_forced_flow:.0f}W)", level="DEBUG")
                return {'action': None, 'remaining': flow_change}
            
            increase_amount = min(flow_change, self.config.max_battery_power - current_charge_limit)
            self.log_if_enabled(f"DEBUG charging_adjustment surplus: flow_change={flow_change:.0f}W, current_limit={current_charge_limit}W, increase_amount={increase_amount:.0f}W, minimum={self.config.minimum_charging_adjustment_watts}W", level="DEBUG")
            if increase_amount >= self.config.minimum_charging_adjustment_watts:
                new_limit = current_charge_limit + increase_amount
                return {
                    'action': ChargingAdjustmentAction(
                        target_rate=int(new_limit),
                        reason=f"Surplus: increase charging to absorb {increase_amount:.0f}W"
                    ),
                    'remaining': flow_change - increase_amount
                }
            self.log_if_enabled(f"DEBUG charging_adjustment surplus: action=None (increase {increase_amount:.0f}W < minimum {self.config.minimum_charging_adjustment_watts}W)", level="DEBUG")
            return {'action': None, 'remaining': flow_change}
            
        elif flow_change < 0:
            # DEFICIT scenario: Reduce charging based on actual current battery power
            energy_deficit = abs(flow_change)
            current_battery_power = system_state['battery_power']
            
            # If battery is discharging, set charging limit to 0 immediately
            # (can't be charging if battery is discharging)
            if current_battery_power < 0:
                new_limit = 0
                reduction_achieved = 0  # Already discharging, reducing limit doesn't help now
                remaining_deficit = energy_deficit
            elif current_battery_power > 0:
                # Battery is charging, reduce from actual charging power
                new_limit = max(0, current_battery_power - energy_deficit)
                reduction_achieved = min(current_battery_power, energy_deficit)
                remaining_deficit = energy_deficit - reduction_achieved
            else:
                # Battery is idle (0W), reduce from charge limit
                new_limit = max(0, current_charge_limit - energy_deficit)
                reduction_achieved = min(current_charge_limit, energy_deficit)
                remaining_deficit = energy_deficit - reduction_achieved
            
            # Check if this reduction is significant enough
            if abs(new_limit - current_charge_limit) >= self.config.minimum_charging_adjustment_watts:
                return {
                    'action': ChargingAdjustmentAction(
                        target_rate=int(new_limit),
                        reason=f"Deficit: reduce charging limit to {new_limit:.0f}W (frees {reduction_achieved:.0f}W)"
                    ),
                    'remaining': -remaining_deficit if remaining_deficit > 0 else 0
                }
            
            # Change too small or already at minimum, pass to next tool
            return {'action': None, 'remaining': flow_change}
            
        return None

    def _handle_forced_discharging(self, system_state, flow_change, mode='normal'):
        """
        Handle forced discharging tool logic - bidirectional or absolute.
        
        For buy/sell modes: flow_change is absolute target (set to exact value)
        For other modes: flow_change is delta (add/subtract)
        
        Positive flow_change = reduce discharge (surplus) OR absolute target to discharge at
        Negative flow_change = increase discharge (deficit)
        """
        if system_state['battery_soc'] <= self.config.battery_soc_minimum_for_discharging:
            return None

        current_forced_flow = system_state['forced_power_flow']
        current_discharge = max(0, -current_forced_flow)
        discharging_limit = system_state['discharging_rate_limit']

        # Buy/Sell modes: flow_change is absolute target
        if mode in ['buy', 'sell']:
            target_power = abs(flow_change)
            minimum = self.config.minimum_discharge_change_watts
            
            # Check if already at target
            if abs(current_discharge - target_power) < minimum:
                self.log_if_enabled(f"Forced discharging absolute target: {target_power:.0f}W (mode={mode}) - already at target, action=None, remaining=0W", level="DEBUG")
                return {'action': None, 'remaining': 0}
            
            # Set to absolute target
            self.log_if_enabled(f"Forced discharging absolute target: {target_power:.0f}W (mode={mode}) - setting discharge", level="DEBUG")
            return {
                'action': ForcedDischargingAction(
                    target_power=int(target_power),
                    reason=f"{mode.capitalize()} mode: set discharge to {target_power:.0f}W"
                ),
                'remaining': 0  # Absolute target consumes entire request
            }

        # Delta adjustment logic for other modes
        if flow_change > 0:
            # SURPLUS: Reduce discharge
            if current_discharge > 0:
                reduction = min(flow_change, current_discharge)
                # Always cancel forced discharge if we have surplus, even if below threshold
                # Otherwise small forced discharge (e.g., 6W) won't be canceled when threshold is 10W
                if reduction >= self.config.minimum_discharge_reduction_watts or reduction == current_discharge:
                    new_discharge = current_discharge - reduction
                    return {
                        'action': ForcedDischargingAction(
                            target_power=int(new_discharge),
                            reason=f"Surplus: reduce discharge by {reduction:.0f}W"
                        ),
                        'remaining': flow_change - reduction
                    }
            return {'action': None, 'remaining': flow_change}
            
        elif flow_change < 0:
            # DEFICIT: Increase discharge
            # CRITICAL: Do not increase discharge if current forced discharge is still ramping
            # Check if forced discharge command has been realized
            from pbr_tools import is_forced_power_realized
            if current_discharge > 0 and not is_forced_power_realized(self, self.data_manager):
                self.log_if_enabled(f"DEBUG forced_discharging deficit: skipping increase, current discharge {current_discharge:.0f}W still ramping", level="DEBUG")
                return {'action': None, 'remaining': flow_change}
            
            max_additional = min(
                self.config.max_battery_power - current_discharge,
                discharging_limit - current_discharge
            )
            increase_amount = min(abs(flow_change), max_additional)
            
            if increase_amount >= self.config.minimum_discharge_change_watts:
                new_discharge = current_discharge + increase_amount
                return {
                    'action': ForcedDischargingAction(
                        target_power=int(new_discharge),
                        reason=f"Deficit: increase discharge by {increase_amount:.0f}W"
                    ),
                    'remaining': flow_change + increase_amount  # Add back what we added
                }
            return {'action': None, 'remaining': flow_change}
            
        return None

    def _handle_forced_charging(self, system_state, flow_change, mode='normal'):
        """
        SIMPLE EXECUTOR: Set forced charging to target power level.
        
        For BUY/SELL modes: flow_change represents ABSOLUTE target (set to this value)
        For other modes: flow_change represents DELTA (add/subtract from current)
        
        Args:
            flow_change: For BUY: negative absolute target. For others: positive = reduce, negative = increase
            mode: Operating mode (buy/sell use absolute targets, others use deltas)
        
        Returns:
            dict with 'action' and 'remaining' flow_change
        """
        current_charging = max(0, system_state['forced_power_flow'])  # Positive = charging
        
        # BUY/SELL modes: flow_change is ABSOLUTE target, not delta
        if mode in ['buy', 'sell']:
            target_power = abs(flow_change)  # Convert to positive (buy passes negative)
            
            # Already at target? Skip
            if abs(current_charging - target_power) < self.config.minimum_charging_adjustment_watts:
                return {'action': None, 'remaining': 0}
            
            # Set to absolute target
            self.log_if_enabled(f"Forced charging absolute target: {target_power:.0f}W (mode={mode})", level="DEBUG")
            return {
                'action': ForcedChargingAction(
                    target_power=int(target_power),
                    reason=f"Set charging to {target_power:.0f}W (buy/sell mode)"
                ),
                'remaining': 0  # Consumed the entire request
            }
        
        # Normal modes: flow_change is DELTA (incremental adjustment)
        if flow_change > 0:
            # SURPLUS: Reduce charging from grid (reduce grid import)
            if current_charging > 0:
                reduction = min(flow_change, current_charging)
                if reduction >= self.config.minimum_charging_adjustment_watts:
                    target_power = current_charging - reduction
                    return {
                        'action': ForcedChargingAction(
                            target_power=int(target_power),
                            reason=f"Reduce grid import by {reduction:.0f}W"
                        ),
                        'remaining': flow_change - reduction
                    }
            return {'action': None, 'remaining': flow_change}
            
        elif flow_change < 0:
            # DEFICIT: Increase charging from grid
            max_additional = self.config.max_battery_power - current_charging
            increase_amount = min(abs(flow_change), max_additional)
            
            if increase_amount >= self.config.minimum_charging_adjustment_watts:
                target_power = current_charging + increase_amount
                self.log_if_enabled(f"Forced charging target: {target_power:.0f}W (adding {increase_amount:.0f}W)", level="DEBUG")
                return {
                    'action': ForcedChargingAction(
                        target_power=int(target_power),
                        reason=f"Increase grid import by {increase_amount:.0f}W"
                    ),
                    'remaining': flow_change + increase_amount
                }
            else:
                self.log_if_enabled(f"Forced charging: increase {increase_amount:.0f}W < minimum {self.config.minimum_charging_adjustment_watts}W", level="DEBUG")
            return {'action': None, 'remaining': flow_change}
            
        return None

    def _handle_export_limitation(self, flow_change, mode):
        """Handle export limitation tool logic - only for surplus in limitexport mode"""
        if flow_change > 0 and mode == 'limitexport':
            return ExportLimitationAction(
                target_limit=0,  # TODO: Calculate actual limit
                reason=f"Charging at maximum, need to limit export for {flow_change:.0f}W additional charging"
            )
        return None

    def _handle_discharge_limitation(self, system_state, flow_change):
        """
        Handle discharge limitation tool logic - bidirectional.
        
        Positive flow_change = reduce discharge (surplus OR need more grid import in FRRDOWN)
        Negative flow_change = increase discharge (deficit OR allow more export)
        """
        current_discharge_limit = system_state['discharging_rate_limit']
        current_battery_power = system_state['battery_power']
        current_discharge = max(0, -current_battery_power)  # Positive value for discharge
        
        if flow_change > 0:
            # SURPLUS or FRRDOWN: Reduce discharge limit to reduce discharge (force grid import)
            # Calculate new limit based on current discharge and how much we need to reduce
            reduction_needed = flow_change
            new_limit = max(0, current_discharge - reduction_needed)
            
            # Only take action if limit actually changes significantly
            if abs(new_limit - current_discharge_limit) >= self.config.minimum_charging_adjustment_watts:
                # How much does this actually help?
                limit_reduction = current_discharge_limit - new_limit
                # Effective help: can only help if battery is actually discharging
                # If battery is charging/idle, limiting discharge prepares but doesn't help immediately
                effective_help = min(limit_reduction, reduction_needed, current_discharge)
                
                return {
                    'action': DischargeLimitationAction(
                        target_limit=int(new_limit),
                        reason=f"Reduce discharge limit by {limit_reduction:.0f}W (helps {effective_help:.0f}W immediately, prepares for future)"
                    ),
                    'remaining': flow_change - effective_help  # Subtract what we freed
                }
            # No meaningful change - pass to next tool
            return {'action': None, 'remaining': flow_change}
            
        elif flow_change < 0:
            # DEFICIT: Increase discharge limit to allow more discharge
            increase_amount = min(abs(flow_change), self.config.max_battery_power - current_discharge_limit)
            if increase_amount >= self.config.minimum_charging_adjustment_watts:
                new_limit = current_discharge_limit + increase_amount
                return {
                    'action': DischargeLimitationAction(
                        target_limit=int(new_limit),
                        reason=f"Increase grid export by {increase_amount:.0f}W (allow more discharge)"
                    ),
                    'remaining': flow_change + increase_amount
                }
            return {'action': None, 'remaining': flow_change}
            
        return None

    def log_desired_state_decisions(self, system_state, desired_state, mode):
        """Log the calculated desired state and decision reasoning - compact format"""
        # Extract key values
        phases = system_state['phases']
        forced_power_flow = system_state.get('forced_power_flow', 0)
        energy_flow = desired_state.energy_flow
        
        # Get mode/source info for context
        qw_source = self.data_manager.get_sensor_value(self.config.qw_source_sensor, use_fallback=True)
        
        # Build compact log line: mode(source) | [phases] | batt | forced | chg_lim | dis_lim | soc% | pv | loads | dflow
        parts = [
            f"{mode}({qw_source})",
            f"[{phases[0]:.0f},{phases[1]:.0f},{phases[2]:.0f}]",
            f"{system_state['battery_power']:.0f}W",
        ]
        
        # Add forced flow if active
        if forced_power_flow != 0:
            parts.append(f"F:{forced_power_flow:.0f}W")
        
        # Add charging and discharging limits
        parts.append(f"C:{system_state['charging_rate_limit']:.0f}W")
        parts.append(f"D:{system_state['discharging_rate_limit']:.0f}W")
        
        # Add SOC
        parts.append(f"{system_state['battery_soc']:.0f}%")
        
        # Add PV
        parts.append(f"PV:{system_state['solar_input']:.0f}W")
        
        # Add loads if active
        heating = system_state.get('heating_active')
        boiler = system_state.get('boiler_active')
        if heating or boiler:
            load_parts = []
            if heating:
                load_parts.append("H")
            if boiler:
                load_parts.append("B")
            parts.append(f"L:{'+'.join(load_parts)}")
        
        # Add flow change if non-zero
        if energy_flow.battery_flow_change != 0:
            parts.append(f"d:{energy_flow.battery_flow_change:.0f}W")
        
        # Join and log
        self.log_if_enabled(" | ".join(parts) + f" -> {desired_state.reasoning}")

    def log_proposed_actions(self, actions, mode):
        """Log the proposed tool actions in read-only mode"""
        if not actions:
            return

        self.log_if_enabled("PROPOSED TOOL ACTIONS (Read-Only):")
        for i, action in enumerate(actions, 1):
            self.log_if_enabled(f"  {i}. {action['tool']}: {action['action']}")
            self.log_if_enabled(f"     Reason: {action['reason']}")
        self.log_if_enabled("-" * 60)


    def determine_current_mode(self, qw_mode, qw_source, qw_powerlimit=None):
        """Determine current operating mode from QW sensors with source validation"""

        # Standard QW mode mapping
        internal_mode = ModeManager.map_qw_mode(qw_mode)
        
        # Validate mode exists
        if not internal_mode or not ModeManager.is_valid_mode(internal_mode):
            self.log_if_enabled(f"UNKNOWN QW MODE: '{qw_mode}' (source: '{qw_source}', powerlimit: {qw_powerlimit}), skipping phase balancing")
            return None  # Unknown mode, skip balancing
        
        # Validate source is compatible with mode
        if not ModeManager.is_valid_source_for_mode(internal_mode, qw_source):
            self.log_if_enabled(f"INVALID SOURCE FOR MODE: mode='{internal_mode}', source='{qw_source}' (powerlimit: {qw_powerlimit}), skipping phase balancing")
            return None  # Invalid source for this mode, skip balancing
        
        return internal_mode

    def _safe_numeric_format(self, value, label, unit, decimals=1):
        """Safely format a numeric value for logging, handling None/invalid values"""
        try:
            if value is None:
                return f"{label}=None {unit}"
            if isinstance(value, (int, float)):
                format_str = f"{{:.{decimals}f}}"
                return f"{label}={format_str.format(value)} {unit}"
            else:
                # Non-numeric value
                return f"{label}={value} {unit}"
        except Exception:
            return f"{label}=ERROR {unit}"

    def _get_current_mode_for_fast_trigger(self) -> Optional[str]:
        """Get current mode for fast trigger validation (without logging)."""
        qw_mode = self.data_manager.get_sensor_value(self.config.qw_mode_sensor, use_fallback=True)
        qw_source = self.data_manager.get_sensor_value(self.config.qw_source_sensor, use_fallback=True)
        qw_powerlimit = self.data_manager.get_sensor_value(self.config.qw_powerlimit_sensor, use_fallback=True)
        return self.determine_current_mode(qw_mode, qw_source, qw_powerlimit)

    def terminate(self):
        """Cancel all AppDaemon handles registered by this app instance."""
        # Clean up fast trigger listeners
        if hasattr(self, 'fast_trigger') and self.fast_trigger:
            self.fast_trigger.cleanup()
        
        if hasattr(self, "_ad_handles"):
            for typ, handle in list(self._ad_handles):
                try:
                    if typ == "listen_state":
                        self.cancel_listen_state(handle)
                    elif typ in ("timer", "run_every", "run_in", "run_at"):
                        self.cancel_timer(handle)
                except Exception as e:
                    self.log_if_enabled(f"Failed to cancel handle {typ}: {e}", level="WARNING")

    def log_if_enabled(self, *args, **kwargs):
        """Log only if logging is enabled, but always log ERROR and WARNING levels."""
        # Always log ERROR and WARNING
        if kwargs.get('level') in ['ERROR', 'WARNING']:
            self.log(*args, **kwargs)
        # Check for DEBUG level - requires both LOGGING_ENABLED and debug_logging config
        elif kwargs.get('level') == 'DEBUG':
            if self.LOGGING_ENABLED and self.config.debug_logging:
                self.log(*args, **kwargs)
        # Regular INFO logs - just need LOGGING_ENABLED
        elif self.LOGGING_ENABLED:
            self.log(*args, **kwargs)

    def on_logging_toggle(self, entity, attribute, old, new, kwargs):
        """Handle toggling of the logging boolean."""
        self.LOGGING_ENABLED = (new == "on")
        if self.LOGGING_ENABLED:
            self.log("PBR Logging enabled")
        else:
            self.log("PBR Logging disabled")