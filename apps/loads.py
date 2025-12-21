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
            
            # Schedule daily calculation using robust timezone handling
            self._schedule_next_run()
            
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
            
            # Register dashboard API endpoint
            self.register_endpoint(self._dashboard_api, "load_scheduler_data")
            
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
                service_name = "homeassistant.turn_on" if turn_on else "homeassistant.turn_off"
                self.call_service(service_name, entity_id=device.entity_id)
                self.log(f"Override {device_name}: {'ON' if turn_on else 'OFF'}")
                self.fire_event("loads_device_override", 
                              device=device_name, 
                              state="on" if turn_on else "off")
                return
        
        self.log(f"Device not found: {device_name}", level="WARNING")
    
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
            'calculated_at': self.datetime().isoformat(),
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
                    'weather_info': f"{getattr(self.scheduler, 'avg_temp', 'N/A')}°C" if hasattr(self, 'scheduler') else None
                }
                for device in DEVICES
            ],
            'weather': getattr(self.scheduler, 'avg_temp', None) if hasattr(self, 'scheduler') else None,
            'package': GLOBAL_CONFIG.electricity_package.upper()
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
                            self.log("Serving API data from disk persistence")
                            return disk_data, 200
                    except Exception as e:
                        self.log(f"Failed to read persistence file: {e}", level="WARNING")
            
            return response_data, 200
            
        except Exception as e:
            self.log(f"Dashboard API error: {e}", level="ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", level="ERROR")
            return {'error': str(e)}, 500
    
    def terminate(self):
        """Cleanup on termination"""
        self.log("Load Scheduling Application terminating")

