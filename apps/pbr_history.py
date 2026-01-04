
import json
import os
import time
from collections import deque
from datetime import datetime, timedelta

class PbrHistoryManager:
    """
    Manages historical data for the Phase Balancer.
    
    1. High-Frequency: In-memory circular buffer for 1-minute state snapshots (last 24h).
    2. Events: Persistent JSON log for major events (Mode changes, Load Switching).
    """
    
    # 24 hours * 60 minutes = 1440 snapshots
    MAX_SNAPSHOTS = 1440
    PERSISTENCE_FILE = "pbr_events.json"
    
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.snapshots = deque(maxlen=self.MAX_SNAPSHOTS)
        self.events = deque(maxlen=100) # Keep last 100 events in memory
        
        self.file_path = os.path.join(self.data_dir, self.PERSISTENCE_FILE)
        self._load_events()

    def add_snapshot(self, state):
        """Add a 1-minute state snapshot"""
        snapshot = {
            "ts": int(time.time()),
            "l1": state.get("l1_current", 0),
            "l2": state.get("l2_current", 0),
            "l3": state.get("l3_current", 0),
            "soc": state.get("battery_soc", 0),
            "mode": state.get("mode", "unknown"),
            "grid": state.get("grid_power", 0),
            "pv": state.get("pv_power", 0),
            "bat": state.get("battery_power", 0),
            "load": state.get("house_load", 0)
        }
        self.snapshots.append(snapshot)

    def add_event(self, event_type, message, details=None):
        """Add a significant event"""
        event = {
            "ts": int(time.time()),
            "type": event_type,
            "msg": message,
            "details": details or {}
        }
        self.events.append(event)
        self._save_events()

    def get_history(self, hours=24):
        """Get flattened history for API"""
        cutoff = time.time() - (hours * 3600)
        
        return {
            "snapshots": [s for s in self.snapshots if s["ts"] > cutoff],
            "events": [e for e in self.events if e["ts"] > cutoff]
        }

    def _save_events(self):
        """Save events to disk"""
        try:
            with open(self.file_path, 'w') as f:
                json.dump(list(self.events), f)
        except Exception as e:
            print(f"Failed to save PBR events: {e}")

    def _load_events(self):
        """Load events from disk"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    self.events.extend(data)
            except Exception as e:
                print(f"Failed to load PBR events: {e}")
