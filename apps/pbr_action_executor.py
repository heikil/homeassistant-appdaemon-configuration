"""
Phase Balancer Rewrite (PBR) - Action Executor Module

This module handles the execution of proposed actions using structured action classes.

Responsibilities:
- Execute actions using strongly-typed action objects
- Map actions to tool method calls
- Handle execution errors gracefully
- Provide clear logging of execution results
"""

from typing import Dict, List
from pbr_actions import (
    Action,
    ChargingAdjustmentAction,
    DischargeLimitationAction,
    ForcedChargingAction,
    ForcedDischargingAction,
    ExportLimitationAction,
    LoadSwitchingAction
)


class ActionExecutor:
    """
    Executes proposed actions using tool instances.
    
    Takes strongly-typed action objects and executes them using the appropriate tools.
    """
    
    def __init__(self, hass_instance, tools: Dict):
        """
        Initialize the Action Executor.
        
        Args:
            hass_instance: AppDaemon Hass instance for logging
            tools: Dictionary of tool instances {'tool_name': tool_instance}
        """
        self.hass = hass_instance
        self.tools = tools
        self.active_actions = []
        
    def execute_actions(self, actions: List[Action], mode: str) -> None:
        """
        Execute the proposed actions using tools.
        
        Args:
            actions: List of strongly-typed action objects
            mode: Current operating mode (for logging context)
        """
        # Reset active actions for this cycle
        self.active_actions = []

        for action in actions:
            # Store for API visibility
            self.active_actions.append(action.description())

            # Log the action description and reason
            self.hass.log_if_enabled(f"{action.description()} (reason: {action.reason})")
            
            try:
                # Get the tool instance
                if action.tool not in self.tools:
                    self.hass.log_if_enabled(f"Tool {action.tool} not found in tools dictionary", level="WARNING")
                    continue
                
                tool = self.tools[action.tool]
                
                # Execute based on action type
                if isinstance(action, ChargingAdjustmentAction):
                    tool.execute(action.target_rate, reason=action.reason)
                
                elif isinstance(action, DischargeLimitationAction):
                    tool.execute(action.target_limit, reason=action.reason)
                
                elif isinstance(action, ForcedChargingAction):
                    if action.target_power == 0:
                        tool.stop(reason=action.reason)
                    else:
                        tool.execute(action.target_power, reason=action.reason)
                
                elif isinstance(action, ForcedDischargingAction):
                    if action.stop:
                        tool.stop(reason=action.reason)
                    else:
                        tool.execute(action.target_power, emergency=False, reason=action.reason)
                
                elif isinstance(action, ExportLimitationAction):
                    tool.execute(action.target_limit, reason=action.reason)
                
                elif isinstance(action, LoadSwitchingAction):
                    tool.execute_action(action)
                
                else:
                    self.hass.log_if_enabled(f"Unknown action type: {type(action)}", level="WARNING")
            
            except Exception as e:
                self.hass.log_if_enabled(f"Error executing {action.tool}: {e}", level="ERROR")
