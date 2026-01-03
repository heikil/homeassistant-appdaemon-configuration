"""
loads_prices.py - Electricity price fetching and analysis

STRICT REQUIREMENT: No pbr.py dependencies - reusable module
"""

import requests
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Dict
import pytz


@dataclass
class PriceSlot:
    """Single 15-minute price slot"""
    timestamp: datetime
    spot_price: float  # EUR/kWh (converted from MWh)
    network_fee: float  # EUR/kWh
    total_price: float  # EUR/kWh (spot + network)
    slot_index: int  # 0-95
    hour: int  # 0-23
    always_on: bool = False  # Must be ON (device requirement)
    always_off: bool = False  # Must be OFF (overrides everything, including always_on)


class LoadsPriceManager:
    """
    Manages electricity price fetching and analysis
    
    Handles Elering API, 15-minute slots, network fees
    """
    
    VAT_RATE = 1.24  # 24% VAT
    RENEWABLE_ENERGY_FEE = 0.0084  # 0.84 c/kWh = 0.0084 EUR/kWh (before VAT)
    ELECTRICITY_EXCISE = 0.0021  # 0.21 c/kWh = 0.0021 EUR/kWh (before VAT)
    BALANCING_FEE = 0.00373  # 0.373 c/kWh = 0.00373 EUR/kWh (before VAT)
    SECURITY_FEE = 0.00758  # 0.758 c/kWh = 0.00758 EUR/kWh (before VAT)
    SELLER_MARGIN = 0.00413 / 1.24  # 0.413 c/kWh (with VAT) -> converted to EUR/kWh (before VAT)
    
    def __init__(self, network_provider: str, electricity_package: str, 
                 country: str, timezone_str: str):
        self.network_provider = network_provider
        self.package = electricity_package  # Store package name
        self.country = country
        self.tz = pytz.timezone(timezone_str)
        self._cache: Dict[str, List[PriceSlot]] = {}
    
    def fetch_prices_for_date(self, target_date: datetime) -> List[PriceSlot]:
        """
        Fetch 96 price slots for a specific date
        
        Args:
            target_date: Date to fetch (timezone-aware)
            
        Returns:
            List of 96 PriceSlot objects
        """
        if target_date.tzinfo is None:
            target_date = self.tz.localize(target_date)
        
        cache_key = target_date.strftime("%Y-%m-%d")
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        prices = self._fetch_from_elering(target_date)
        self._cache[cache_key] = prices
        return prices
    
    def _fetch_from_elering(self, target_date: datetime) -> List[PriceSlot]:
        """Fetch from Nord Pool API with retry logic
        
        Returns 96 slots covering 22:00 (day N-1) to 22:00 (day N)
        Slot 0 = 22:00, Slot 8 = 00:00, Slot 95 = 21:45
        """
        # Validate type
        if not isinstance(target_date, datetime):
            raise TypeError(f"target_date must be datetime, got {type(target_date).__name__}")
        
        # Need to fetch two days to cover 22:00-22:00 window
        # If target is Nov 16, we need Nov 15 (for 22:00-23:45) and Nov 16 (for 00:00-21:59)
        dates_to_fetch = [
            (target_date - timedelta(days=1)).date(),  # Previous day
            target_date.date()                          # Target day
        ]
        
        max_retries = 3
        all_prices = []
        
        for fetch_date in dates_to_fetch:
            for attempt in range(max_retries):
                try:
                    # Nord Pool API endpoint
                    url = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPriceIndices"
                    params = {
                        'date': fetch_date.strftime('%Y-%m-%d'),
                        'market': 'DayAhead',
                        'indexNames': 'EE',
                        'currency': 'EUR',
                        'resolutionInMinutes': '15'
                    }
                    
                    # Log Nord Pool query
                    query_url = f"{url}?date={params['date']}&market={params['market']}&indexNames={params['indexNames']}&currency={params['currency']}&resolutionInMinutes={params['resolutionInMinutes']}"
                    print(f"Nord Pool query: {query_url}")
                    
                    response = requests.get(url, params=params, timeout=15)
                    response.raise_for_status()
                    
                    # Parse JSON response
                    data = response.json()
                    
                    # Extract price entries
                    for entry in data.get('multiIndexEntries', []):
                        # Parse deliveryStart timestamp (UTC)
                        delivery_start_str = entry['deliveryStart']
                        ts = datetime.fromisoformat(delivery_start_str.replace('Z', '+00:00'))
                        ts = ts.astimezone(self.tz)
                        
                        # Get price for Estonia
                        spot = entry['entryPerArea']['EE'] / 1000.0  # MWh -> kWh
                        network = self._calc_network_fee(ts, spot)
                        
                        # Add renewable energy fee, electricity excise, balancing fee, security fee, and seller margin (before VAT)
                        spot_with_fees = spot + self.RENEWABLE_ENERGY_FEE + self.ELECTRICITY_EXCISE + self.BALANCING_FEE + self.SECURITY_FEE + self.SELLER_MARGIN
                        
                        # Apply VAT to spot (with fees) and network separately
                        spot_with_vat = spot_with_fees * self.VAT_RATE
                        network_with_vat = network * self.VAT_RATE
                        
                        # Store with calendar-based slot index (will reorder later)
                        slot_idx = (ts.hour * 60 + ts.minute) // 15
                        
                        all_prices.append(PriceSlot(
                            timestamp=ts,
                            spot_price=spot_with_vat,
                            network_fee=network_with_vat,
                            total_price=spot_with_vat + network_with_vat,
                            slot_index=slot_idx,
                            hour=ts.hour
                        ))
                    
                    # Successfully fetched this date
                    break
                    
                except Exception as e:
                    if attempt < max_retries - 1:
                        continue  # Retry
                    else:
                        # Final attempt failed for this date
                        print(f"Nord Pool API failed for {fetch_date} after {max_retries} attempts: {e}")
                        # Continue to next date or use fallback
        
        # Sort all prices by timestamp
        all_prices.sort(key=lambda x: x.timestamp)
        
        # Extract 22:00-22:00 window
        # We need: (target_date - 1 day) 22:00 to target_date 22:00
        window_start = (target_date - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        window_end = target_date.replace(hour=22, minute=0, second=0, microsecond=0)
        
        # Filter to 22:00-22:00 window
        prices_22_22 = [
            p for p in all_prices 
            if window_start <= p.timestamp < window_end
        ]
        
        if len(prices_22_22) == 96:
            # Reindex: slot 0 = 22:00, slot 8 = 00:00, slot 95 = 21:45
            reindexed_prices = []
            for idx, price in enumerate(prices_22_22):
                reindexed_prices.append(PriceSlot(
                    timestamp=price.timestamp,
                    spot_price=price.spot_price,
                    network_fee=price.network_fee,
                    total_price=price.total_price,
                    slot_index=idx,  # New index: 0-95 starting from 22:00
                    hour=price.hour
                ))
            
            print(f"We got market prices from Nord Pool at {datetime.now()}")
            print(f"Price window: {window_start.strftime('%Y-%m-%d %H:%M')} to {window_end.strftime('%Y-%m-%d %H:%M')}")
            return reindexed_prices
        else:
            print(f"Expected 96 slots for 22:00-22:00 window, got {len(prices_22_22)} - using fallback")
            return self._get_fallback_prices(target_date)
    
    def _get_fallback_prices(self, target_date: datetime) -> List[PriceSlot]:
        """Generate fallback prices when API fails"""
        # Use typical day/night pattern
        prices = []
        base_price = 50.0  # EUR/MWh
        
        for slot_idx in range(96):
            hour = slot_idx // 4
            minute = (slot_idx % 4) * 15
            
            ts = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # Simple pattern: expensive 7-22, cheap night
            if 7 <= hour < 22:
                spot = base_price * 1.3
            else:
                spot = base_price * 0.7
            
            network = self._calc_network_fee(ts, spot)
            
            # Add renewable energy fee, electricity excise, balancing fee, security fee, and seller margin (before VAT)
            spot_with_fees = spot + self.RENEWABLE_ENERGY_FEE + self.ELECTRICITY_EXCISE + self.BALANCING_FEE + self.SECURITY_FEE + self.SELLER_MARGIN
            
            # Apply VAT to spot (with fees) and network separately
            spot_with_vat = spot_with_fees * self.VAT_RATE
            network_with_vat = network * self.VAT_RATE
            
            prices.append(PriceSlot(
                timestamp=ts,
                spot_price=spot_with_vat,
                network_fee=network_with_vat,
                total_price=spot_with_vat + network_with_vat,
                slot_index=slot_idx,
                hour=hour
            ))
        
        return prices
    
    def _calc_network_fee(self, ts: datetime, spot: float) -> float:
        """Calculate network + transfer fees based on package (€/kWh)"""
        hour = ts.hour
        is_winter = ts.month in [11, 12, 1, 2, 3]  # Nov-Mar (Python months: 1-12)
        is_workday = ts.weekday() < 5  # Monday=0, Sunday=6
        is_weekend = not is_workday
        
        # All fees from JS are in €/MWh, need to convert to €/kWh
        MWH_TO_KWH = 1000.0
        
        if self.network_provider == "elektrilevi":
            # Elektrilevi packages - exact port from SmartHeatingWithShelly.js
            
            if self.package == "vork1":
                # VORK1: Single rate
                return 77.2 / MWH_TO_KWH
            
            elif self.package == "vork2":
                # VORK2: Day/Night
                # Night: 22-07 MO-FR + SA-SU all day
                if hour < 7 or hour >= 22 or is_weekend:
                    return 35.1 / MWH_TO_KWH  # nRt
                else:
                    return 60.7 / MWH_TO_KWH  # dRt
            
            elif self.package == "vork4":
                # VORK4: Day/Night
                # Night: 22-07 MO-FR + SA-SU all day
                if hour < 7 or hour >= 22 or is_weekend:
                    return 21.0 / MWH_TO_KWH  # nRt
                else:
                    return 36.9 / MWH_TO_KWH  # dRt
            
            elif self.package == "vork5":
                # VORK5: Day/Night/Peak
                # Peak holiday: Nov-Mar, SA-SU 16:00-20:00
                if is_winter and is_weekend and 16 <= hour < 20:
                    return 47.4 / MWH_TO_KWH  # hMRt
                # Peak daytime: Nov-Mar, MO-FR 09:00-12:00 and 16:00-20:00
                elif is_winter and is_workday and ((9 <= hour < 12) or (16 <= hour < 20)):
                    return 81.8 / MWH_TO_KWH  # dMRt
                # Night: MO-FR 22:00-07:00 + SA-SU all day
                elif hour < 7 or hour >= 22 or is_weekend:
                    return 30.3 / MWH_TO_KWH  # nRt
                else:
                    return 52.9 / MWH_TO_KWH  # dRt
            
            # Fallback
            return 77.2 / MWH_TO_KWH
        
        elif self.network_provider == "imatra":
            # Imatra Finland packages - exact port from JS
            # Summer time check (UTC+3 vs UTC+2)
            # Simplified: use month-based check instead of timezone offset
            is_summer = ts.month in [3, 4, 5, 6, 7, 8, 9]  # Apr-Sep
            
            if self.package == "partn24":
                # Partner24: Flat rate
                return 60.7 / MWH_TO_KWH
            
            elif self.package == "partn24pl":
                # Partner24Plus: Flat rate
                return 38.6 / MWH_TO_KWH
            
            elif self.package == "partn12":
                # Partner12: Day/Night
                if is_summer:
                    # Summer: night 00-08 + SA-SU all day
                    if hour < 8 or is_weekend:
                        return 42.0 / MWH_TO_KWH  # nRt
                    else:
                        return 72.4 / MWH_TO_KWH  # dRt
                else:
                    # Winter: night 23-07 + SA-SU all day
                    if hour < 7 or hour >= 23 or is_weekend:
                        return 42.0 / MWH_TO_KWH  # nRt
                    else:
                        return 72.4 / MWH_TO_KWH  # dRt
            
            elif self.package == "partn12pl":
                # Partner12Plus: Day/Night
                if is_summer:
                    # Summer: night 00-08 + SA-SU all day
                    if hour < 8 or is_weekend:
                        return 27.1 / MWH_TO_KWH  # nRt
                    else:
                        return 46.4 / MWH_TO_KWH  # dRt
                else:
                    # Winter: night 23-07 + SA-SU all day
                    if hour < 7 or hour >= 23 or is_weekend:
                        return 27.1 / MWH_TO_KWH  # nRt
                    else:
                        return 46.4 / MWH_TO_KWH  # dRt
            
            # Fallback
            return 60.7 / MWH_TO_KWH
        
        elif self.network_provider == "latvia":
            # Latvia packages - exact port from JS
            
            if self.package == "pamata1":
                return 39.62 / MWH_TO_KWH
            
            elif self.package == "special1":
                return 158.48 / MWH_TO_KWH
            
            # Fallback
            return 39.62 / MWH_TO_KWH
        
        return 0.0
    
    def get_cheapest_slots(self, prices: List[PriceSlot], num_slots: int,
                          min_rank: Optional[int] = None,
                          max_rank: Optional[int] = None) -> List[int]:
        """
        Get cheapest slot indices (relative to input list)
        
        Args:
            prices: List of PriceSlot objects (can be subset)
            num_slots: How many to select
            min_rank: Minimum percentile (0-100)
            max_rank: Maximum percentile (0-100)
            
        Returns:
            List of indices relative to the input prices list (0 to len(prices)-1)
        """
        # Create list with both price and original index
        indexed_prices = [(i, p) for i, p in enumerate(prices)]
        sorted_indexed = sorted(indexed_prices, key=lambda x: x[1].total_price)
        
        # Filter by rank if specified
        if min_rank is not None or max_rank is not None:
            filtered = []
            for i, (idx, price) in enumerate(sorted_indexed):
                rank = (i / len(sorted_indexed)) * 100
                if min_rank and rank < min_rank:
                    continue
                if max_rank and rank > max_rank:
                    continue
                filtered.append((idx, price))
            sorted_indexed = filtered
        
        # Return relative indices (position in input list)
        cheapest = sorted_indexed[:num_slots]
        return [idx for idx, _ in cheapest]
    
    def get_price_stats(self, prices: List[PriceSlot]) -> Dict:
        """Get price statistics"""
        if not prices:
            return {}
        
        totals = [p.total_price for p in prices]
        return {
            'min': min(totals),
            'max': max(totals),
            'avg': sum(totals) / len(totals),
            'median': sorted(totals)[len(totals) // 2]
        }
