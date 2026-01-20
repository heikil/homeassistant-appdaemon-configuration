"""
loads.py - Main load scheduling application

Standalone AppDaemon app with all configuration in Python classes.
Edit loads_config.py to configure devices and settings.

STRICT REQUIREMENT: No pbr.py dependencies
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import time, datetime, timedelta
import pytz
import json
import os
from collections import deque

from loads_config import GLOBAL_CONFIG, DEVICES, ScheduleMode
from loads_prices import LoadsPriceManager
from loads_scheduler import LoadsScheduler


class LoadSchedulingApp(hass.Hass):
    """
    Main load scheduling application
    
    Configuration is in loads_config.py - no apps.yaml edits needed
    """
    
    PERSISTENCE_FILE = "loads_api_data.json"
    
    def initialize(self):
        """Initialize the application"""
        self.log("=" * 60)
        self.log("Load Scheduling Application Starting")
        self.log("=" * 60)
        
        try:
            # Override config with secrets if provided
            if self.args.get('latitude'):
                GLOBAL_CONFIG.latitude = float(self.args['latitude'])
            if self.args.get('longitude'):
                GLOBAL_CONFIG.longitude = float(self.args['longitude'])

            # Validate configuration
            self._validate_config()
            
            # Initialize price manager
            self.price_manager = LoadsPriceManager(
                network_provider=GLOBAL_CONFIG.network_provider,
                electricity_package=GLOBAL_CONFIG.electricity_package,
                country=GLOBAL_CONFIG.country,
                timezone_str=GLOBAL_CONFIG.timezone
            )
            
            # Initialize scheduler
            self.scheduler = LoadsScheduler(
                devices=DEVICES,
                global_config=GLOBAL_CONFIG,
                price_manager=self.price_manager,
                logger=self.log
            )
            
            # History of last 20 recoveries
            self.recent_recoveries = deque(maxlen=20)
            
            # Load persistent data (energy debt) from disk
            self._load_persistence_data()
            
            # Schedule daily calculation using robust timezone handling
            self._schedule_next_run()
            
            # Schedule energy debt check every minute
            self.run_every(self._check_energy_debt, "now", 60)
            
            # Run immediately on startup if configured (Manual Run)
            if GLOBAL_CONFIG.run_on_startup:
                self.log("run_on_startup=True, triggering immediate calculation...")
                self.run_in(lambda kwargs: self._daily_callback(kwargs, is_manual=True), 2)
            
            # Register services
            self.register_service(
                "loads/recalculate",
                self._service_recalculate
            )
            
            self.register_service(
                "loads/get_status",
                self._service_status
            )
            
            self.register_service(
                "loads/enable_device",
                self._service_enable_device
            )
            
            self.register_service(
                "loads/disable_device",
                self._service_disable_device
            )
            
            self.register_service(
                "loads/override_device",
                self._service_override_device
            )
            
            self.register_service(
                "loads/reset_debt",
                self._service_reset_debt
            )
            
            # Register dashboard API endpoints
            self.register_endpoint(self._dashboard_api, "load_scheduler_data")
            self.register_endpoint(self._api_reset_debt, "load_scheduler_reset_debt")
            
            self.log(f"Initialized with {len(DEVICES)} devices")
            self.log(f"Daily schedule at {GLOBAL_CONFIG.schedule_time}")
            self.log(f"Network provider: {GLOBAL_CONFIG.network_provider} {GLOBAL_CONFIG.electricity_package.upper()}")
            self.log(f"Country: {GLOBAL_CONFIG.country}")
            self.log(f"Timezone: {GLOBAL_CONFIG.timezone}")
            self.log("=" * 60)
        
        except Exception as e:
            self.log(f"INITIALIZATION FAILED: {e}", level="ERROR")
            raise
    
    def _validate_config(self):
        """Validate configuration"""
        errors = []
        
        if not DEVICES:
            errors.append("No devices configured in loads_config.py")
        
        for device in DEVICES:
            if not isinstance(device.schedule_mode, ScheduleMode):
                errors.append(f"{device.name}: invalid schedule_mode")
            
            if device.schedule_mode == ScheduleMode.PERIOD and device.desired_on_hours is None:
                errors.append(f"{device.name}: period mode needs desired_on_hours (can be 0 for weather-only)")
            
            if device.schedule_mode == ScheduleMode.PERIOD:
                # Validate period_hours
                if device.period_hours not in [24, 12, 8, 6, 4, 3, 2, 1]:
                    errors.append(f"{device.name}: period_hours must be a divisor of 24 (1,2,3,4,6,8,12,24)")
                
                # Validate desired_on_hours doesn't exceed period length
                if device.desired_on_hours and device.desired_on_hours > device.period_hours:
                    errors.append(f"{device.name}: desired_on_hours ({device.desired_on_hours}) cannot exceed period_hours ({device.period_hours})")
            
            if device.schedule_mode == ScheduleMode.THRESHOLD and not device.max_price_rank:
                errors.append(f"{device.name}: threshold mode needs max_price_rank")
        
        if errors:
            raise ValueError("Configuration errors: " + "; ".join(errors))
    
    def _schedule_next_run(self):
        """Schedule next run avoiding pytz LMT 21-minute offset bug"""
        try:
            tz = pytz.timezone(GLOBAL_CONFIG.timezone)
            now = datetime.now(tz)
            hour, minute = map(int, GLOBAL_CONFIG.schedule_time.split(':'))
            
            # Create target for today
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If target is in the past, schedule for tomorrow
            if target <= now:
                target += timedelta(days=1)
                
            # Normalize to handle DST transitions correctly
            target = tz.normalize(target)
            
            self.run_at(self._daily_callback, target, is_scheduled=True)
            self.log(f"Next scheduled run: {target.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        except Exception as e:
            self.log(f"Failed to schedule next run: {e}", level="ERROR")
    
    def _daily_callback(self, kwargs, is_manual=False):
        """Daily schedule calculation callback"""
        
        # Reschedule for the next day if this was a scheduled run
        if kwargs.get('is_scheduled'):
            self._schedule_next_run()
            is_manual = False  # Ensure it's treated as scheduled

        self.log(f"Starting daily schedule calculation (Manual: {is_manual})")
        
        try:
            result = self.scheduler.calculate_daily_schedule(is_manual=is_manual)
            
            if result['success']:
                self.log("Schedule calculation SUCCESS")
                self.log(f"  Date: {result['date']}")
                self.log(f"  Devices: {result['devices']}")
                self.log(f"  Total slots: {result['total_slots']}")
                self.log(f"  Shelly schedules created: {result['shelly']['created']}")
                
                # Store calculation timestamp
                self.schedule_calculated_at = self.datetime().isoformat()
                
                # Update HA sensors
                self._update_sensors(result)
                
                # Save API data to disk
                self._save_api_data_to_disk()
            else:
                self.log(f"Schedule calculation FAILED: {result.get('error')}", 
                        level="ERROR")
        
        except Exception as e:
            self.log(f"Unexpected error: {e}", level="ERROR")
    
    def _service_recalculate(self, namespace, domain, service, data):
        """Service: manually recalculate schedule"""
        self.log("Manual recalculation triggered")
        self._daily_callback({}, is_manual=True)
    
    def _service_status(self, namespace, domain, service, kwargs):
        """Handle get_status service call"""
        self.log("Status service called")
        
        status = {
            'last_calculation': self.datetime().isoformat(),
            'next_calculation': GLOBAL_CONFIG.schedule_time,
            'devices': []
        }
        
        for device in DEVICES:
            status['devices'].append({
                'name': device.name,
                'entity_id': device.entity_id,
                'enabled': device.scheduling_enabled,
                'mode': device.schedule_mode,
                'slots_scheduled': sum(device.scheduled_slots),
                'schedule_ids': device.schedule_ids
            })
        
        self.fire_event("loads_status", status=status)
        self.log("Status event fired")
    
    def _service_enable_device(self, namespace, domain, service, kwargs):
        """Enable scheduling for a device"""
        device_name = kwargs.get('device_name')
        
        for device in DEVICES:
            if device.name == device_name:
                device.scheduling_enabled = True
                self.log(f"Enabled scheduling for {device_name}")
                self.fire_event("loads_device_enabled", device=device_name)
                return
        
        self.log(f"Device not found: {device_name}", level="WARNING")
    
    def _service_disable_device(self, namespace, domain, service, kwargs):
        """Disable scheduling for a device"""
        device_name = kwargs.get('device_name')
        
        for device in DEVICES:
            if device.name == device_name:
                device.scheduling_enabled = False
                self.log(f"Disabled scheduling for {device_name}")
                self.fire_event("loads_device_disabled", device=device_name)
                return
        
        self.log(f"Device not found: {device_name}", level="WARNING")
    
    def _service_override_device(self, namespace, domain, service, kwargs):
        """Manually override device ON/OFF"""
        device_name = kwargs.get('device_name')
        turn_on = kwargs.get('turn_on', True)
        
        for device in DEVICES:
            if device.name == device_name:
                service_name = "homeassistant/turn_on" if turn_on else "homeassistant/turn_off"
                self.call_service(service_name, entity_id=device.entity_id)
                self.log(f"Override {device_name}: {'ON' if turn_on else 'OFF'}")
                self.fire_event("loads_device_override", 
                              device=device_name, 
                              state="on" if turn_on else "off")
                return
        
        self.log(f"Device not found: {device_name}", level="WARNING")
    
    def _service_reset_debt(self, namespace, domain, service, kwargs):
        """Reset energy debt for a device or all devices"""
        device_name = kwargs.get('device_name')  # Optional - if not provided, reset all
        
        reset_count = 0
        for device in DEVICES:
            if device_name is None or device.name == device_name:
                if hasattr(device, 'energy_debt') and device.energy_debt > 0:
                    old_debt = device.energy_debt
                    device.energy_debt = 0
                    self._update_device_sensor_debt(device)
                    self.log(f"Reset energy debt for {device.name}: {old_debt} -> 0")
                    reset_count += 1
        
        if reset_count > 0:
            # Only update debt fields in JSON, don't overwrite the whole file
            self._update_debt_in_persistence()
            self.fire_event("loads_debt_reset", devices=reset_count)
            self.log(f"Energy debt reset for {reset_count} device(s)")
        elif device_name:
            self.log(f"Device not found or no debt: {device_name}", level="WARNING")
        else:
            self.log("No devices with debt to reset")
    
    def _update_sensors(self, result: dict):
        """Update Home Assistant sensors"""
        try:
            # Main status sensor
            self.set_state(
                "sensor.load_scheduler_status",
                state="active" if result['success'] else "error",
                attributes={
                    'last_update': self.datetime().isoformat(),
                    'next_date': result.get('date', 'unknown'),
                    'devices': result.get('devices', 0),
                    'total_slots': result.get('total_slots', 0),
                    'shelly_created': result.get('shelly', {}).get('created', 0),
                    'shelly_errors': len(result.get('shelly', {}).get('errors', [])),
                    'success': result['success'],
                    'electricity_package': GLOBAL_CONFIG.electricity_package.upper(),
                    'next_calculation': GLOBAL_CONFIG.schedule_time
                }
            )
            
            # Price stats sensor
            stats = result.get('price_stats', {})
            if stats:
                self.set_state(
                    "sensor.electricity_price_stats",
                    state=round(stats.get('avg', 0), 2),
                    attributes={
                        'min': stats.get('min', 0),
                        'max': stats.get('max', 0),
                        'avg': stats.get('avg', 0),
                        'current': stats.get('current', 0),
                        'current_rank': stats.get('current_rank', 0),
                        'unit': 'c/kWh',
                        'friendly_name': 'Electricity Price'
                    }
                )
            
            # Create price chart data sensor for graphing
            if hasattr(self.scheduler, 'price_slots') and self.scheduler.price_slots:
                # Create hourly averages for cleaner chart
                hourly_prices = []
                for hour in range(24):
                    hour_slots = self.scheduler.price_slots[hour*4:(hour+1)*4]
                    if hour_slots:
                        avg = sum(s.total_price for s in hour_slots) / len(hour_slots)
                        hourly_prices.append(round(avg * 100, 2))  # Convert to c/kWh
                
                self.set_state(
                    "sensor.electricity_price_chart",
                    state=len(hourly_prices),
                    attributes={
                        'prices': hourly_prices,
                        'hours': list(range(24)),
                        'unit': 'c/kWh',
                        'friendly_name': 'Hourly Electricity Prices',
                        'device_class': 'monetary'
                    }
                )
            
            # Per-device sensors
            for device in DEVICES:
                sensor_id = f"sensor.load_schedule_{device.name.lower().replace(' ', '_')}"
                self.set_state(
                    sensor_id,
                    state=sum(device.scheduled_slots),
                    attributes={
                        'device_name': device.name,
                        'entity_id': device.entity_id,
                        'mode': device.schedule_mode.value,  # Convert enum to string
                        'slots_scheduled': sum(device.scheduled_slots),
                        'hours_scheduled': sum(device.scheduled_slots) / 4,
                        'weather_adjusted': device.weather_adjustment,
                        'schedule_ids': device.schedule_ids,
                        'scheduled_slots': device.scheduled_slots,  # Add the actual slot array!
                        'enabled': device.scheduling_enabled,
                        'energy_debt': getattr(device, 'energy_debt', 0),
                        'last_update': self.datetime().isoformat()
                    }
                )
        
        except Exception as e:
            self.log(f"Failed to update sensors: {e}", level="ERROR")
    
    def _save_api_data_to_disk(self):
        """Save current API response data to disk for persistence"""
        try:
            # Generate the response data using the same logic as the API
            data = self._generate_api_response()
            
            # Save to file
            file_path = os.path.join(os.path.dirname(__file__), self.PERSISTENCE_FILE)
            with open(file_path, 'w') as f:
                json.dump(data, f)
            
            self.log(f"Saved API data to {file_path}")
        except Exception as e:
            self.log(f"Failed to save API data: {e}", level="ERROR")

    def _generate_api_response(self):
        """Generate the API response dictionary from current memory state"""
        # Prices are already in 22:00-22:00 order (slot 0 = 22:00)
        price_slots = []
        if hasattr(self, 'scheduler') and hasattr(self.scheduler, 'price_slots') and self.scheduler.price_slots:
            price_slots = self.scheduler.price_slots
        
        return {
            'calculated_at': getattr(self, 'schedule_calculated_at', None) or self.datetime().isoformat(),
            'prices': [
                {
                    'time': slot.timestamp.strftime('%H:%M'),
                    'price': round(slot.total_price * 100, 2),  # Convert to c/kWh
                    'spot': round(slot.spot_price * 100, 2),
                    'network': round(slot.network_fee * 100, 2)
                }
                for slot in price_slots
            ],
            'devices': [
                {
                    'name': device.name,
                    'mode': device.schedule_mode.value,
                    'slots': device.scheduled_slots,
                    'always_on_slots': [False] * 96,
                    'total_hours': sum(device.scheduled_slots) / 4,
                    'always_on_hours': device.always_on_hours if device.always_on_hours else "None",
                    'always_on_price': device.always_on_price,
                    'weather_adjustment': device.weather_adjustment,
                    'currently_active': sum(device.scheduled_slots) > 0,
                    'weather_info': f"{getattr(self.scheduler, 'avg_temp', 'N/A')}°C" if hasattr(self, 'scheduler') else None,
                    'energy_debt': getattr(device, 'energy_debt', 0)
                }
                for device in DEVICES
            ],
            'weather': getattr(self.scheduler, 'avg_temp', None) if hasattr(self, 'scheduler') else None,
            'package': GLOBAL_CONFIG.electricity_package.upper(),
            'recent_recoveries': list(getattr(self, 'recent_recoveries', []))
        }

    def _dashboard_api(self, data, **kwargs):
        """API endpoint for dashboard data
        
        Args:
            data: JSON data from request (empty for GET requests)
            **kwargs: Additional parameters including 'request' object
        """
        try:
            # Try to generate response from memory first
            response_data = self._generate_api_response()
            
            # Check if we have valid data (prices are key)
            if not response_data['prices']:
                # Memory is empty, try to load from disk
                file_path = os.path.join(os.path.dirname(__file__), self.PERSISTENCE_FILE)
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            disk_data = json.load(f)
                            # self.log("Serving API data from disk persistence")
                            return disk_data, 200
                    except Exception as e:
                        self.log(f"Failed to read persistence file: {e}", level="WARNING")
            
            return response_data, 200
            
        except Exception as e:
            self.log(f"Dashboard API error: {e}", level="ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {'error': str(e)}, 500
    def _api_reset_debt(self, data, **kwargs):
        """API endpoint for resetting energy debt
        
        Args:
            data: JSON data from request, optionally containing 'device_name'
            **kwargs: Additional parameters
        """
        try:
            device_name = data.get('device_name') if data else None
            
            reset_count = 0
            reset_devices = []
            for device in DEVICES:
                if device_name is None or device.name == device_name:
                    if hasattr(device, 'energy_debt') and device.energy_debt > 0:
                        old_debt = device.energy_debt
                        device.energy_debt = 0
                        self._update_device_sensor_debt(device)
                        self.log(f"API: Reset energy debt for {device.name}: {old_debt} -> 0")
                        reset_count += 1
                        reset_devices.append(device.name)
            
            if reset_count > 0:
                # Only update debt fields in JSON, don't overwrite the whole file
                self._update_debt_in_persistence()
                self.fire_event("loads_debt_reset", devices=reset_count)
            
            return {'success': True, 'reset_count': reset_count, 'devices': reset_devices}, 200
            
        except Exception as e:
            self.log(f"Reset debt API error: {e}", level="ERROR")
            return {'error': str(e)}, 500
    
    def _update_debt_in_persistence(self):
        """Update only energy_debt fields in the persistence file, preserving everything else"""
        try:
            file_path = os.path.join(os.path.dirname(__file__), self.PERSISTENCE_FILE)
            
            # Load existing data
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    data = json.load(f)
            else:
                # No file - create minimal structure
                data = {'devices': []}
            
            # Update debt values for each device
            existing_devices = {d['name']: d for d in data.get('devices', [])}
            
            for device in DEVICES:
                if device.name in existing_devices:
                    existing_devices[device.name]['energy_debt'] = getattr(device, 'energy_debt', 0)
                else:
                    # Device not in file yet, add minimal entry
                    data.setdefault('devices', []).append({
                        'name': device.name,
                        'energy_debt': getattr(device, 'energy_debt', 0)
                    })
            
            # Write back
            with open(file_path, 'w') as f:
                json.dump(data, f)
            
            self.log("Updated debt values in persistence file")
            
        except Exception as e:
            self.log(f"Failed to update debt in persistence: {e}", level="ERROR")
    
    def _load_persistence_data(self):
        """Load persistent data (energy debt) from disk"""
        try:
            file_path = os.path.join(os.path.dirname(__file__), self.PERSISTENCE_FILE)
            data = {'devices': []}
            
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        self.log("Error decoding persistence file, starting with empty data", level="WARNING")
            else:
                 self.log("No persistence file found, starting with 0 debt")

            # Create a map of loaded debts for easier lookup
            loaded_debts = {}
            if 'devices' in data:
                for device_data in data['devices']:
                    name = device_data.get('name')
                    debt = device_data.get('energy_debt', 0)
                    if name:
                        loaded_debts[name] = debt

            loaded_count = 0
            # Iterate ALL defined devices to ensure sync (even if debt is 0)
            for device in DEVICES:
                debt = loaded_debts.get(device.name, 0)
                device.energy_debt = debt
                
                # ALWAYS update sensor on startup to clear any stale state ("Ghost Debt")
                self._update_device_sensor_debt(device)
                
                if debt > 0:
                    self.log(f"Restored energy debt for {device.name}: {debt} min")
                    loaded_count += 1
            
            if loaded_count > 0:
                self.log(f"Restored debt for {loaded_count} devices from persistence")
                
        except Exception as e:
            self.log(f"Error loading persistence data: {e}", level="WARNING")

    def terminate(self):
        """Cleanup on termination"""
        self.log("Load Scheduling Application terminating")

    def _check_energy_debt(self, kwargs):
        """Check for energy debt and attempt recovery"""
        # Run every minute
        
        if not hasattr(self.scheduler, 'price_slots') or not self.scheduler.price_slots:
            return

        now = datetime.now(self.scheduler.tz)
        
        # Find current slot index
        # price_slots[0] is start time (e.g. 22:00 yesterday or today)
        start_time = self.scheduler.price_slots[0].timestamp
        
        # Check if we are in the valid range of current schedule
        # Schedule covers 24h from start_time
        if not (start_time <= now < start_time + timedelta(hours=24)):
            return

        # Calculate slot index (0-95)
        diff = now - start_time
        current_slot_idx = int(diff.total_seconds() // 900) # 15 min slots
        
        if not (0 <= current_slot_idx < 96):
            return

        debt_changed = False

        for device in DEVICES:
            if not device.scheduling_enabled:
                continue
                
            # 1. Track Debt / Payback
            is_scheduled_on = device.scheduled_slots[current_slot_idx]
            
            # Get actual state
            actual_state = self.get_state(device.entity_id)
            is_actual_on = actual_state == "on"
            
            if is_scheduled_on and not is_actual_on:
                # Accumulate debt (1 minute)
                old_debt = device.energy_debt
                device.energy_debt += 1
                if device.energy_debt > device.max_energy_debt:
                    device.energy_debt = device.max_energy_debt
                
                if device.energy_debt != old_debt:
                    debt_changed = True

                # Log occasionally
                if device.energy_debt % 15 == 0:
                    self.log(f"{device.name}: Energy debt increased to {device.energy_debt} min")
                    
            elif not is_scheduled_on and is_actual_on:
                # Payback debt
                if device.energy_debt > 0:
                    device.energy_debt -= 1
                    debt_changed = True
                    if device.energy_debt == 0:
                        self.log(f"{device.name}: Energy debt fully repaid")
            
            # 2. Attempt Recovery if needed
            if device.energy_debt > 0 and not is_scheduled_on and not is_actual_on:
                self._attempt_recovery(device, current_slot_idx, now)
                
            # Update sensor with debt info
            self._update_device_sensor_debt(device)

        # Save to disk if debt changed, so PBR sees it immediately
        if debt_changed:
            self._save_api_data_to_disk()

    def _attempt_recovery(self, device, current_slot_idx, now):
        """Attempt to recover energy debt"""
        # Postpone recovery if system is in mFRR mode (silently)
        qw_mode = self.get_state("sensor.qw_mode")
        if qw_mode in ["frrup", "frrdown"]:
            # self.log(f"Postponing recovery due to active mFRR mode ({qw_mode})")
            return

        # Look ahead recovery_window_hours
        slots_to_check = device.recovery_window_hours * 4
        
        candidates = []
        for i in range(slots_to_check):
            idx = current_slot_idx + i
            if idx >= 96:
                break # End of schedule
                
            # Must be unscheduled
            if device.scheduled_slots[idx]:
                continue
                
            # Check price
            price_slot = self.scheduler.price_slots[idx]
            price_cents = price_slot.total_price * 100
            
            if device.max_recovery_price and price_cents > device.max_recovery_price:
                continue
                
            candidates.append({
                'idx': idx,
                'price': price_cents,
                'time': price_slot.timestamp
            })
            
        if not candidates:
            return
            
        # Sort by price
        candidates.sort(key=lambda x: x['price'])
        
        # How many slots do we need?
        # Debt is in minutes. Each slot is 15 min.
        # We need ceil(debt / 15) slots.
        import math
        slots_needed = math.ceil(device.energy_debt / 15)
        
        # Take top N cheapest
        best_slots = candidates[:slots_needed]
        best_indices = [c['idx'] for c in best_slots]

        # Debug selection
        self.log(f"DEBUG RECOVERY {device.name}: Need {slots_needed} slots. Best candidates: {[{'idx': c['idx'], 'price': c['price']} for c in best_slots]}")
        self.log(f"DEBUG RECOVERY {device.name}: Current slot {current_slot_idx} in best? {current_slot_idx in best_indices}")
        
        # Is CURRENT slot one of the best?
        if current_slot_idx in best_indices:
            self.log(f"{device.name}: Recovery triggered! Debt={device.energy_debt}m")
            self.call_service("homeassistant/turn_on", entity_id=device.entity_id)
            
            # Log to history
            if hasattr(self, 'recent_recoveries'):
                self.recent_recoveries.append({
                    'timestamp': now.isoformat(),
                    'device': device.name,
                    'debt_burned': 15, # Approx
                    'price': self.scheduler.price_slots[current_slot_idx].total_price * 100
                })

    def _update_device_sensor_debt(self, device):
        """Update sensor with debt attribute"""
        sensor_id = f"sensor.load_schedule_{device.name.lower().replace(' ', '_')}"
        current = self.get_state(sensor_id, attribute="all")
        if current:
            attrs = current['attributes']
            # Only update if changed to avoid spamming events
            if attrs.get('energy_debt') != device.energy_debt:
                attrs['energy_debt'] = device.energy_debt
                self.set_state(sensor_id, state=current['state'], attributes=attrs)


