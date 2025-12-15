"""
pbr_load_switching_tool.py - Load switching tool for mFRR modes

Integrates loads.py modules into PBR for mFRR frequency regulation.
"""

from typing import Dict, Optional, List
from loads_config import LoadDevice
from loads_scheduler import LoadsScheduler


class LoadSwitchingTool:
    """
    Tool for switching loads ON/OFF during mFRR modes
    
    - FRRUP: Turn OFF loads to free power for grid export
    - FRRDOWN: Turn ON loads to consume power from grid
    """
    
    def __init__(self, hass, scheduler: LoadsScheduler, devices: List[LoadDevice]):
        self.hass = hass
        self.scheduler = scheduler
        self.devices = devices
        self.log = hass.log
    
    def execute(self, power_needed: float, mode: str, reason: str) -> Dict:
        """
        Execute load switching based on power requirement
        
        Args:
            power_needed: Power to adjust (negative = need more export, positive = need more import)
            mode: 'frrup' or 'frrdown'
            reason: Logging explanation
            
        Returns:
            Dict with action results and remaining power
        """
        if mode == "frrup" and power_needed < 0:
            # FRRUP: Need to export more - turn OFF loads
            return self._handle_frrup(abs(power_needed), reason)
        
        elif mode == "frrdown" and power_needed > 0:
            # FRRDOWN: Need to import more - turn ON loads
            return self._handle_frrdown(power_needed, reason)
        
        return {
            'action': None,
            'power_freed': 0,
            'remaining': power_needed
        }
    
    def _handle_frrup(self, power_needed: float, reason: str) -> Dict:
        """
        FRRUP mode: Turn OFF loads to free power for grid export
        
        Args:
            power_needed: How much power to free (positive value)
            
        Returns:
            Dict with loads switched and power freed
        """
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
                    self.log(f"Cannot turn OFF {device.name}: commitment not met ({slots_done}/{slots_needed} slots)")
                    continue
            
            available_loads.append(device)
        
        # Sort by power (largest first - free most power with fewest switches)
        available_loads.sort(key=lambda x: x.estimated_power, reverse=True)
        
        power_freed = 0
        loads_switched = []
        
        for device in available_loads:
            if power_freed >= power_needed:
                break
            
            # Turn OFF
            self.hass.call_service("homeassistant/turn_off", entity_id=device.entity_id)
            self.log(f"FRRUP: Turned OFF {device.name} ({device.estimated_power}W) - {reason}")
            
            loads_switched.append(device.name)
            power_freed += device.estimated_power
        
        return {
            'action': 'load_switching',
            'loads': loads_switched,
            'power_freed': power_freed,
            'remaining': -(power_needed - power_freed)  # Negative = still need more export
        }
    
    def _handle_frrdown(self, power_needed: float, reason: str) -> Dict:
        """
        FRRDOWN mode: Turn ON loads to consume power from grid
        
        Args:
            power_needed: How much power to consume (positive value)
            
        Returns:
            Dict with loads switched and power consumed
        """
        available_loads = []
        
        for device in self.devices:
            if not device.scheduling_enabled:
                continue
            
            # Check if device is currently OFF
            entity_state = self.hass.get_state(device.entity_id)
            is_on = entity_state == "on"
            
            if is_on:
                continue
            
            # Can turn ON if within scheduled hours or safe to override
            available_loads.append(device)
        
        # Sort by power (largest first - consume most power with fewest switches)
        available_loads.sort(key=lambda x: x.estimated_power, reverse=True)
        
        power_consumed = 0
        loads_switched = []
        
        for device in available_loads:
            if power_consumed >= power_needed:
                break
            
            # Turn ON
            self.hass.call_service("homeassistant/turn_on", entity_id=device.entity_id)
            self.log(f"FRRDOWN: Turned ON {device.name} ({device.estimated_power}W) - {reason}")
            
            loads_switched.append(device.name)
            power_consumed += device.estimated_power
        
        return {
            'action': 'load_switching',
            'loads': loads_switched,
            'power_consumed': power_consumed,
            'remaining': power_needed - power_consumed  # Positive = still need more import
        }
    
    def _get_current_slot(self) -> int:
        """Get current 15-minute slot index (0-95)"""
        from datetime import datetime
        now = datetime.now()
        return (now.hour * 60 + now.minute) // 15
    
    def can_switch_off(self, device_name: str) -> bool:
        """Check if a device can be switched OFF (for FRRUP)"""
        device = next((d for d in self.devices if d.name == device_name), None)
        if not device:
            return False
        
        # Check commitment if weather-adjusted
        if device.weather_adjustment and device.desired_on_hours:
            current_slot = self._get_current_slot()
            slots_done = sum(1 for i, scheduled in enumerate(device.scheduled_slots) if scheduled and i < current_slot)
            slots_needed = device.desired_on_hours * 4
            
            return slots_done >= slots_needed
        
        return True
    
    def can_switch_on(self, device_name: str) -> bool:
        """Check if a device can be switched ON (for FRRDOWN)"""
        device = next((d for d in self.devices if d.name == device_name), None)
        if not device:
            return False
        
        # Generally safe to turn ON (won't violate schedules)
        return device.scheduling_enabled
