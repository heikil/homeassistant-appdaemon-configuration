"""
Phase Balancer Rewrite (PBR) - Action Data Classes

Strongly-typed action classes for tool execution.
Each action contains structured parameters instead of text that needs parsing.
"""

from typing import Literal
from dataclasses import dataclass


@dataclass
class ChargingAdjustmentAction:
    """Action to adjust battery charging rate limit"""
    tool: Literal['charging_adjustment'] = 'charging_adjustment'
    target_rate: int = 0  # Target charging rate in watts (0-5000)
    reason: str = ""  # Human-readable reason for logging
    
    def description(self) -> str:
        """Generate human-readable description for logging"""
        return f"Set charging limit to {self.target_rate}W"


@dataclass
class DischargeLimitationAction:
    """Action to adjust battery discharge rate limit"""
    tool: Literal['discharge_limitation'] = 'discharge_limitation'
    target_limit: int = 0  # Target discharge limit in watts (0-5000)
    reason: str = ""
    
    def description(self) -> str:
        """Generate human-readable description for logging"""
        return f"Limit battery discharge to {self.target_limit}W"


@dataclass
class ForcedChargingAction:
    """Action to force charge battery from grid"""
    tool: Literal['forced_charging'] = 'forced_charging'
    target_power: int = 0  # Target charging power in watts (0-5000)
    reason: str = ""
    
    def description(self) -> str:
        """Generate human-readable description for logging"""
        return f"Force charge battery at {self.target_power}W from grid"


@dataclass
class ForcedDischargingAction:
    """Action to force discharge battery to grid"""
    tool: Literal['forced_discharging'] = 'forced_discharging'
    target_power: int = 0  # Target discharge power in watts (0-5000)
    stop: bool = False  # If True, stop forced discharging
    reason: str = ""
    
    def description(self) -> str:
        """Generate human-readable description for logging"""
        if self.stop:
            return "Stop forced discharging"
        return f"Force discharge battery at {self.target_power}W to grid"


@dataclass
class ExportLimitationAction:
    """Action to limit inverter export to grid"""
    tool: Literal['export_limitation'] = 'export_limitation'
    target_limit: int = 0  # Target export limit in watts (0-8800)
    reason: str = ""
    
    def description(self) -> str:
        """Generate human-readable description for logging"""
        return f"Set export limit to {self.target_limit}W"


# Union type for all possible actions
Action = (
    ChargingAdjustmentAction |
    DischargeLimitationAction |
    ForcedChargingAction |
    ForcedDischargingAction |
    ExportLimitationAction
)
