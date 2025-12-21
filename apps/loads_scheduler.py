"""
loads_scheduler.py - Schedule calculation and Shelly API integration

STRICT REQUIREMENT: No pbr.py dependencies - reusable module
"""

import requests
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any
import pytz

from loads_config import LoadDevice, GlobalConfig, ScheduleMode
from loads_prices import LoadsPriceManager, PriceSlot
from loads_weather import LoadsWeatherManager


class LoadsScheduler:
    """
    Coordinates schedule calculation and Shelly API integration
    """
    
    def __init__(self, devices: List[LoadDevice], global_config: GlobalConfig,
                 price_manager: LoadsPriceManager, logger):
        self.devices = devices
        self.config = global_config
        self.prices = price_manager
        self.log = logger
        self.tz = pytz.timezone(global_config.timezone)
        self.avg_temp = None  # Store average temperature from last forecast
        
        # Initialize weather manager
        self.weather = LoadsWeatherManager(
            latitude=global_config.latitude,
            longitude=global_config.longitude,
            timezone=global_config.timezone
        )
    
    def calculate_daily_schedule(self, is_manual: bool = False) -> Dict[str, Any]:
        """
        Calculate schedule for next 24 hours (or today if manual/startup)
        
        Args:
            is_manual: If True, calculate for today from 22:00 yesterday
                       If False, calculate for tomorrow from 00:00
        
        Returns:
            Results dictionary with success status and details
        """
        now = datetime.now(self.tz)
        
        if is_manual:
            # For manual/startup: calculate today's schedule (from 22:00 yesterday to 22:00 today)
            target_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self.log(f"Calculating schedule for TODAY (manual/startup): {target_date.strftime('%Y-%m-%d')}")
        else:
            # For scheduled run: calculate tomorrow's schedule
            target_date = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self.log(f"Calculating schedule for TOMORROW (scheduled): {target_date.strftime('%Y-%m-%d')}")
        
        tomorrow = target_date  # Keep variable name for compatibility
        
        try:
            # Fetch prices
            price_slots = self.prices.fetch_prices_for_date(tomorrow)
            self.price_slots = price_slots  # Store for logging
            
            # Calculate schedule for each device
            results = {}
            for device in self.devices:
                if not device.scheduling_enabled:
                    continue
                
                schedule = self._calc_device_schedule(device, price_slots)
                results[device.name] = schedule
                
                # Update device state
                device.scheduled_slots = schedule['slots']
                
                self.log(f"{device.name}: {sum(schedule['slots'])} slots scheduled")
            
            # Create Shelly schedules
            shelly_results = self._create_shelly_schedules(results)
            
            return {
                'success': True,
                'date': tomorrow.strftime('%Y-%m-%d'),
                'devices': len(results),
                'total_slots': sum(sum(r['slots']) for r in results.values()),
                'shelly': shelly_results,
                'price_stats': self.prices.get_price_stats(price_slots)
            }
        
        except Exception as e:
            self.log(f"Schedule calculation failed: {e}", level="ERROR")
            return {'success': False, 'error': str(e)}
    
    def _apply_slot_constraints(self, device: LoadDevice, prices: List[PriceSlot]) -> None:
        """
        Apply always_on and always_off constraints to price slots
        
        This modifies the PriceSlot objects to mark which slots have constraints.
        always_off takes precedence over always_on.
        """
        always_on_hours = device.parse_always_on_hours()
        always_off_hours = device.parse_always_off_hours()
        
        always_on_count = 0
        always_off_count = 0
        
        # Apply always_on constraints
        if always_on_hours:
            for calendar_hour in always_on_hours:
                # Convert calendar hour to slot index (slot 0 = 22:00)
                slot_offset = ((calendar_hour - 22) % 24) * 4
                # Mark all 4 slots for this hour
                for i in range(4):
                    slot_idx = slot_offset + i
                    if slot_idx < len(prices):
                        prices[slot_idx].always_on = True
                        always_on_count += 1
            
            self.log(f"{device.name}: Always ON hours: {always_on_hours} ({always_on_count} slots)")
        
        # Apply always_off constraints (overrides always_on)
        if always_off_hours:
            for calendar_hour in always_off_hours:
                slot_offset = ((calendar_hour - 22) % 24) * 4
                for i in range(4):
                    slot_idx = slot_offset + i
                    if slot_idx < len(prices):
                        prices[slot_idx].always_off = True
                        prices[slot_idx].always_on = False  # Override always_on
                        always_off_count += 1
            
            self.log(f"{device.name}: Always OFF hours: {always_off_hours} ({always_off_count} slots blocked)")
    
    def _calc_device_schedule(self, device: LoadDevice, 
                             prices: List[PriceSlot]) -> Dict[str, Any]:
        """Calculate 96-slot schedule for one device
        
        Slot indexing: slot 0 = 22:00, slot 8 = 00:00, slot 95 = 21:45
        """
        # Create a deep copy of prices for this device to avoid cross-contamination
        from copy import deepcopy
        device_prices = deepcopy(prices)
        
        # Apply always_on/off constraints to this device's price slots
        self._apply_slot_constraints(device, device_prices)
        
        # Apply always_on_price constraint
        if device.always_on_price is not None:
            price_threshold_count = 0
            for idx, price_slot in enumerate(device_prices):
                # Check if price is below threshold (convert total_price to cents)
                if (price_slot.total_price * 100) < device.always_on_price:
                    # Only enable if not explicitly forced OFF
                    if not price_slot.always_off:
                        # Mark as always_on so it's treated same as time-based always-on
                        price_slot.always_on = True
                        price_threshold_count += 1
            
            if price_threshold_count > 0:
                self.log(f"{device.name}: Price threshold < {device.always_on_price}c/kWh enabled {price_threshold_count} slots")
        
        # Initialize result slots
        slots = [False] * 96
        
        # Get weather-adjusted slots if enabled
        num_slots_per_period = None
        weather_adjusted = False
        
        if device.weather_adjustment and device.desired_on_hours is not None:
            # Calculate required slots based on weather
            # For weather adjustment, we calculate for the entire period first
            num_slots_total = self.weather.get_heating_requirement(
                heating_curve=device.heating_curve,
                power_factor=device.power_factor,
                period_hours=device.period_hours,
                min_slots=device.desired_on_hours * 4
            )
            weather_adjusted = True
            heating_hours = num_slots_total // 4
            self.log(f"{device.name}: Weather adjustment: {num_slots_total} slots required per period (config minimum {device.desired_on_hours * 4})")
            num_slots_per_period = num_slots_total
            # Get forecast temp for logging
            forecast = self.weather.fetch_forecast(device.period_hours)
            if forecast:
                self.avg_temp = forecast.avg_temperature  # Store for dashboard
                self.log(f"{device.name}: Temperature forecast with windchill is {int(forecast.avg_temperature)} °C, and heating enabled for {heating_hours} hours per {device.period_hours}h period")
        
        # Calculate schedule based on mode
        if device.schedule_mode == ScheduleMode.PERIOD:
            # Determine how many slots we need per period
            if not num_slots_per_period and device.desired_on_hours is not None:
                num_slots_per_period = device.desired_on_hours * 4
            
            if num_slots_per_period:
                # Calculate number of periods in a day
                num_periods = 24 // device.period_hours if device.period_hours > 0 else 1
                slots_per_period = (96 // num_periods) if num_periods > 0 else 96
                
                self.log(f"{device.name}: Period scheduling - {num_periods} periods of {device.period_hours}h each, {num_slots_per_period} slots per period")
                
                # First, enable all always_on slots
                always_on_total = sum(1 for p in device_prices if p.always_on)
                for idx, price_slot in enumerate(device_prices):
                    if price_slot.always_on:
                        slots[idx] = True
                
                # Process each period separately
                total_added = 0
                for period_idx in range(num_periods):
                    period_start_slot = period_idx * slots_per_period
                    period_end_slot = (period_idx + 1) * slots_per_period
                    
                    # Get prices for this period, excluding always_off slots
                    period_prices = []
                    for idx in range(period_start_slot, period_end_slot):
                        if idx < len(device_prices) and not device_prices[idx].always_off:
                            period_prices.append(device_prices[idx])
                    
                    # Count already-on slots in this period (from always_on)
                    already_on = sum(slots[period_start_slot:period_end_slot])
                    
                    if device.weather_adjustment:
                        # Weather mode: always_on counts toward requirement
                        remaining_slots = max(0, num_slots_per_period - already_on)
                    else:
                        # Non-weather mode: always_on is extra
                        remaining_slots = num_slots_per_period
                    
                    if remaining_slots > 0 and len(period_prices) > 0:
                        # Get cheapest slots within this period
                        try:
                            cheapest = self.prices.get_cheapest_slots(
                                period_prices, remaining_slots,
                                device.min_price_rank, device.max_price_rank
                            )
                        except Exception as e:
                            self.log(f"{device.name}: Error getting cheapest slots for period {period_idx+1}: {e}", level="ERROR")
                            self.log(f"  period_prices length: {len(period_prices)}, remaining_slots: {remaining_slots}", level="ERROR")
                            raise
                        
                        # Enable selected slots (using slot_index from PriceSlot)
                        added = 0
                        for relative_idx in cheapest:
                            if relative_idx < len(period_prices):
                                absolute_idx = period_prices[relative_idx].slot_index
                                if not slots[absolute_idx]:
                                    added += 1
                                slots[absolute_idx] = True
                        
                        total_added += added
                        # Convert slot index to actual hour (slot 0 = 22:00)
                        period_start_hour = ((period_start_slot // 4) + 22) % 24
                        period_end_hour = ((period_end_slot // 4) + 22) % 24
                        self.log(f"{device.name}: Period {period_idx+1} (slot {period_start_slot}-{period_end_slot-1}: {period_start_hour:02d}:00-{period_end_hour:02d}:00) - added {added} new slots")
                
                if not device.weather_adjustment:
                    self.log(f"{device.name}: Total added {total_added} new slots ({always_on_total} from always-on)")
        
        elif device.schedule_mode == ScheduleMode.THRESHOLD:
            # First enable always_on slots
            for idx, price_slot in enumerate(device_prices):
                if price_slot.always_on:
                    slots[idx] = True
            
            # Run when price rank <= max_price_rank, excluding always_off
            if device.max_price_rank:
                available_prices = [p for p in device_prices if not p.always_off]
                threshold_slots = self.prices.get_cheapest_slots(
                    available_prices, len(available_prices), None, device.max_price_rank
                )
                for relative_idx in threshold_slots:
                    if relative_idx < len(available_prices):
                        absolute_idx = available_prices[relative_idx].slot_index
                        slots[absolute_idx] = True
        
        # Count constraint statistics
        always_on_count = sum(1 for p in device_prices if p.always_on)
        
        return {
            'slots': slots,
            'mode': device.schedule_mode.value,
            'count': sum(slots),
            'weather_adjusted': weather_adjusted,
            'always_on_count': always_on_count
        }
    
    def _clear_shelly_schedules(self, device_name: str, shelly_ip: str) -> bool:
        """Clear all existing schedules on a Shelly device"""
        try:
            # Shelly supports up to 10 schedules, clear all
            for schedule_id in range(10):
                url = f"http://{shelly_ip}/rpc/Schedule.Delete"
                data = {"id": schedule_id}
                try:
                    response = requests.post(url, json=data, timeout=5)
                    time.sleep(self.config.shelly_delay_between_deletes)
                    # Don't fail if schedule doesn't exist
                except:
                    pass  # Schedule might not exist, that's fine
            
            self.log(f"{device_name}: Cleared existing schedules")
            return True
        except Exception as e:
            self.log(f"{device_name}: Failed to clear schedules: {e}", level="WARNING")
            return False
    
    def _create_shelly_schedules(self, device_schedules: Dict[str, Dict]) -> Dict[str, Any]:
        """Create Shelly schedules for all devices"""
        shelly_results = {
            'created': 0,
            'devices_updated': 0,
            'errors': []
        }
        
        for device_name, result in device_schedules.items():
            device = next((d for d in self.devices if d.name == device_name), None)
            if not device:
                continue
            
            # Clear existing schedules first
            if device.shelly_ip:
                self._clear_shelly_schedules(device_name, device.shelly_ip)
            
            try:
                schedule_ids = self._create_device_schedules(
                    device, result['slots']
                )
                device.schedule_ids = schedule_ids
                shelly_results['created'] += len(schedule_ids)
                shelly_results['devices_updated'] += 1
                
                self.log(f"Created {len(schedule_ids)} Shelly schedules for {device_name}")
                
                # Log ALL 15-minute heating slots with prices
                heating_slots = []
                for slot_idx, is_on in enumerate(result['slots']):
                    if is_on and hasattr(self, 'price_slots'):
                        # Convert slot index to hour and minute (slot 0 = 22:00)
                        hour = ((slot_idx // 4) + 22) % 24
                        minute = (slot_idx % 4) * 15
                        
                        # Get price for this slot
                        if slot_idx < len(self.price_slots):
                            price_slot = self.price_slots[slot_idx]
                            # Convert from €/kWh to c/kWh for display
                            heating_slots.append((hour, minute, price_slot.total_price * 100))
                
                if heating_slots:
                    heating_slots.sort()  # Sort by hour and minute
                    slots_str = ', '.join([f"{h:02d}:{m:02d} ({p:.2f})" for h, m, p in heating_slots])
                    self.log(f"{device_name}: Heating slots 'HH:mm (c/kWh Price+Network)': {slots_str}")
            
            except Exception as e:
                error_msg = f"{device_name}: {str(e)}"
                shelly_results['errors'].append(error_msg)
                self.log(f"Shelly API failed for {device_name}: {e}", level="ERROR")
        
        return shelly_results
    
    def _create_device_schedules(self, device: LoadDevice, 
                                slots: List[bool]) -> Dict[str, int]:
        """
        Create 4 Shelly schedules for device (one per 15-min offset)
        
        Returns:
            Dict mapping slot names to schedule IDs
        """
        # Delete old schedules first
        self._delete_old_schedules(device)
        
        # Configure auto-off once for the device (before creating schedules)
        try:
            auto_cfg = {
                "id": 0,
                "config": {
                    ("auto_on" if device.inverted_logic else "auto_off"): True,
                    ("auto_on_delay" if device.inverted_logic else "auto_off_delay"): 910
                }
            }
            requests.post(
                f"http://{device.shelly_ip}/rpc/Switch.SetConfig",
                json=auto_cfg, timeout=5
            ).raise_for_status()
            time.sleep(self.config.shelly_delay_between_creates)  # Give device time to apply config
        except Exception as e:
            raise Exception(f"Failed to configure auto-off: {e}")
        
        # Group slots by minute offset
        slot_hours = {0: [], 15: [], 30: [], 45: []}
        
        for idx, is_on in enumerate(slots):
            if is_on:
                # Convert slot index to actual wall-clock hour (slot 0 = 22:00)
                hour = ((idx // 4) + 22) % 24
                minute = (idx % 4) * 15
                slot_hours[minute].append(hour)
        
        # Create one schedule per minute offset
        schedule_ids = {}
        for minute, hours in slot_hours.items():
            if hours:
                slot_name = f"slot_{minute}"
                sched_id = self._create_shelly_schedule(
                    device.shelly_ip, slot_name, hours, minute,
                    inverted=device.inverted_logic
                )
                schedule_ids[slot_name] = sched_id
                time.sleep(self.config.shelly_delay_between_creates)
        
        return schedule_ids
    
    def _delete_old_schedules(self, device: LoadDevice):
        """Delete existing Shelly schedules"""
        for slot_name, sched_id in device.schedule_ids.items():
            try:
                url = f"http://{device.shelly_ip}/rpc/Schedule.Delete"
                requests.post(url, json={"id": sched_id}, timeout=5)
            except:
                pass  # Ignore errors
    
    def _create_shelly_schedule(self, ip: str, name: str, hours: List[int],
                               minute: int, inverted: bool = False) -> int:
        """
        Create schedule via Shelly Gen2 API
        
        Returns:
            Schedule ID
        """
        # Build timespec
        hours_str = ",".join(map(str, sorted(set(hours))))
        timespec = f"0 {minute} {hours_str} * * *"
        
        # Create schedule
        payload = {
            "enable": True,
            "timespec": timespec,
            "calls": [{
                "method": "Switch.Set",
                "params": {"id": 0, "on": not inverted}
            }]
        }
        
        resp = requests.post(
            f"http://{ip}/rpc/Schedule.Create",
            json=payload, timeout=5
        )
        resp.raise_for_status()
        result = resp.json()
        
        if "id" not in result:
            raise Exception(f"No schedule ID in response: {result}")
        
        return result["id"]
