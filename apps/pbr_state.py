from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import pytz
from pbr_config import Config
from pbr_modes import ModeManager


class Constraint(Enum):
    """System constraints that limit or block certain operations"""
    BATTERY_SOC_TOO_LOW = "battery_soc_too_low_for_discharge"
    HEATING_ACTIVE = "load_protection_heating_active"
    BOILER_OUTSIDE_HOURS = "load_protection_boiler_active_outside_daytime_hours"
    BOILER_DAYTIME = "load_protection_boiler_active_during_daytime_hours"


@dataclass
class EnergyFlow:
    """Represents desired energy flow adjustments"""
    battery_flow_change: float  # Positive = increase charging, Negative = increase discharging
    export_limit: Optional[float]  # If set, limit inverter export to this value


@dataclass
class DesiredState:
    """Complete desired system state with reasoning"""
    target_phase: float           # Target phase balance value
    energy_flow: EnergyFlow       # Energy flow adjustments
    range_low: Optional[float]    # Acceptable low range
    range_high: Optional[float]   # Acceptable high range
    constraints: List[Constraint]  # Active constraints considered
    reasoning: str                # Human-readable explanation


class StateEngine:
    """
    Calculates desired system state based on current state and mode.
    
    This is the "brain" that determines what the system should do
    based on phase balance, battery state, and operating mode.
    """
    
    def __init__(self, data_manager):
        """
        Initialize the State Engine.
        
        Args:
            data_manager: DataManager instance for accessing sensor data
        """
        self.data_manager = data_manager

    def calculate_desired_state(self, system_state: dict, mode: str) -> Optional[DesiredState]:
        """
        Calculate the desired system state using DesiredState dataclass

        Args:
            system_state: Current system state from data manager
            mode: Operating mode (normal, limitexport, pvsell, nobattery, savebattery, buy, sell, frrup, frrdown)

        Returns:
            DesiredState object or None if calculation fails
        """
        # Get basic state information
        most_negative = system_state['most_negative']
        target_phase = self._get_target_phase()
        
        if target_phase is None:
            return None
        
        # Get range values
        range_low, range_high = self._get_range_values()
        
        # Check if within range - if so, no adjustment needed
        if range_low is not None and range_high is not None and range_low <= most_negative <= range_high:
            return DesiredState(
                target_phase=target_phase,
                energy_flow=EnergyFlow(battery_flow_change=0, export_limit=None),
                range_low=range_low,
                range_high=range_high,
                constraints=[],
                reasoning=f"Phase {most_negative:.1f}W is within range [{range_low:.1f}, {range_high:.1f}], no adjustment needed."
            )
        
        # Calculate power balance
        power_balance = most_negative - target_phase
        total_power_adjustment = power_balance * 3
        
        # Determine constraints
        constraints = self._calculate_constraints(system_state)
        
        # Calculate energy flow based on mode
        energy_flow, reasoning = self._calculate_mode_energy_flow(mode, system_state, total_power_adjustment)
        
        # Apply SOC minimum constraint - block discharge when battery too low
        # EXEMPT mFRR modes (frrup/frrdown) - they are grid regulation services that must always work
        if Constraint.BATTERY_SOC_TOO_LOW in constraints and not ModeManager.is_mfrr_mode(mode):
            # Battery too low - block any discharge (positive battery_flow_change)
            if energy_flow.battery_flow_change > 0:
                energy_flow.battery_flow_change = 0
                reasoning = f"Battery SOC too low ({system_state['battery_soc']:.0f}% < {Config.battery_soc_minimum_for_discharging:.0f}%): discharge blocked."
            else:
                # Allow charging (negative flow)
                if reasoning:
                    reasoning += f" (Battery SOC {system_state['battery_soc']:.0f}% - charging allowed)"
        
        # Apply load protection constraints - ONLY block discharge, allow charging
        # EXEMPT mFRR modes - they are grid regulation services that must always work
        elif (Constraint.HEATING_ACTIVE in constraints or Constraint.BOILER_OUTSIDE_HOURS in constraints) and not ModeManager.is_mfrr_mode(mode):
            # Block discharge (positive flow_change) but allow charging (negative flow_change)
            if energy_flow.battery_flow_change > 0:
                energy_flow.battery_flow_change = 0
                if reasoning:
                    reasoning += " Load protection active: battery discharge blocked."
            else:
                if reasoning:
                    reasoning += " Load protection active: battery discharge blocked (charging allowed)."
        
        return DesiredState(
            target_phase=target_phase,
            energy_flow=energy_flow,
            range_low=range_low,
            range_high=range_high,
            constraints=constraints,
            reasoning=reasoning
        )
    
    def _get_target_phase(self) -> Optional[float]:
        """Get target phase value from HA input"""
        target_phase_raw = self.data_manager.get_sensor_value(Config.phase_target_input, use_fallback=True)
        try:
            return float(target_phase_raw) if target_phase_raw is not None else None
        except (ValueError, TypeError):
            if hasattr(self.data_manager, 'log'):
                self.data_manager.log(f"Error converting target_phase: {target_phase_raw}", level="WARNING")
            return None
    
    def _get_range_values(self) -> tuple[Optional[float], Optional[float]]:
        """Get range low and high values from HA inputs"""
        range_low_raw = self.data_manager.get_sensor_value(Config.phase_range_low_input, use_fallback=True)
        range_high_raw = self.data_manager.get_sensor_value(Config.phase_range_high_input, use_fallback=True)
        
        try:
            range_low = float(range_low_raw) if range_low_raw is not None else None
            range_high = float(range_high_raw) if range_high_raw is not None else None
            return range_low, range_high
        except (ValueError, TypeError):
            if hasattr(self.data_manager, 'log'):
                self.data_manager.log(f"Error converting range values: low={range_low_raw}, high={range_high_raw}", level="WARNING")
            return None, None
    
    def _calculate_constraints(self, system_state: dict) -> List[Constraint]:
        """Calculate active constraints based on system state"""
        constraints = []
        
        if system_state['battery_soc'] < Config.battery_soc_minimum_for_discharging:
            constraints.append(Constraint.BATTERY_SOC_TOO_LOW)
        
        if system_state['heating_active']:
            constraints.append(Constraint.HEATING_ACTIVE)
        
        if system_state['boiler_active']:
            # Check time-based boiler constraint (7am-10pm Tallinn time)
            tallinn_tz = pytz.timezone(Config.timezone)
            now = datetime.now(tallinn_tz)
            hour = now.hour
            if not (Config.boiler_allowed_start_hour <= hour < Config.boiler_allowed_end_hour):
                constraints.append(Constraint.BOILER_OUTSIDE_HOURS)
            else:
                constraints.append(Constraint.BOILER_DAYTIME)
        
        return constraints
    
    def _calculate_mode_energy_flow(self, mode: str, system_state: dict, net_power_adjustment: float) -> tuple[EnergyFlow, str]:
        """Calculate energy flow and reasoning based on mode"""
        if ModeManager.is_fixed_power_mode(mode):
            return self._calculate_fixed_power_mode(mode, system_state)
        elif ModeManager.is_mfrr_mode(mode):
            return self._calculate_mfrr_mode(mode, system_state)
        elif mode in ['nobattery', 'savebattery']:
            return self._calculate_conservative_mode(mode)
        elif mode == 'pvsell':
            return self._calculate_pvsell_mode(net_power_adjustment)
        elif mode == 'limitexport':
            return self._calculate_limitexport_mode(net_power_adjustment)
        else:  # normal mode
            return self._calculate_normal_mode(system_state, net_power_adjustment)
    
    def _calculate_fixed_power_mode(self, mode: str, system_state: dict) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for buy/sell modes"""
        qw_powerlimit = self.data_manager.get_sensor_value(Config.qw_powerlimit_sensor, use_fallback=True)
        try:
            fixed_power = float(qw_powerlimit) if qw_powerlimit is not None else 0
        except (ValueError, TypeError):
            if hasattr(self.data_manager, 'log'):
                self.data_manager.log(f"Error converting qw_powerlimit: {qw_powerlimit}", level="WARNING")
            fixed_power = 0
        
        if mode == 'buy':
            return (
                EnergyFlow(battery_flow_change=-fixed_power, export_limit=None),
                f"Buy mode: Charging battery at fixed power {fixed_power:.0f}W from grid (no phase balancing)"
            )
        else:  # sell mode
            return (
                EnergyFlow(battery_flow_change=fixed_power, export_limit=None),
                f"Sell mode: Discharging battery at fixed power {fixed_power:.0f}W to grid (no phase balancing)"
            )
    
    def _calculate_mfrr_mode(self, mode: str, system_state: dict) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for mFRR frequency regulation modes
        
        For FRRUP: qw_powerlimit is positive for export (e.g., 300 = export 300W to grid)
        For FRRDOWN: qw_powerlimit is positive but means import (e.g., 300 = import 300W from grid, so target = -300W)
        
        Applies ±15W deadband to prevent unnecessary adjustments for minor fluctuations
        """
        qw_powerlimit = self.data_manager.get_sensor_value(Config.qw_powerlimit_sensor, use_fallback=True)
        try:
            raw_limit = float(qw_powerlimit) if qw_powerlimit is not None else 0
        except (ValueError, TypeError):
            if hasattr(self.data_manager, 'log'):
                self.data_manager.log(f"Error converting qw_powerlimit: {qw_powerlimit}", level="WARNING")
            raw_limit = 0
        
        # For FRRDOWN, negate the powerlimit since positive value means import (negative grid flow)
        if mode == 'frrdown':
            target_grid_flow = -raw_limit
        else:  # frrup
            target_grid_flow = raw_limit
        
        # Calculate current total grid flow (sum of all phases)
        current_grid_flow = sum(system_state['phases'])
        grid_flow_adjustment = target_grid_flow - current_grid_flow
        
        # Apply deadband: ignore adjustments within ±15W of target
        deadband = 15.0
        if abs(grid_flow_adjustment) < deadband:
            grid_flow_adjustment = 0
        
        if mode == 'frrup':
            reasoning = f"mFRR UP: T {target_grid_flow:.0f}W, C {current_grid_flow:.0f}W, D {grid_flow_adjustment:.0f}W"
            return (
                EnergyFlow(battery_flow_change=-grid_flow_adjustment, export_limit=None),
                reasoning
            )
        else:  # frrdown mode
            reasoning = f"mFRR DOWN: T {target_grid_flow:.0f}W, C {current_grid_flow:.0f}W, D {grid_flow_adjustment:.0f}W"
            return (
                EnergyFlow(battery_flow_change=-grid_flow_adjustment, export_limit=None),
                reasoning
            )
    
    def _calculate_conservative_mode(self, mode: str) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for conservative modes (nobattery/savebattery)"""
        return (
            EnergyFlow(battery_flow_change=0, export_limit=None),
            ""
        )
    
    def _calculate_pvsell_mode(self, net_power_adjustment: float) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for pvsell mode"""
        return (
            EnergyFlow(battery_flow_change=-max(0, -net_power_adjustment), export_limit=None),
            "PV Sell mode: Prioritizing export. Battery charge set to 0, discharge as needed for phase balance."
        )
    
    def _calculate_limitexport_mode(self, net_power_adjustment: float) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for limitexport mode"""
        if net_power_adjustment > 0:
            battery_flow_change = net_power_adjustment
        else:
            battery_flow_change = -(-net_power_adjustment)
        
        return (
            EnergyFlow(battery_flow_change=battery_flow_change, export_limit=None),
            "Limit Export mode: Prioritizing battery charging, then export limitation when charging maxed."
        )
    
    def _calculate_normal_mode(self, system_state: dict, net_power_adjustment: float) -> tuple[EnergyFlow, str]:
        """Calculate energy flow for normal balanced mode"""
        if net_power_adjustment > 0:
            if system_state['battery_power'] < 0:  # Currently discharging
                return (
                    EnergyFlow(battery_flow_change=net_power_adjustment, export_limit=None),
                    f"Normal mode: Balanced energy management. Reducing discharge. Net adjustment: {net_power_adjustment:.1f}W."
                )
            else:
                return (
                    EnergyFlow(battery_flow_change=net_power_adjustment, export_limit=None),
                    f"Normal mode: Balanced energy management. Increasing charging. Net adjustment: {net_power_adjustment:.1f}W."
                )
        else:
            return (
                EnergyFlow(battery_flow_change=net_power_adjustment, export_limit=None),
                f"Normal mode: Balanced energy management. Increasing discharging. Net adjustment: {net_power_adjustment:.1f}W."
            )
