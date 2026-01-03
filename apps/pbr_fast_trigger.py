"""
Fast Phase Trigger Module

Event-driven phase monitoring to trigger early control loop execution
when large loads are detected (two consecutive readings ≤-300W).

This improves response time for large load additions from 15-20s to 14-16s.
"""

import time
from collections import deque
from typing import Callable, Dict, Optional


class FastPhaseTrigger:
    """
    Fast phase monitoring trigger for early control loop activation.
    
    Subscribes to phase sensors and triggers control loop when detecting
    sustained high loads (two consecutive readings ≤ threshold).
    
    Features:
    - Per-phase history buffers (deque, maxlen=2)
    - Two-reading filter to ignore transient loads
    - 10s minimum interval between any control loop executions
    - Only active in balancing modes (normal, limitexport, pvsell)
    - Natural debounce from 2-3s sensor updates + lockout
    
    Architecture:
    - Callback-based: Calls back to pbr.py's control loop
    - No state duplication: Uses shared last_execution_time
    - Easy to disable: Just don't initialize this module
    """
    
    def __init__(
        self,
        hass_instance,
        trigger_callback: Callable,
        get_mode_callback: Callable,
        config,
        get_last_execution_callback: Callable,
        get_soc_callback: Optional[Callable] = None,
        get_heating_active_callback: Optional[Callable] = None,
        log_if_enabled_callback: Optional[Callable] = None,
    ):
        """
        Initialize fast phase trigger.
        
        Args:
            hass_instance: AppDaemon Hass instance for state listeners
            trigger_callback: Function to call when trigger conditions met (control_loop)
            get_mode_callback: Function to get current operating mode
            config: Config class with fast trigger settings
            get_last_execution_callback: Function to get timestamp of last control loop execution
            get_soc_callback: Function to get current battery SOC % (optional)
            log_if_enabled_callback: Function to log messages (respects logging toggle)
        """
        self.hass = hass_instance
        self.trigger_callback = trigger_callback
        self.get_mode_callback = get_mode_callback
        self.get_last_execution = get_last_execution_callback
        self.get_soc_callback = get_soc_callback
        # Optional callback that returns True when heating is active
        self.get_heating_active = get_heating_active_callback
        # Optional callback for logging (respects logging toggle)
        self.log_if_enabled = log_if_enabled_callback if log_if_enabled_callback else self.hass.log
        self.config = config
        
        # Per-phase history buffers (store last 2 readings)
        self.phase_history: Dict[str, deque] = {
            'phase_a': deque(maxlen=2),
            'phase_b': deque(maxlen=2),
            'phase_c': deque(maxlen=2)
        }
        
        # Track listeners for cleanup
        self._listeners = []
        
        # Feature flag
        self.enabled = config.fast_trigger_enabled
        
        if self.enabled:
            self.log_if_enabled(f"Fast trigger initialized: threshold={config.fast_trigger_threshold}W, "
                               f"interval={config.fast_trigger_minimum_interval}s, "
                               f"modes={config.fast_trigger_balancing_modes}")
    
    def on_phase_update(self, entity, attribute, old, new, kwargs):
        """
        Callback for phase sensor updates.
        
        Updates history buffer and checks if trigger conditions are met.
        """
        if not self.enabled:
            return
        
        # Determine which phase this is
        phase_name = None
        if entity == self.config.phases_sensor[0]:
            phase_name = 'phase_a'
        elif entity == self.config.phases_sensor[1]:
            phase_name = 'phase_b'
        elif entity == self.config.phases_sensor[2]:
            phase_name = 'phase_c'
        else:
            return  # Unknown sensor
        
        # Parse new value
        try:
            phase_value = float(new)
        except (ValueError, TypeError):
            return  # Invalid value
        
        # Update history buffer
        self.phase_history[phase_name].append(phase_value)
        
        # Check if trigger conditions are met
        if self.should_trigger(phase_name, phase_value):
            # If heating is active, suppress both logging and the callback to
            # minimize spam and avoid unnecessary control loop runs.
            try:
                heating_active = False
                if self.get_heating_active:
                    heating_active = bool(self.get_heating_active())
                if heating_active:
                    if self.config.log_triggers:
                        # User requested suppression of this specific log
                        # self.log_if_enabled(f"FAST TRIGGER SUPPRESSED: {phase_name}={phase_value:.0f}W "
                        #                   f"(heating active)")
                        pass
                    return
            except Exception:
                # If heating check fails, fall through and allow triggering
                pass

            # Log trigger event
            history = list(self.phase_history[phase_name])
            if self.config.log_triggers:
                self.log_if_enabled(f"FAST TRIGGER: {phase_name}={phase_value:.0f}W "
                                   f"(history: {history}) -> triggering control loop")

            # Call trigger callback
            self.trigger_callback(source=f"fast_trigger_{phase_name}")
    
    def should_trigger(self, phase_name: str, phase_value: float) -> bool:
        """
        Determine if trigger conditions are met.
        
        Conditions:
        1. Two consecutive readings in buffer
        2. Both readings ≤ threshold (-300W)
        3. Current mode is a balancing mode
        4. Minimum interval since last execution has passed
        
        Returns:
            True if should trigger control loop
        """
        # Check buffer has 2 readings
        history = self.phase_history[phase_name]
        if len(history) < 2:
            return False
        
        # Check both readings are ≤ threshold
        threshold = self.config.fast_trigger_threshold
        if not all(reading <= threshold for reading in history):
            return False
        
        # Check current mode is a balancing mode
        current_mode = self.get_mode_callback()
        if current_mode not in self.config.fast_trigger_balancing_modes:
            return False
        
        # Check minimum interval since last execution
        last_execution = self.get_last_execution()
        time_since_last = time.time() - last_execution
        if time_since_last < self.config.fast_trigger_minimum_interval:
            # Too soon - still in lockout period
            if self.config.log_triggers:
                # User requested suppression of this specific log
                # self.log_if_enabled(f"FAST TRIGGER: {phase_name} conditions met but in lockout "
                #                   f"({time_since_last:.1f}s < {self.config.fast_trigger_minimum_interval}s)")
                pass
            return False
        
        # All conditions met
        return True
    
    def subscribe(self):
        """
        Subscribe to phase sensor updates.
        
        Registers state listeners for all three phase sensors.
        Only subscribes if SOC is above minimum threshold.
        """
        if not self.enabled:
            return
        
        # Check if already subscribed
        if self._listeners:
            return  # Already active
        
        # Check SOC before subscribing (avoid unnecessary listeners when battery too low)
        if self.get_soc_callback:
            soc = self.get_soc_callback()
            if soc is not None and soc <= self.config.battery_soc_minimum_for_discharging:
                # Battery too low - don't subscribe yet
                return
        
        # Subscribe to all three phase sensors
        for i, phase_entity in enumerate(self.config.phases_sensor):
            phase_name = ['phase_a', 'phase_b', 'phase_c'][i]
            try:
                listener = self.hass.listen_state(self.on_phase_update, phase_entity)
                self._listeners.append(listener)
                self.log_if_enabled(f"Fast trigger subscribed to {phase_name}: {phase_entity}")
            except Exception as e:
                self.log_if_enabled(f"ERROR: Failed to subscribe to {phase_entity}: {e}", level="ERROR")
    
    def unsubscribe(self):
        """
        Unsubscribe from phase sensor updates.
        
        Cancels all registered state listeners (e.g., when battery SOC too low).
        """
        if not self._listeners:
            return  # Nothing to unsubscribe
        
        for listener in self._listeners:
            try:
                self.hass.cancel_listen_state(listener)
            except Exception as e:
                self.log_if_enabled(f"Failed to cancel fast trigger listener: {e}", level="WARNING")
        
        self._listeners = []
        self.log_if_enabled("Fast trigger unsubscribed (battery SOC too low)")
    
    def update_subscription(self):
        """
        Update subscription based on current SOC.
        
        Subscribes if SOC > minimum, unsubscribes if SOC <= minimum.
        Call this periodically (e.g., in main control loop).
        """
        if not self.enabled:
            return
        
        if not self.get_soc_callback:
            return  # No SOC check - keep current state
        
        soc = self.get_soc_callback()
        if soc is None:
            return  # No SOC data
        
        should_be_subscribed = soc > self.config.battery_soc_minimum_for_discharging
        is_subscribed = bool(self._listeners)
        
        if should_be_subscribed and not is_subscribed:
            # SOC rose above threshold - subscribe
            self.subscribe()
        elif not should_be_subscribed and is_subscribed:
            # SOC fell below threshold - unsubscribe
            self.unsubscribe()
    
    def cleanup(self):
        """
        Clean up listeners on shutdown.
        
        Cancels all registered state listeners.
        """
        for listener in self._listeners:
            try:
                self.hass.cancel_listen_state(listener)
            except Exception as e:
                self.log_if_enabled(f"Failed to cancel fast trigger listener: {e}", level="WARNING")
        self._listeners = []
