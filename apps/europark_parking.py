import appdaemon.plugins.hass.hassapi as hass
import requests
import time
import datetime
from datetime import datetime, timezone
import pytz

class EuroparkParking(hass.Hass):

    def initialize(self):
        # Schedule daily run at 1:00 AM
        self.run_daily(self.activate_parking, "00:40:00")
        
        # Entities and constants
        self.vehicle_reg_entity = "input_text.vehicle_registration"
        self.api_enabled_entity = "input_boolean.europark_api_call_enabled"
        self.email = self.args.get("email")
        self.auth_url = "https://partner.europark.ee/admin/api/login"
        self.products_url = "https://partner.europark.ee/admin/api/products/guest-parking"
        self.parking_base_url = "https://partner.europark.ee/admin/api/products"
        self.password = self.args.get("password")
        self.retry_interval = 1800  # 30 minutes in seconds
        self.max_retries = 48       # Retry for 24 hours (48 * 30min)
        self.product_id = None  # Will be fetched dynamically
        # Parking zone/product name substring to match (configurable via app args)
        self.zone_name = self.args.get("zone_name", "EP90")
        
        # Session tracking
        self.last_session = self.get_app_state()  # Get saved state or initialize empty
        
        # Listen for changes to the api_enabled_entity
        self.listen_state(self.on_api_enabled_changed, self.api_enabled_entity)

    def on_api_enabled_changed(self, entity, attribute, old, new, kwargs):
        """Handler for when the api_enabled_entity state changes"""
        if new == "on" and old == "off":
            self.log("Parking automation enabled, activating parking")
            # Run the parking activation with a slight delay
            self.run_in(self.activate_parking, 2)  # 2 second delay
            


    def activate_parking(self, kwargs):
        """Non-blocking activation entrypoint. Uses AppDaemon scheduling for retries."""
        # Support being called directly (kwargs may be None) or via run_in (kwargs is a dict)
        attempt = 1
        if isinstance(kwargs, dict):
            attempt = int(kwargs.get("retry_attempt", 1))

        if self.get_state(self.api_enabled_entity) != "on":
            self.log("Automation disabled, skipping parking activation.")
            return

        vehicle_reg = self.get_state(self.vehicle_reg_entity)
        if not vehicle_reg:
            self.notify("Vehicle registration not set. Please enter it in the UI.", title="Parking Activation Error")
            self.log("Vehicle registration input_text is empty.")
            return

        # Check if we've already successfully registered parking for this vehicle today
        if self.last_session and self.last_session.get("last_vehicle_reg") == vehicle_reg:
            last_end_time = self.last_session.get("last_end_time")
            # Parse the stored end time
            try:
                # Try to parse it as a string first
                if isinstance(last_end_time, str) and 'T' in last_end_time:
                    end_time_utc = last_end_time
                    dt_utc = datetime.strptime(end_time_utc.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)

                    # If the end time is in the future, parking is still valid
                    if dt_utc > now_utc:
                        local_end_time = self.convert_utc_to_local(last_end_time)
                        self.log(f"Already activated parking for {vehicle_reg} today (valid until {local_end_time})")
                        self.notify(f"Parking already activated today for {vehicle_reg} until {local_end_time}",
                                    title="Parking Already Active")
                        return
                    else:
                        self.log(f"Previous parking session has expired, activating new session")
            except Exception as e:
                self.log(f"Error parsing stored session data: {e}")

        # Attempt to authenticate
        token = self.authenticate()
        if not token:
            self.notify(f"Auth failed (attempt {attempt}/{self.max_retries}). Retrying in 30 minutes.",
                        title="Parking Activation Authentication Failed")
            self.log(f"Auth failed on attempt {attempt}. Scheduling retry in {self.retry_interval} seconds.")
            if attempt < self.max_retries:
                # schedule a retry without blocking
                self.run_in(self.activate_parking, self.retry_interval, retry_attempt=attempt + 1)
            else:
                self.notify(f"All {self.max_retries} authentication attempts failed for {vehicle_reg}. Manual intervention required.",
                            title="Parking Activation Failure")
                self.log(f"All authentication attempts failed for {vehicle_reg}. Manual intervention required.")
            return

        # Check if parking is already active before starting a new session
        is_active, end_time = self.check_active_parking(token, vehicle_reg)
        if is_active:
            # Save the session info
            self.save_session_info(vehicle_reg, end_time)

            # Convert end time to local time
            local_end_time = self.convert_utc_to_local(end_time)
            # Format title and message per user request: "OK - PLATE - ZONE"
            title = f"OK - {vehicle_reg} - {self.zone_name}"
            # extract HH:MM from local_end_time ("YYYY-MM-DD HH:MM:SS (Estonian time)")
            end_hhmm = "unknown"
            try:
                if local_end_time and isinstance(local_end_time, str) and len(local_end_time.split()) >= 2:
                    end_hhmm = local_end_time.split()[1][:5]
            except Exception:
                end_hhmm = local_end_time
            message = f"Parking active until {end_hhmm}"
            self.notify(message, title=title)
            self.log(f"Parking already active for {vehicle_reg} until {local_end_time}")
            return

        # Schedule the actual parking start shortly to mimic previous small delay, non-blocking
        self.run_in(self._start_parking_attempt, 3, vehicle_reg=vehicle_reg, token=token, retry_attempt=attempt)

    def _start_parking_attempt(self, kwargs):
        """Helper scheduled by activate_parking to perform start_parking and schedule retries if needed."""
        vehicle_reg = kwargs.get("vehicle_reg")
        token = kwargs.get("token")
        attempt = int(kwargs.get("retry_attempt", 1))

        try:
            if self.start_parking(vehicle_reg, token):
                # Retrieve end time from saved session if available
                end_time = None
                if self.last_session:
                    end_time = self.last_session.get("last_end_time")
                local_end_time = self.convert_utc_to_local(end_time) if end_time else "unknown time"
                # extract end HH:MM
                end_hhmm = "unknown"
                try:
                    if isinstance(local_end_time, str) and len(local_end_time.split()) >= 2:
                        end_hhmm = local_end_time.split()[1][:5]
                except Exception:
                    end_hhmm = local_end_time
                # Start time in Estonian local timezone (HH:MM)
                tallinn_tz = pytz.timezone('Europe/Tallinn')
                start_hhmm = datetime.now(tallinn_tz).strftime("%H:%M")
                # Build formatted title and message
                title = f"OK - {vehicle_reg} - {self.zone_name}"
                message = f"Parking successful at {start_hhmm} until {end_hhmm}"
                self.notify(message, title=title)
                self.log(f"Parking activated for {vehicle_reg} until {local_end_time}")
                return
            else:
                # Failure notification formatted
                tallinn_tz = pytz.timezone('Europe/Tallinn')
                fail_time = datetime.now(tallinn_tz).strftime("%H:%M")
                title = f"FAIL - {vehicle_reg} - {self.zone_name}"
                message = f"Parking failed at {fail_time}"
                self.notify(message, title=title)
                self.log(f"Parking activation failed on attempt {attempt}. Scheduling retry in {self.retry_interval} seconds.")
                if attempt < self.max_retries:
                    self.run_in(self.activate_parking, self.retry_interval, retry_attempt=attempt + 1)
                else:
                    self.notify(f"All {self.max_retries} attempts failed for {vehicle_reg}. Manual intervention required.",
                                title="Parking Activation Failure")
                    self.log(f"All attempts failed for {vehicle_reg}. Manual intervention required.")
        except Exception as e:
            self.log(f"Exception in _start_parking_attempt: {e}")
            if attempt < self.max_retries:
                self.run_in(self.activate_parking, self.retry_interval, retry_attempt=attempt + 1)
            else:
                # Final exception -> send formatted failure notification
                tallinn_tz = pytz.timezone('Europe/Tallinn')
                fail_time = datetime.now(tallinn_tz).strftime("%H:%M")
                title = f"FAIL - {vehicle_reg} - {self.zone_name}"
                message = f"Parking failed at {fail_time} due to an exception"
                self.notify(message, title=title)
                self.log(f"All attempts failed for {vehicle_reg} due to exception. Manual intervention required.")

    def authenticate(self):
        try:
            payload = {"email": self.email, "password": self.password}
            response = requests.post(self.auth_url,
                                     json=payload,
                                     timeout=10)
            
            if response.status_code == 200:
                response_json = response.json()
                token = response_json.get("token")
                if token:
                    self.log("Authentication successful")
                    return token
                else:
                    self.log("Auth response missing token")
            else:
                self.log(f"Auth failed with status code {response.status_code}")
        except Exception as e:
            self.log(f"Auth exception: {e}")
        return None

    def start_parking(self, vehicle_reg, token):
        try:
            # Get the product ID if we don't have it already
            product_id = self.get_product_id(token)
            if not product_id:
                self.log("Failed to get parking product ID")
                return False
                
            # Construct the parking URL with the product ID
            parking_url = f"{self.parking_base_url}/{product_id}/guest-parking/start"
            
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "vehicle_reg": vehicle_reg,
                "payment_method": "partner"
            }
            
            response = requests.post(parking_url, json=payload, headers=headers, timeout=10)
            
            # Consider both 200 and 201 as success status codes
            # 200 = OK, 201 = Created (a new parking session was created)
            if response.status_code in [200, 201]:
                data = response.json().get("data", {})
                if data.get("vehicle_reg", "").upper() == vehicle_reg.upper():
                    # Get the end time from the response
                    end_time_utc = data.get("end_time", "")
                    
                    # Convert to Estonian time and save both UTC and local time
                    local_time = self.convert_utc_to_local(end_time_utc)
                    self.log(f"Parking activated for {vehicle_reg} until {local_time}")
                    # Save with the original UTC time for proper future comparisons
                    self.save_session_info(vehicle_reg, end_time_utc)
                    return True
                else:
                    self.log("Vehicle registration mismatch in response")
                    return False
            else:
                self.log(f"Parking API call failed with status code {response.status_code}")
                return False
        except Exception as e:
            self.log(f"Parking API exception: {e}")
        return False

    def notify(self, message, title="Parking Activation Notification", mobile=True):
        """Send notification inside Home Assistant and optionally as an iOS push.
 
        Uses a configurable notify service from app args (notify_service). If not provided,
        mobile push is skipped. The user should configure the phone reference from Home Assistant
        in secrets.yaml (e.g. notify.mobile_app_your_phone).
        The function expects callers to include licence plate / area / result in the title
        and the time information in the message when appropriate.
        """
        # Persistent notification in Home Assistant UI
        try:
            self.call_service("persistent_notification/create", title=title, message=message)
        except Exception as e:
            self.log(f"Failed to create persistent notification: {e}")
 
        if not mobile:
            return
 
        # Mobile push: use configured service
        mobile_service = self.args.get("notify_service")
        
        if not mobile_service:
            self.log("No notify_service configured, skipping mobile push")
            return

        # Normalize service format: accept either "domain/service" or "domain.service"
        ms = str(mobile_service)
        if "/" in ms:
            service_to_call = ms
        elif "." in ms:
            service_to_call = ms.replace(".", "/", 1)
        else:
            self.log(f"Invalid notify_service format: {ms}")
            return
 
        try:
            # Call the mobile notify service with title and message so it appears as a system push on iOS
            self.call_service(service_to_call, message=message, title=title)
        except Exception as e:
            self.log(f"Mobile notify failed using service '{mobile_service}': {e}")

    def get_product_id(self, token):
        """Fetch the product ID for EP90 parking from the API."""
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(self.products_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get("data", [])
                
                # Look for product with "EP90" in the name
                for product in products:
                    if self.zone_name in product.get("name", ""):
                        product_id = product.get("id")
                        return product_id
                
                self.log("No parking product with 'EP90' in the name found")
            else:
                self.log(f"Failed to fetch parking products: {response.status_code}")
        except Exception as e:
            self.log(f"Error fetching parking products: {e}")
        
        return None

    def check_active_parking(self, token, vehicle_reg):
        """Check if a vehicle already has active parking.
        
        Args:
            token: Auth token for API
            vehicle_reg: Vehicle registration number to check
            
        Returns:
            tuple: (is_active, end_time) - Boolean indicating if parking is active and end time if active
        """
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(self.products_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get("data", [])
                
                # Go through all products that have "EP90" in the name
                for product in products:
                    if self.zone_name in product.get("name", ""):
                        # Check active parking sessions in this product
                        for parking in product.get("parkings", []):
                            if parking.get("vehicle_reg", "").upper() == vehicle_reg.upper() and parking.get("status") == "active":
                                end_time = parking.get("end_time")
                                self.log(f"Vehicle {vehicle_reg} already has active parking until {end_time}")
                                return True, end_time
            
            return False, None
        except Exception as e:
            self.log(f"Error checking active parking: {e}")
            return False, None

    def convert_utc_to_local(self, utc_time_str):
        """Convert UTC time string to Estonian local time.
        
        Args:
            utc_time_str: UTC time string in ISO format
            
        Returns:
            str: Formatted local time string
        """
        try:
            # Parse the UTC time (removing milliseconds if present)
            if not utc_time_str:
                return "unknown time"
                
            utc_time_str = utc_time_str.split('.')[0] + 'Z' if '.' in utc_time_str else utc_time_str
            dt_utc = datetime.strptime(utc_time_str, "%Y-%m-%dT%H:%M:%SZ")
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            
            # Get the Estonian timezone (Europe/Tallinn)
            tallinn_tz = pytz.timezone('Europe/Tallinn')
            
            # Convert to Estonian time with proper DST handling
            dt_estonia = dt_utc.astimezone(tallinn_tz)
            return dt_estonia.strftime("%Y-%m-%d %H:%M:%S (Estonian time)")
        except Exception as e:
            self.log(f"Time conversion error: {e}")
            return f"{utc_time_str} UTC"

    def get_app_state(self):
        """Get the saved app state or initialize with default values."""
        try:
            state = self.get_state(f"{self.name}.state", attribute="all")
            if state is None:
                return {
                    "last_vehicle_reg": None,
                    "last_end_time": None,
                    "last_activation_time": None
                }
            return state
        except Exception as e:
            self.log(f"Error getting app state: {e}")
            return {
                "last_vehicle_reg": None,
                "last_end_time": None, 
                "last_activation_time": None
            }
            
    def save_session_info(self, vehicle_reg, end_time):
        """Save information about the current parking session."""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_session = {
                "last_vehicle_reg": vehicle_reg,
                "last_end_time": end_time,
                "last_activation_time": now
            }
            
            # Store in AppDaemon's state
            self.set_state(f"{self.name}.state", state="active", attributes=self.last_session)
            
            # Convert end_time to Estonian time for the log message
            local_end_time = self.convert_utc_to_local(end_time)
            self.log(f"Saved session info: {vehicle_reg} until {local_end_time}")
        except Exception as e:
            self.log(f"Error saving session info: {e}")
