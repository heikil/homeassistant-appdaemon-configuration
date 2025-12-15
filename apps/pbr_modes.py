"""
Phase Balancer Rewrite (PBR) - Modes Module

This module contains mode-specific logic, tool priority configurations,
mode transition handling, and initial state definitions.
"""

import time
from typing import Dict, List, ClassVar, Optional, Literal, Any
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


class ModeManager:
    """Manages operating mode logic, tool sequences, and mode transitions"""

    # Mode initial states - applied when entering a mode
    # Values: 'maximum' (5000/8800W), 'zero' (0W), 'stop' (stop forced operations), 'keep' (don't change)
    mode_initial_states: ClassVar[Dict[str, Dict[str, Literal['maximum', 'zero', 'stop', 'keep']]]] = {
        'normal': {
            'export_limit': 'maximum',           # 8800W - full export capability
            'charging_limit': 'maximum',         # 5000W - full charging capability
            'discharge_limit': 'maximum',        # 5000W - full discharging capability
            'forced_charge_discharge': 'stop'    # Stop any forced operation
        },
        'limitexport': {
            'export_limit': 'keep',              # Mode will manage export limit
            'charging_limit': 'maximum',         # 5000W - full charging capability
            'discharge_limit': 'maximum',        # 5000W - full discharging capability
            'forced_charge_discharge': 'stop'    # Stop forced operations
        },
        'pvsell': {
            'export_limit': 'maximum',           # 8800W - need full export for selling PV
            'charging_limit': 'zero',            # 0W - prioritize grid export over battery charging
            'discharge_limit': 'maximum',        # 5000W - may need discharge for phase balancing
            'forced_charge_discharge': 'stop'    # Stop forced operations
        },
        'nobattery': {
            'export_limit': 'maximum',           # 8800W - full export
            'charging_limit': 'maximum',         # 5000W - allow charging from solar
            'discharge_limit': 'zero',           # 0W - no battery discharging
            'forced_charge_discharge': 'stop'    # Stop forced operations
        },
        'savebattery': {
            'export_limit': 'maximum',           # 8800W - full export
            'charging_limit': 'maximum',         # 5000W - can charge from solar
            'discharge_limit': 'zero',           # 0W - preserve battery (no discharging)
            'forced_charge_discharge': 'stop'    # Stop forced operations
        },
        'buy': {
            'export_limit': 'maximum',           # 8800W - full export
            'charging_limit': 'maximum',         # 5000W - CRITICAL: must be max for forced charging to work!
            'discharge_limit': 'maximum',        # 5000W - not restricting
            'forced_charge_discharge': 'stop'    # Will be started by mode logic
        },
        'sell': {
            'export_limit': 'maximum',           # 8800W - need full export for selling
            'charging_limit': 'maximum',         # 5000W - not restricting
            'discharge_limit': 'maximum',        # 5000W - CRITICAL: must be max for forced discharge to work!
            'forced_charge_discharge': 'stop'    # Will be started by mode logic
        },
        'frrup': {
            'export_limit': 'maximum',           # 8800W - full export capability
            'charging_limit': 'maximum',         # 5000W - CRITICAL: for forced charging capability
            'discharge_limit': 'maximum',        # 5000W - CRITICAL: for forced discharge capability
            'forced_charge_discharge': 'stop'    # Will be managed by mode logic
        },
        'frrdown': {
            'export_limit': 'maximum',           # 8800W - full export capability
            'charging_limit': 'maximum',         # 5000W - CRITICAL: for forced charging capability
            'discharge_limit': 'maximum',        # 5000W - CRITICAL: for forced discharge capability
            'forced_charge_discharge': 'stop'    # Will be managed by mode logic
        }
    }

    # Mode-specific tool execution sequences (priority order)
    mode_tool_sequences: ClassVar[Dict[str, List[str]]] = {
        'normal': ['charging_adjustment', 'forced_discharging'],
        'limitexport': ['charging_adjustment', 'export_limitation', 'forced_discharging'],
        'pvsell': ['charging_adjustment', 'forced_discharging'],
        'nobattery': ['forced_discharging', 'charging_adjustment'],  # Prioritize discharge over charging in nobattery mode
        'savebattery': ['charging_adjustment', 'forced_discharging'],  # Normal priority but may have different thresholds
        'buy': ['forced_charging'],  # Direct forced charge at qw_powerlimit - no phase balancing
        'sell': ['forced_discharging'],  # Direct forced discharge at qw_powerlimit - no phase balancing
        'frrup': ['load_switching', 'charging_adjustment', 'forced_discharging'],  # mFRR UP: Reduce charging first (free PV to grid), then discharge battery if needed
        'frrdown': ['load_switching', 'discharge_limitation', 'charging_adjustment', 'forced_charging']  # mFRR DOWN: Limit discharge to force grid import, increase charging from grid
    }

    # QW mode mappings are now in pbr_config.py (moved for better separation of data vs logic)

    @classmethod
    def get_tool_sequence(cls, mode: str, surplus: bool = False) -> List[str]:
        """Get tool execution sequence for the given mode

        Args:
            mode: Operating mode ('normal', 'limitexport', etc.)
            surplus: True for energy surplus (reverse sequence), False for deficit (normal sequence)

        Returns:
            List of tool names in execution priority order
        """
        base_sequence = cls.mode_tool_sequences.get(mode, ['charging_adjustment', 'forced_discharging'])

        # Reverse sequence for surplus scenarios (bidirectional logic)
        if surplus:
            return list(reversed(base_sequence))

        return base_sequence

    @classmethod
    def map_qw_mode(cls, qw_mode: str) -> str:
        """Map QW mode to internal mode using config"""
        return Config.qw_mode_mappings.get(qw_mode, qw_mode)  # Return original if no mapping found

    @classmethod
    def is_valid_mode(cls, mode: str) -> bool:
        """Check if mode is valid"""
        return mode in cls.mode_tool_sequences

    @classmethod
    def is_valid_source_for_mode(cls, mode: str, source: str) -> bool:
        """Check if source is valid for the given mode"""
        # mFRR modes only accept 'kratt' source
        if mode in Config.mfrr_modes:
            return source == Config.kratt_only_source
        # All other modes accept standard sources (not kratt)
        return source in Config.valid_qw_sources and source != Config.kratt_only_source

    @classmethod
    def is_mfrr_mode(cls, mode: str) -> bool:
        """Check if mode is an mFRR frequency regulation mode"""
        return mode in Config.mfrr_modes

    @classmethod
    def is_fixed_power_mode(cls, mode: str) -> bool:
        """Check if mode uses fixed power (buy/sell)"""
        return mode in ['buy', 'sell']

    @classmethod
    def get_available_modes(cls) -> List[str]:
        """Get list of all available modes"""
        return list(cls.mode_tool_sequences.keys())

    @classmethod
    def get_mode_description(cls, mode: str) -> str:
        """Get human-readable description of mode behavior"""
        descriptions = {
            'normal': 'Balanced energy management with minimal intervention',
            'limitexport': 'Maximize battery charging, limit export when charging maxed',
            'pvsell': 'Prioritize grid export over battery storage',
            'nobattery': 'Conservative mode, no battery discharge allowed',
            'savebattery': 'Preserve battery charge, no active balancing',
            'buy': 'Charge battery from grid at fixed power limit (no phase balancing)',
            'sell': 'Discharge battery to grid at fixed power limit (no phase balancing)',
            'frrup': 'mFRR UP: Increase grid export/reduce consumption for frequency regulation',
            'frrdown': 'mFRR DOWN: Increase grid import/consumption for frequency regulation'
        }
        return descriptions.get(mode, f'Unknown mode: {mode}')

    # ====================================================================================
    # Mode Transition Management
    # ====================================================================================

    def __init__(self, hass_instance, tools: Dict):
        """
        Initialize ModeManager instance for mode transition handling.

        Args:
            hass_instance: AppDaemon Hass instance
            tools: Dictionary of tool instances {'forced_charging': tool, ...}
        """
        self.hass = hass_instance
        self.tools = tools
        self.config = Config()
        
        # Transition state tracking
        self.current_mode: Optional[str] = None
        self.current_source: Optional[str] = None

    def handle_mode_change(self, new_mode: str, new_source: str) -> bool:
        """
        Detect and handle mode transitions.

        Args:
            new_mode: The new operating mode
            new_source: The new mode source

        Returns:
            bool: True if mode changed and transition was initiated
        """
        # Check if mode actually changed
        if new_mode == self.current_mode and new_source == self.current_source:
            return False

        # Log the transition
        if self.current_mode is None:
            self.hass.log_if_enabled(f"Initial mode: {new_mode} (source: {new_source})")
        else:
            self.hass.log_if_enabled(f"Mode transition: {self.current_mode} → {new_mode} (source: {new_source})")

        # Apply initial state for new mode
        self._apply_mode_initial_state(new_mode)

        # Update current mode and source
        self.current_mode = new_mode
        self.current_source = new_source

        return True

    def _execute_mode_primary_tool(self, mode: str) -> None:
        """
        Execute the primary tool immediately for modes that require it.
        This is called after initial state is applied to ensure buy/sell/mFRR modes
        start their operations without waiting for the next control loop cycle.
        
        Args:
            mode: Operating mode
        """
        # Get qw_powerlimit for buy/sell/mFRR modes
        qw_powerlimit = self.hass.get_state(self.config.qw_powerlimit_sensor, default=0)
        try:
            target_power = float(qw_powerlimit) if qw_powerlimit else 0
        except (ValueError, TypeError):
            self.hass.log_if_enabled(f"Invalid qw_powerlimit value: {qw_powerlimit}, using 0W", level="WARNING")
            target_power = 0
        
        # Execute mode-specific primary tool
        if mode == 'buy' and target_power > 0:
            if 'forced_charging' in self.tools:
                self.hass.log_if_enabled(f"  - Executing forced charging: {target_power:.0f}W (immediate mode entry)")
                self.tools['forced_charging'].execute(
                    target_power, 
                    reason=f"Mode transition to buy ({target_power:.0f}W)",
                    mode_transition=True
                )
        
        elif mode == 'sell' and target_power > 0:
            if 'forced_discharging' in self.tools:
                self.hass.log_if_enabled(f"  - Executing forced discharging: {target_power:.0f}W (immediate mode entry)")
                self.tools['forced_discharging'].execute(
                    target_power, 
                    reason=f"Mode transition to sell ({target_power:.0f}W)",
                    mode_transition=True
                )
        
        elif mode in ['frrup', 'frrdown']:
            # mFRR modes will be handled by control loop based on qw_powerlimit
            # We don't force execution here as they have complex multi-tool sequences
            pass
    
    def _apply_mode_initial_state(self, mode: str) -> None:
        """
        Apply initial state settings for a mode.

        Args:
            mode: Operating mode to initialize
        """
        initial_state = self.mode_initial_states.get(mode)
        if not initial_state:
            self.hass.log_if_enabled(f"No initial state defined for mode: {mode}", level="WARNING")
            return

        self.hass.log_if_enabled(f"Applying initial state for {mode} mode:")

        # 1. Stop forced charging/discharging first
        if initial_state['forced_charge_discharge'] == 'stop':
            forced_state = self.hass.get_state(self.config.battery_forced_charge_sensor, default="Unknown")
            if forced_state and (forced_state.startswith("Charging at") or forced_state.startswith("Discharging at")):
                # Use the tools to stop
                if 'forced_charging' in self.tools:
                    self.tools['forced_charging'].stop(reason=f"Mode transition to {mode}")
                if 'forced_discharging' in self.tools:
                    self.tools['forced_discharging'].stop(reason=f"Mode transition to {mode}")

        # 2. Set export limit
        if initial_state['export_limit'] == 'maximum' and 'export_limitation' in self.tools:
            self.tools['export_limitation'].reset_to_maximum(reason=f"Mode transition to {mode}")
            self.hass.log_if_enabled(f"  - Export limit: maximum (unlimited)")
        # 'keep' means don't change

        # 3. Set charging limit
        if 'charging_adjustment' in self.tools:
            if initial_state['charging_limit'] == 'maximum':
                self.tools['charging_adjustment'].reset_to_maximum(reason=f"Mode transition to {mode}")
            elif initial_state['charging_limit'] == 'zero':
                self.tools['charging_adjustment'].execute(0, reason=f"Mode transition to {mode}")
        # 'keep' means don't change

        # 4. Set discharge limit
        # Note: We don't have a DischargeLimitTool yet, so use direct service call
        if initial_state['discharge_limit'] == 'maximum':
            self.hass.call_service(
                "number/set_value",
                entity_id=self.config.battery_discharge_limit_sensor,
                value=self.config.max_battery_power,
                callback=service_call_callback
            )
            self.hass.log_if_enabled(f"  - Discharge limit: maximum ({self.config.max_battery_power}W)")
        elif initial_state['discharge_limit'] == 'zero':
            self.hass.call_service(
                "number/set_value",
                entity_id=self.config.battery_discharge_limit_sensor,
                value=0,
                callback=service_call_callback
            )
            self.hass.log_if_enabled(f"  - Discharge limit: zero (0W)")
        # 'keep' means don't change

        # TODO: Future - handle load switching during transitions
        # self._handle_load_switching(mode)

        self.hass.log_if_enabled(f"Initial state applied for {mode} mode")
        
        # 5. Execute primary tool immediately for fixed-power and mFRR modes
        # This ensures buy/sell modes start charging/discharging without waiting for next cycle
        self._execute_mode_primary_tool(mode)