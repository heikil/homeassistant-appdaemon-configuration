from typing import Dict, Optional, List
from loads_config import LoadDevice
from pbr_actions import LoadSwitchingAction


class LoadSwitchingTool:
    """
    Tool for switching loads ON/OFF during mFRR modes
    
    - FRRUP: Turn OFF loads to free power for grid export
    - FRRDOWN: Turn ON loads to consume power from grid
    """
    
    def __init__(self, hass, devices: List[LoadDevice]):
        self.hass = hass
        self.devices = devices
        self.log = hass.log
    
    def get_proposed_action(self, power_needed: float, mode: str, reason: str) -> Dict:
        """
        Calculate proposed load switching action
        
        Args:
            power_needed: Power to adjust (negative = need more export, positive = need more import)
            mode: 'frrup' or 'frrdown'
            reason: Logging explanation
            
        Returns:
            Dict with 'action' (LoadSwitchingAction) and 'remaining' power
        """
        if mode == "frrup" and power_needed < 0:
            # FRRUP: Need to export more - turn OFF loads
            return self._plan_frrup(abs(power_needed), reason)
        
        elif mode == "frrdown" and power_needed > 0:
            # FRRDOWN: Need to import more - turn ON loads
            return self._plan_frrdown(power_needed, reason)
        
        return {
            'action': None,
            'remaining': power_needed
        }
    
    def execute_action(self, action: LoadSwitchingAction):
        """Execute the proposed action"""
        for device_name in action.loads:
            self.hass.call_service(
                "loads/override_device",
                device_name=device_name,
                turn_on=action.turn_on
            )
            self.log(f"LoadSwitchingTool: Switched {device_name} {'ON' if action.turn_on else 'OFF'} - {action.reason}")

    def _plan_frrup(self, power_needed: float, reason: str) -> Dict:
        """Plan FRRUP action (Turn OFF)"""
        available_loads = []
        
        for device in self.devices:
            if not device.scheduling_enabled:
                continue
            
            # Check if device is currently ON
            entity_state = self.hass.get_state(device.entity_id)
            is_on = entity_state == "on"
            
            if not is_on:
                continue
            
            # Check if we can turn it OFF (commitment met?)
            if device.weather_adjustment and device.desired_on_hours:
                # Check if heating commitment is met
                slots_done = sum(1 for i, scheduled in enumerate(device.scheduled_slots) if scheduled and i < self._get_current_slot())
                slots_needed = device.desired_on_hours * 4
                
                if slots_done < slots_needed:
                    # self.log(f"Cannot turn OFF {device.name}: commitment not met")
                    continue
            
            available_loads.append(device)
        
        # Sort by power (largest first)
        available_loads.sort(key=lambda x: x.estimated_power, reverse=True)
        
        power_freed = 0
        loads_switched = []
        
        for device in available_loads:
            # Select device if it fits within the remaining power needed
            # We prefer undershooting (and letting battery handle the rest) 
            # rather than overshooting (which would require counter-action)
            if device.estimated_power <= (power_needed - power_freed):
                loads_switched.append(device.name)
                power_freed += device.estimated_power
        
        if not loads_switched:
            return {'action': None, 'remaining': -power_needed}

        return {
            'action': LoadSwitchingAction(
                loads=loads_switched,
                turn_on=False,
                power_change=power_freed,
                reason=reason
            ),
            'remaining': -(power_needed - power_freed)
        }
    
    def _plan_frrdown(self, power_needed: float, reason: str) -> Dict:
        """Plan FRRDOWN action (Turn ON)"""
        available_loads = []
        
        for device in self.devices:
            if not device.scheduling_enabled:
                continue
            
            # Check if device is currently OFF
            entity_state = self.hass.get_state(device.entity_id)
            is_on = entity_state == "on"
            
            if is_on:
                continue
            
            available_loads.append(device)
        
        # Sort by power (largest first)
        available_loads.sort(key=lambda x: x.estimated_power, reverse=True)
        
        power_consumed = 0
        loads_switched = []
        
        for device in available_loads:
            # Select device if it fits within the remaining power needed
            # We prefer undershooting (and letting battery handle the rest) 
            # rather than overshooting (which would require counter-action)
            if device.estimated_power <= (power_needed - power_consumed):
                loads_switched.append(device.name)
                power_consumed += device.estimated_power
            
        if not loads_switched:
            return {'action': None, 'remaining': power_needed}
        
        return {
            'action': LoadSwitchingAction(
                loads=loads_switched,
                turn_on=True,
                power_change=power_consumed,
                reason=reason
            ),
            'remaining': power_needed - power_consumed
        }
    
    def _get_current_slot(self) -> int:
        """Get current 15-minute slot index (0-95, where 0 = 22:00 yesterday)"""
        from datetime import datetime
        now = datetime.now()
        # Calculate slot index from 00:00
        day_slot = (now.hour * 60 + now.minute) // 15
        # Adjust for 22:00 start (offset +8 slots)
        return (day_slot + 8) % 96

    def restore_state(self):
        """Restore devices to their scheduled state"""
        current_slot = self._get_current_slot()
        
        for device in self.devices:
            if not device.scheduling_enabled:
                continue
                
            # Determine desired state from schedule
            should_be_on = device.scheduled_slots[current_slot]
            
            # Get actual state
            entity_state = self.hass.get_state(device.entity_id)
            is_on = entity_state == "on"
            
            if is_on != should_be_on:
                self.log(f"Restoring {device.name} to {'ON' if should_be_on else 'OFF'} (Schedule mismatch)")
                self.hass.call_service(
                    "loads/override_device",
                    device_name=device.name,
                    turn_on=should_be_on
                )
