"""
Range of Strike Prices (RSP) Strategy Implementation
Monitors a range of strike prices for trading opportunities
"""

import asyncio
import logging
import httpx
import json
import redis.asyncio as redis
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.orm import Session

from database import get_db, SessionLocal, get_naive_ist_now, CrossoverEvent, OptionsPairs
from models.rsp_monitoring import RSPMonitoring, RSPMonitoringStrike
from kite_services.kite_service import KiteService
from notification_service import NotificationService
from pushover_service import send_pushover_notification
from datetime_utils import get_naive_ist_now

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/rsp", tags=["rsp-strategy"])

# Lock configuration
LOCK_TTL_SECONDS = 28800  # 1 hour TTL for safety
LOCK_KEY_PREFIX = "monitor:active:rsp:"

# Trading Hours Configuration (IST) - Updated for RSP strategy
TRADING_SESSIONS = [
    {"start": "00:00", "end": "23:59"}   # Full trading session
]

def is_trading_hours(timestamp: datetime = None) -> bool:
    """
    Check if current time (or given timestamp) is within trading hours
    Trading Hours: 9:09 AM - 3:30 PM IST (Monday to Friday)
    """
    if timestamp is None:
        timestamp = get_naive_ist_now()
    
    # Get current time in IST
    current_time = timestamp.time()
    current_weekday = timestamp.weekday()  # 0=Monday, 6=Sunday
    current_weekday = 4  # 0=Monday, 6=Sunday
    
    # Check if it's a weekday (Monday=0 to Friday=4)
    if current_weekday > 4:  # Saturday=5, Sunday=6
        logger.debug(f"⏰ Weekend detected - Trading disabled: {timestamp.strftime('%A %H:%M:%S')}")
        return False
    
    # Check each trading session
    for session in TRADING_SESSIONS:
        start_time = datetime.strptime(session["start"], "%H:%M").time()
        end_time = datetime.strptime(session["end"], "%H:%M").time()
        
        if start_time <= current_time <= end_time:
            logger.debug(f"🟢 Trading hours active: {timestamp.strftime('%H:%M:%S')}")
            return True
    
    logger.debug(f"🚫 Outside trading hours: {timestamp.strftime('%H:%M:%S')}")
    return False

def is_before_market_open(timestamp: datetime = None) -> bool:
    """
    Check if current time is before market open (9:15 AM IST)
    Returns True if before 9:15 AM on a weekday
    """
    if timestamp is None:
        timestamp = get_naive_ist_now()
    
    current_time = timestamp.time()
    current_weekday = timestamp.weekday()  # 0=Monday, 6=Sunday
    
    # Only relevant for weekdays
    if current_weekday > 4:  # Weekend
        return False
    
    # Market opens at 9:15 AM
    market_open_time = datetime.strptime("09:15", "%H:%M").time()
    
    return current_time < market_open_time

async def wait_for_market_open():
    """
    Wait until market opens (9:15 AM IST) on weekdays
    Returns immediately if already past market open or on weekends
    """
    current_time = get_naive_ist_now()
    
    if not is_before_market_open(current_time):
        return  # Already past market open
    
    # Calculate time until 9:15 AM
    market_open_time = datetime.strptime("09:15", "%H:%M").time()
    today_market_open = datetime.combine(current_time.date(), market_open_time)
    
    # If we're before 9:15 AM today
    time_to_wait = (today_market_open - current_time).total_seconds()
    
    if time_to_wait > 0:
        logger.info(f"⏰ Waiting {time_to_wait/60:.1f} minutes until market open (9:15 AM)...")
        await asyncio.sleep(time_to_wait)
        logger.info("🟢 Market open time reached - starting monitoring")

@dataclass
class RSPConfig:
    """Configuration for Range of Strike Prices monitoring"""
    symbol: str
    monitoring_threshold: float = 2.0  # Default 2% threshold for price movement monitoring
    monitoring_interval: float = 0.5  # Default monitoring interval in seconds
    is_paper: bool = True  # Default to paper trading - True: paper trade, False: actual trade
    quantity_lots: int = 1  # Number of lots to trade (1 lot = lot_size units)
    
    # RSP specific settings
    strike_range_count: int = 5  # Number of strikes to monitor (ATM-2 to ATM+2)
    price_movement_alert_threshold: float = 1.5  # Alert when price moves by this percentage
    
    # Manual meeting point strikes input
    manual_meetingpoint_strikes: Optional[List[int]] = field(default=None)  # Optional manual input for yesterday's meeting point strikes
    calculated_strikes: Optional[List[float]] = field(default=None)  # Store one-time calculated strikes
    
    # Database persistence
    monitoring_id: Optional[str] = field(default=None)  # Database monitoring session ID

@dataclass
class StrikeRangeData:
    """Data structure for strike range information"""
    strike_price: float
    ce_token: int
    pe_token: int
    ce_symbol: str
    pe_symbol: str
    ce_price: float
    pe_price: float
    distance_from_atm: int  # -2, -1, 0, +1, +2 etc.
    is_atm: bool = False

class RSPStrategy:
    """Range of Strike Prices Strategy Implementation with Redis-based concurrency control"""
    
    def __init__(self, kite_service: KiteService, notification_service: NotificationService, 
                 base_url: str = "http://localhost:8000"):
        self.kite_service = kite_service
        self.notification_service = notification_service
        self.active_monitors: Dict[str, asyncio.Task] = {}
        self.base_url = base_url
        self.http_client = httpx.AsyncClient(timeout=30.0)
        
        # Redis client for atomic locking - will be initialized on first use
        self.redis_client: Optional[redis.Redis] = None
        self.redis_pool: Optional[redis.ConnectionPool] = None
        self.redis_initialized: bool = False
        self.redis_init_lock = asyncio.Lock()  # Prevent concurrent initialization
        self.acquired_locks: Set[str] = set()  # Track locks acquired by this instance
        
        # NEW: In-memory caches for efficient monitoring
        self.strike_cache: Dict[str, List[RSPMonitoringStrike]] = {}  # monitoring_id -> strikes
        self.previous_prices: Dict[str, Dict[str, float]] = {}  # monitoring_id -> {token: price}
        self.last_cache_refresh: Dict[str, datetime] = {}  # monitoring_id -> timestamp
        
        # Track current configurations for each symbol
        self.symbol_configs: Dict[str, RSPConfig] = {}
        
        # System status
        self.is_running = False
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_health_check = datetime.now()
        
        # Major instrument tokens mapping
        self.instrument_tokens = {
            "NIFTY": 256265,     # NSE:NIFTY 50
            "BANKNIFTY": 260105, # NSE:NIFTY BANK
            "SENSEX": 265,       # BSE:SENSEX
            "BANKEX": 12311      # BSE:BANKEX
        }
    
    def _get_exchange_for_symbol(self, symbol: str) -> str:
        """
        Determine the exchange based on the underlying symbol
        SENSEX and BANKEX options are traded on BFO (BSE)
        NIFTY and BANKNIFTY options are traded on NFO (NSE)
        """
        symbol_upper = symbol.upper()
        
        # BFO symbols (BSE)
        if any(bfo_symbol in symbol_upper for bfo_symbol in ['SENSEX', 'BANKEX']):
            return "BFO"
        
        # NFO symbols (NSE) - default for others
        return "NFO"
    
    async def _start_health_monitoring(self):
        """Start health monitoring task for automatic cleanup"""
        if self._health_check_task is None or self._health_check_task.done():
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            logger.info("🏥 Started RSP health monitoring")
    
    async def _health_check_loop(self):
        """Periodic health check and cleanup"""
        try:
            while self.is_running:
                try:
                    # Perform health check every 5 minutes
                    await asyncio.sleep(300)
                    
                    if not self.is_running:
                        break
                    
                    # Check and cleanup stale locks
                    await self._cleanup_stale_locks()
                    
                    # Check active monitors health
                    await self._check_monitors_health()
                    
                    self._last_health_check = datetime.now()
                    logger.debug("🏥 Health check completed")
                    
                except Exception as e:
                    logger.error(f"❌ Error in health check: {e}")
                    await asyncio.sleep(60)  # Wait 1 minute before retry
                    
        except asyncio.CancelledError:
            logger.info("🏥 Health monitoring cancelled")
            raise
        except Exception as e:
            logger.error(f"❌ Critical error in health monitoring: {e}")
    
    async def _check_monitors_health(self):
        """Check health of active monitors and clean up dead ones"""
        try:
            dead_monitors = []
            
            for symbol, task in self.active_monitors.items():
                if task.done():
                    # Monitor is dead, check if it ended with error
                    try:
                        await task  # This will raise if task failed
                        logger.info(f"✅ Monitor for {symbol} completed normally")
                    except Exception as e:
                        logger.error(f"❌ Monitor for {symbol} failed: {e}")
                    
                    dead_monitors.append(symbol)
            
            # Clean up dead monitors
            for symbol in dead_monitors:
                logger.warning(f"🧹 Cleaning up dead monitor for {symbol}")
                
                # Release lock
                await self._release_monitoring_lock(symbol)
                
                # Clean up data
                if symbol in self.active_monitors:
                    del self.active_monitors[symbol]
                if symbol in self.symbol_configs:
                    del self.symbol_configs[symbol]
                
                # Send notification about dead monitor
                await self._send_rsp_notification(
                    f"⚠️ RSP Monitor Cleanup - {symbol}",
                    f"Dead monitor detected and cleaned up for {symbol}\n"
                    f"Lock released automatically\n"
                    f"Time: {datetime.now().strftime('%H:%M:%S')}"
                )
            
            # Update system running status
            if not self.active_monitors:
                self.is_running = False
                
        except Exception as e:
            logger.error(f"❌ Error checking monitors health: {e}")
    
    async def _ensure_redis_connection(self):
        """Ensure Redis connection is established (centralized initialization)"""
        if self.redis_initialized and self.redis_client is not None:
            return  # Already initialized and ready
        
        # Use lock to prevent concurrent initialization
        async with self.redis_init_lock:
            # Double-check after acquiring lock
            if self.redis_initialized and self.redis_client is not None:
                return
            
            logger.info("🔄 Initializing centralized Redis connection...")
            await self._initialize_redis()
            self.redis_initialized = True
            logger.info("✅ Centralized Redis connection established")

    async def _initialize_redis(self):
        """Initialize Redis connection pool with conservative settings"""
        try:
            self.redis_pool = redis.ConnectionPool(
                host='localhost',
                port=6379,
                decode_responses=True,
                max_connections=10,  # Reduced from 50 to 10
                socket_keepalive=True,
                socket_keepalive_options={},
                retry_on_timeout=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30  # Check connection health every 30s
            )
            self.redis_client = redis.Redis(connection_pool=self.redis_pool)
            
            # Test connection
            await self.redis_client.ping()
            logger.info("✅ Redis connection established successfully with 10 max connections")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Redis: {e}")
            raise HTTPException(
                status_code=503, 
                detail="Redis connection failed - atomic locking not available"
            )

    async def initialize_redis_startup(self):
        """Initialize Redis connection during application startup"""
        try:
            logger.info("🚀 Pre-initializing Redis connection for RSP strategy...")
            await self._ensure_redis_connection()
            logger.info("✅ Redis pre-initialization completed for RSP strategy")
        except Exception as e:
            logger.error(f"❌ Failed to pre-initialize Redis: {e}")
            # Don't raise exception here - let it be initialized on first use
            pass

    async def get_latest_tick(self, instrument_token: int) -> Optional[dict]:
        """Get latest tick for a specific instrument"""
        try:
            # Ensure Redis connection is initialized
            await self._ensure_redis_connection()
            
            # If Redis is still not available, return None
            if not self.redis_initialized or self.redis_client is None:
                logger.warning(f"Redis client not available for getting latest tick {instrument_token}")
                return None
            
            tick_data = await self.redis_client.hgetall(f"tick:latest:{instrument_token}")
            # print(tick_data)
            if not tick_data:
                return None
            
            # Parse JSON fields
            parsed_data = {}
            for key, value in tick_data.items():
                try:
                    parsed_data[key] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    parsed_data[key] = value
            
            return parsed_data
            
        except Exception as e:
            logger.error(f"Error getting latest tick for {instrument_token}: {e}")
            return None
    
    async def get_tick_at_time(self, instrument_token: int, target_time: str = "09:15") -> Optional[dict]:
        """Get tick data at a specific time from Redis stream"""
        try:
            # Ensure Redis connection is initialized
            await self._ensure_redis_connection()
            
            if self.redis_client is None:
                logger.warning(f"Redis client not available for getting tick at time {target_time}")
                return None
            
            # Test connection
            await self.redis_client.ping()
            
            stream_key = f"tick:stream:{instrument_token}"
            
            # Get current date for timestamp construction
            today = datetime.now().date()
            target_datetime = datetime.combine(today, datetime.strptime(target_time, "%H:%M").time())
            target_timestamp_ms = int(target_datetime.timestamp() * 1000)
            
            # Get stream entries from start of day to find closest to target time
            start_of_day = datetime.combine(today, datetime.min.time())
            start_timestamp_ms = int(start_of_day.timestamp() * 1000)
            
            # Use XRANGE to get entries from start of day to target time + 5 minutes buffer
            end_timestamp_ms = target_timestamp_ms + (5 * 60 * 1000)  # 5 minutes buffer
            
            entries = await self.redis_client.xrange(
                stream_key, 
                min=f"{start_timestamp_ms}-0",
                max=f"{end_timestamp_ms}-0",
                count=1000  # Get sufficient entries to find the closest one
            )
            
            if not entries:
                logger.warning(f"No stream entries found for {instrument_token} around {target_time}")
                return None
            
            # Find the entry closest to target time
            closest_entry = None
            min_time_diff = float('inf')
            
            for entry_id, fields in entries:
                try:
                    # Extract timestamp from Redis stream ID (format: timestamp-sequence)
                    entry_timestamp_ms = int(entry_id.split('-')[0])
                    time_diff = abs(entry_timestamp_ms - target_timestamp_ms)
                    
                    if time_diff < min_time_diff:
                        min_time_diff = time_diff
                        closest_entry = fields
                        
                        # If we find an exact match or very close (within 1 minute), use it
                        if time_diff <= 60000:  # 1 minute in milliseconds
                            break
                            
                except (ValueError, IndexError) as e:
                    logger.debug(f"Error parsing entry timestamp {entry_id}: {e}")
                    continue
            
            if closest_entry:
                # Parse the fields back to proper format
                parsed_data = {}
                for key, value in closest_entry.items():
                    try:
                        # Handle JSON fields like 'ohlc'
                        if key == 'ohlc':
                            parsed_data[key] = json.loads(value)
                        else:
                            # Try to parse as JSON first, then fall back to string
                            try:
                                parsed_data[key] = json.loads(value)
                            except (json.JSONDecodeError, TypeError):
                                parsed_data[key] = value
                    except Exception as e:
                        logger.debug(f"Error parsing field {key}: {e}")
                        parsed_data[key] = value
                
                # Log the time difference for debugging
                time_diff_minutes = min_time_diff / (60 * 1000)
                logger.info(f"📊 Found tick data for {instrument_token} at {target_time} (±{time_diff_minutes:.1f} min)")
                
                return parsed_data
            else:
                logger.warning(f"No suitable tick data found for {instrument_token} around {target_time}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting tick at time {target_time} for {instrument_token}: {e}")
            return None
    
    async def get_earliest_tick_today(self, instrument_token: int) -> Optional[dict]:
        """Get the earliest available tick for today from Redis stream"""
        try:
            # Ensure Redis connection is initialized
            await self._ensure_redis_connection()
            
            if self.redis_client is None:
                logger.warning(f"Redis client not available for getting earliest tick")
                return None
            
            # Test connection
            await self.redis_client.ping()
            
            stream_key = f"tick:stream:{instrument_token}"
            
            # Get current date for timestamp construction
            today = datetime.now().date()
            start_of_day = datetime.combine(today, datetime.min.time())
            end_of_day = start_of_day + timedelta(days=1)
            
            start_timestamp_ms = int(start_of_day.timestamp() * 1000)
            end_timestamp_ms = int(end_of_day.timestamp() * 1000)
            
            # Get the first entry of today using XRANGE with count=1
            entries = await self.redis_client.xrange(
                stream_key, 
                min=f"{start_timestamp_ms}-0",
                max=f"{end_timestamp_ms}-0",
                count=1  # Get only the first entry
            )
            
            if not entries:
                logger.warning(f"No stream entries found for {instrument_token} today")
                return None
            
            # Get the first (earliest) entry
            entry_id, fields = entries[0]
            
            # Parse the fields back to proper format
            parsed_data = {}
            for key, value in fields.items():
                try:
                    # Handle JSON fields like 'ohlc'
                    if key == 'ohlc':
                        parsed_data[key] = json.loads(value)
                    else:
                        # Try to parse as JSON first, then fall back to string
                        try:
                            parsed_data[key] = json.loads(value)
                        except (json.JSONDecodeError, TypeError):
                            parsed_data[key] = value
                except Exception as e:
                    logger.debug(f"Error parsing field {key}: {e}")
                    parsed_data[key] = value
            
            # Extract and log the time of earliest entry
            entry_timestamp_ms = int(entry_id.split('-')[0])
            entry_time = datetime.fromtimestamp(entry_timestamp_ms / 1000)
            
            logger.info(f"📊 Found earliest tick for {instrument_token} at {entry_time.strftime('%H:%M:%S')}")
            
            return parsed_data
                
        except Exception as e:
            logger.error(f"Error getting earliest tick for {instrument_token}: {e}")
            return None
    
    async def get_latest_price_from_redis(self, symbol: str) -> Optional[dict]:
        """Get latest market price from Redis for a symbol"""
        try:
            # Ensure Redis connection is initialized
            await self._ensure_redis_connection()
            
            # If Redis is still not available, return None
            if not self.redis_initialized or self.redis_client is None:
                logger.warning(f"Redis client not available for getting latest price for {symbol}")
                return None
            
            # Define major indices that need symbol-to-token mapping
            major_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "BANKEX"]
            
            # For major indices, use instrument token mapping
            if symbol in major_indices:
                instrument_token = self.instrument_tokens.get(symbol)
                if not instrument_token:
                    logger.warning(f"No instrument token found for major index: {symbol}")
                    return None
                return await self.get_latest_tick(instrument_token)
            else:
                # For other instruments, symbol is already the token, use directly
                return await self.get_latest_tick(symbol)
            
        except Exception as e:
            logger.error(f"Error getting latest price from Redis for {symbol}: {e}")
            return None
    
    async def _send_no_price_notification(self, symbol: str):
        """Send notification when no latest price is available"""
        try:
            title = f"⚠️ No Latest Price - {symbol}"
            message = f"No latest price available for {symbol}\n\n"
            message += f"📊 Symbol: {symbol}\n"
            message += f"🔍 Redis Key: tick:latest:{symbol}\n"
            message += f"⏰ Time: {get_naive_ist_now().strftime('%H:%M:%S')}\n\n"
            message += f"🚨 RSP monitoring cannot proceed without price data\n"
            message += f"💡 Check if streaming service is running and populating Redis"
            
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=1  # High priority for missing price data
            )
            
            if success:
                logger.info(f"📱 No price notification sent for {symbol}")
            else:
                logger.error(f"📱 Failed to send no price notification for {symbol}")
                
        except Exception as e:
            logger.error(f"❌ Error sending no price notification for {symbol}: {e}")
    
    def _get_lock_key(self, symbol: str) -> str:
        """Get Redis lock key for symbol"""
        return f"{LOCK_KEY_PREFIX}{symbol.upper()}"
    
    async def _acquire_monitoring_lock(self, symbol: str) -> bool:
        """
        Acquire atomic lock for monitoring a symbol using Redis SETNX
        Returns True if lock acquired, False if already exists
        """
        try:
            await self._ensure_redis_connection()
            
            lock_key = self._get_lock_key(symbol)
            lock_value = f"rsp_monitor_{datetime.now().isoformat()}"
            
            # Use SETNX with TTL (SET with NX and EX options)
            lock_acquired = await self.redis_client.set(
                lock_key, 
                lock_value, 
                nx=True,  # Only set if key doesn't exist (SETNX behavior)
                ex=LOCK_TTL_SECONDS  # Set TTL
            )
            
            if lock_acquired:
                self.acquired_locks.add(symbol.upper())
                logger.info(f"🔒 Acquired monitoring lock for {symbol} (TTL: {LOCK_TTL_SECONDS}s)")
                return True
            else:
                # Check who owns the lock
                existing_value = await self.redis_client.get(lock_key)
                lock_ttl = await self.redis_client.ttl(lock_key)
                logger.warning(f"🚫 Lock already exists for {symbol}")
                logger.warning(f"   Existing lock: {existing_value}")
                logger.warning(f"   TTL remaining: {lock_ttl}s")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error acquiring lock for {symbol}: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"Failed to acquire monitoring lock: {str(e)}"
            )
    
    async def _release_monitoring_lock(self, symbol: str) -> bool:
        """Release monitoring lock for symbol"""
        try:
            if self.redis_client is None:
                logger.warning(f"⚠️ Redis client not available to release lock for {symbol}")
                return False
            
            lock_key = self._get_lock_key(symbol)
            
            # Delete the lock
            deleted = await self.redis_client.delete(lock_key)
            
            if deleted:
                self.acquired_locks.discard(symbol.upper())
                logger.info(f"🔓 Released monitoring lock for {symbol}")
                return True
            else:
                logger.warning(f"⚠️ Lock for {symbol} was not found or already released")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error releasing lock for {symbol}: {e}")
            return False
    
    async def _check_lock_status(self, symbol: str) -> Dict[str, Any]:
        """Check current lock status for symbol"""
        try:
            await self._ensure_redis_connection()
            
            lock_key = self._get_lock_key(symbol)
            lock_value = await self.redis_client.get(lock_key)
            
            if lock_value:
                lock_ttl = await self.redis_client.ttl(lock_key)
                return {
                    "locked": True,
                    "lock_value": lock_value,
                    "ttl_seconds": lock_ttl,
                    "owned_by_this_instance": symbol.upper() in self.acquired_locks
                }
            else:
                return {
                    "locked": False,
                    "lock_value": None,
                    "ttl_seconds": 0,
                    "owned_by_this_instance": False
                }
                
        except Exception as e:
            logger.error(f"❌ Error checking lock status for {symbol}: {e}")
            return {
                "locked": False,
                "error": str(e)
            }
    
    async def _cleanup_stale_locks(self):
        """Cleanup locks for inactive monitors (crash recovery) - comprehensive version"""
        cleanup_results = {
            "total_locks_found": 0,
            "locks_cleaned": 0,
            "cleanup_details": [],
            "errors": []
        }
        
        try:
            # Ensure Redis connection
            await self._ensure_redis_connection()
            
            if self.redis_client is None:
                logger.warning("⚠️ Redis client not available for stale lock cleanup")
                cleanup_results["errors"].append("Redis client not available")
                return cleanup_results
            
            # Get all RSP locks
            pattern = f"{LOCK_KEY_PREFIX}*"
            lock_keys = await self.redis_client.keys(pattern)
            cleanup_results["total_locks_found"] = len(lock_keys)
            
            logger.info(f"🔍 Found {len(lock_keys)} RSP monitoring locks for cleanup check")
            
            if not lock_keys:
                logger.info("✅ No RSP monitoring locks found - cleanup not needed")
                return cleanup_results
            
            for lock_key in lock_keys:
                try:
                    symbol = lock_key.replace(LOCK_KEY_PREFIX, "")
                    
                    # Get lock details
                    lock_value = await self.redis_client.get(lock_key)
                    lock_ttl = await self.redis_client.ttl(lock_key)
                    
                    # Check if we have an active monitor for this symbol
                    has_active_monitor = (symbol in self.active_monitors and 
                                        not self.active_monitors[symbol].done())
                    
                    lock_detail = {
                        "symbol": symbol,
                        "lock_key": lock_key,
                        "lock_value": lock_value,
                        "ttl_seconds": lock_ttl,
                        "has_active_monitor": has_active_monitor,
                        "action": "keep"
                    }
                    
                    # Decide whether to clean up the lock
                    should_cleanup = False
                    cleanup_reason = ""
                    
                    if not has_active_monitor:
                        # No active monitor for this symbol
                        should_cleanup = True
                        cleanup_reason = "No active monitor"
                    elif lock_ttl < 300:  # Less than 5 minutes remaining
                        # Lock is expiring soon, likely stale
                        should_cleanup = True
                        cleanup_reason = "Lock expiring soon"
                    
                    if should_cleanup:
                        # Clean up the lock
                        deleted = await self.redis_client.delete(lock_key)
                        if deleted:
                            cleanup_results["locks_cleaned"] += 1
                            lock_detail["action"] = f"cleaned - {cleanup_reason}"
                            self.acquired_locks.discard(symbol)
                            logger.warning(f"🧹 Cleaned up stale lock for {symbol} - {cleanup_reason}")
                        else:
                            lock_detail["action"] = f"cleanup_failed - {cleanup_reason}"
                            logger.error(f"❌ Failed to cleanup lock for {symbol}")
                    else:
                        logger.debug(f"🔒 Keeping active lock for {symbol} (TTL: {lock_ttl}s)")
                    
                    cleanup_results["cleanup_details"].append(lock_detail)
                    
                except Exception as lock_error:
                    error_msg = f"Error processing lock {lock_key}: {str(lock_error)}"
                    cleanup_results["errors"].append(error_msg)
                    logger.error(f"❌ {error_msg}")
            
            logger.info(f"🧹 Stale lock cleanup completed:")
            logger.info(f"   📊 Total locks found: {cleanup_results['total_locks_found']}")
            logger.info(f"   🧹 Locks cleaned: {cleanup_results['locks_cleaned']}")
            logger.info(f"   ❌ Errors: {len(cleanup_results['errors'])}")
            
            return cleanup_results
                        
        except Exception as e:
            error_msg = f"Critical error during stale lock cleanup: {str(e)}"
            cleanup_results["errors"].append(error_msg)
            logger.error(f"❌ {error_msg}")
            return cleanup_results
    
    async def _initialize_strike_cache(self, monitoring_id: str):
        """Initialize in-memory cache for strike data to avoid DB queries every cycle"""
        try:
            logger.info(f"🔄 Initializing strike cache for monitoring_id: {monitoring_id}")
            
            db = SessionLocal()
            try:
                # Fetch all strike records for this monitoring session
                strike_records = db.query(RSPMonitoringStrike).filter(
                    RSPMonitoringStrike.monitoring_id == monitoring_id
                ).all()
                
                if not strike_records:
                    logger.warning(f"⚠️ No strike records found for monitoring_id {monitoring_id}")
                    self.strike_cache[monitoring_id] = []
                    return
                
                # Cache the strike records
                self.strike_cache[monitoring_id] = strike_records
                self.last_cache_refresh[monitoring_id] = datetime.now()
                
                # Initialize previous prices dict for crossover detection
                self.previous_prices[monitoring_id] = {}
                
                # Pre-populate with current prices as "previous" for first cycle
                for strike_record in strike_records:
                    if strike_record.ce_token:
                        ce_tick = await self.get_latest_tick(int(strike_record.ce_token))
                        if ce_tick:
                            ce_price = ce_tick.get('last_price') or ce_tick.get('ltp')
                            if ce_price:
                                self.previous_prices[monitoring_id][strike_record.ce_token] = float(ce_price)
                    
                    if strike_record.pe_token:
                        pe_tick = await self.get_latest_tick(int(strike_record.pe_token))
                        if pe_tick:
                            pe_price = pe_tick.get('last_price') or pe_tick.get('ltp')
                            if pe_price:
                                self.previous_prices[monitoring_id][strike_record.pe_token] = float(pe_price)
                
                logger.info(f"✅ Strike cache initialized: {len(strike_records)} strikes, {len(self.previous_prices[monitoring_id])} prices")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error initializing strike cache for {monitoring_id}: {e}")
    
    async def _refresh_strike_cache_if_needed(self, monitoring_id: str):
        """Refresh strike cache only if levels were updated (to get fresh reentry points)"""
        try:
            # Refresh every 5 minutes or if cache is empty
            cache_age_limit = timedelta(minutes=5)
            last_refresh = self.last_cache_refresh.get(monitoring_id)
            
            needs_refresh = (
                monitoring_id not in self.strike_cache or 
                not self.strike_cache[monitoring_id] or
                not last_refresh or
                datetime.now() - last_refresh > cache_age_limit
            )
            
            if needs_refresh:
                logger.debug(f"🔄 Refreshing strike cache for {monitoring_id}")
                await self._initialize_strike_cache(monitoring_id)
                
        except Exception as e:
            logger.error(f"❌ Error refreshing strike cache for {monitoring_id}: {e}")
    
    async def _update_strike_cache_and_db(self, monitoring_id: str, strike_record: RSPMonitoringStrike, 
                                        option_type: str, new_reentry_point: float, new_stop_loss: float):
        """Update both database and in-memory cache atomically"""
        try:
            db = SessionLocal()
            try:
                # Update database
                db_record = db.query(RSPMonitoringStrike).filter(
                    RSPMonitoringStrike.monitoring_id == monitoring_id,
                    RSPMonitoringStrike.strike == strike_record.strike
                ).first()
                
                if not db_record:
                    logger.error(f"❌ Strike record not found in DB for update: {strike_record.strike}")
                    return False
                
                if option_type == 'CE':
                    db_record.ce_reentry_point = new_reentry_point
                    db_record.ce_stoploss = new_stop_loss
                    db_record.ce_last_updated = datetime.now()
                else:
                    db_record.pe_reentry_point = new_reentry_point
                    db_record.pe_stoploss = new_stop_loss
                    db_record.pe_last_updated = datetime.now()
                
                db_record.updated_at = datetime.now()
                db.commit()
                
                # Update in-memory cache (same record objects)
                if monitoring_id in self.strike_cache:
                    for cached_record in self.strike_cache[monitoring_id]:
                        if cached_record.strike == strike_record.strike:
                            if option_type == 'CE':
                                cached_record.ce_reentry_point = new_reentry_point
                                cached_record.ce_stoploss = new_stop_loss
                                cached_record.ce_last_updated = datetime.now()
                            else:
                                cached_record.pe_reentry_point = new_reentry_point
                                cached_record.pe_stoploss = new_stop_loss
                                cached_record.pe_last_updated = datetime.now()
                            cached_record.updated_at = datetime.now()
                            break
                
                logger.info(f"✅ Updated both DB and cache for {option_type} strike {strike_record.strike}")
                return True
                
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Database update failed, rolling back: {e}")
                return False
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in _update_strike_cache_and_db: {e}")
            return False

    async def start_monitoring(self, symbol: str, config: RSPConfig):
        """Start RSP monitoring for a symbol with atomic locking"""
        try:
            symbol = symbol.upper()
            
            # Step 0: Clear any existing config for this symbol to ensure fresh start
            if symbol in self.symbol_configs:
                logger.info(f"🧹 Clearing existing config for {symbol} to ensure fresh configuration")
                del self.symbol_configs[symbol]
            
            # Step 1: Check atomic lock using Redis SETNX
            lock_acquired = await self._acquire_monitoring_lock(symbol)
            
            if not lock_acquired:
                # Lock already exists - return error response
                lock_status = await self._check_lock_status(symbol)
                error_msg = f"RSP monitoring already active for {symbol}. "
                error_msg += f"Lock TTL: {lock_status.get('ttl_seconds', 0)}s. "
                error_msg += "Please stop existing monitoring before starting new one."
                
                logger.warning(f"🚫 Monitoring lock conflict for {symbol}")
                return {
                    "success": False, 
                    "message": error_msg,
                    "code": 409,  # Conflict status code
                    "lock_info": lock_status
                }
            
            # Step 2: Check if already monitoring (double-check)
            if symbol in self.active_monitors:
                if not self.active_monitors[symbol].done():
                    # Release lock and return error
                    await self._release_monitoring_lock(symbol)
                    logger.warning(f"RSP monitoring already active for {symbol}")
                    return {
                        "success": False, 
                        "message": f"Already monitoring {symbol}",
                        "code": 409
                    }
            
            # Step 3: Check if we need to wait for market open
            current_time = get_naive_ist_now()
            if is_before_market_open(current_time):
                logger.info(f"🕘 Starting RSP monitoring for {symbol} before market open - will wait until 9:15 AM")
                # Allow monitoring to start but it will wait for market open
            elif not is_trading_hours():
                # After market hours or weekend - don't allow
                await self._release_monitoring_lock(symbol)
                logger.warning(f"Outside trading hours - RSP monitoring not started for {symbol}")
                return {
                    "success": False, 
                    "message": "Outside trading hours. Monitoring not started.",
                    "trading_hours": "09:15-15:30 IST (Mon-Fri)",
                    "code": 400
                }
            
            try:
                # Step 4: Create database monitoring session
                monitoring_id = await self._create_monitoring_session(symbol, config)
                if not monitoring_id:
                    # Release lock and return error if database session creation fails
                    await self._release_monitoring_lock(symbol)
                    logger.error(f"❌ Failed to create database monitoring session for {symbol}")
                    return {
                        "success": False,
                        "message": f"Failed to create monitoring session in database for {symbol}",
                        "code": 500
                    }
                
                # Store monitoring_id in config for later reference
                config.monitoring_id = monitoring_id
                
                # Step 5: Store configuration
                self.symbol_configs[symbol] = config
                
                # Step 6: Start monitoring task
                monitor_task = asyncio.create_task(self._monitor_symbol_rsp(symbol, config))
                self.active_monitors[symbol] = monitor_task
                self.is_running = True
                
                logger.info(f"🚀 Started RSP monitoring for {symbol}")
                logger.info(f"📊 Config: Threshold={config.monitoring_threshold}%, Interval={config.monitoring_interval}s")
                logger.info(f"💰 Trading Mode: {'Paper Trading' if config.is_paper else 'Live Trading'}")
                logger.info(f"📦 Quantity: {config.quantity_lots} lot(s)")
                logger.info(f"🎯 Strike Range: {len(config.calculated_strikes) if config.calculated_strikes else config.strike_range_count} strikes")
                if config.calculated_strikes:
                    logger.info(f"📊 Selected strikes: {config.calculated_strikes}")
                logger.info(f"🔒 Lock acquired successfully with TTL: {LOCK_TTL_SECONDS}s")
                
                # Step 6: Send startup notification
                await self._send_rsp_notification(
                    f"🚀 RSP Monitoring Started - {symbol}",
                    f"Range of Strike Prices monitoring is now active for {symbol}\n\n"
                    f"📊 Configuration:\n"
                    f"• Mode: {'Paper Trading' if config.is_paper else 'Live Trading'}\n"
                    f"• Threshold: {config.monitoring_threshold}%\n"
                    f"• Quantity: {config.quantity_lots} lot(s)\n"
                    f"• Strike Range: {len(config.calculated_strikes) if config.calculated_strikes else config.strike_range_count} strikes\n"
                    f"• Interval: {config.monitoring_interval}s\n\n"
                    f"� Price Source: Redis (tick:latest:{symbol})\n"
                    f"�🔒 Atomic lock secured (TTL: {LOCK_TTL_SECONDS}s)\n\n"
                    f"✅ System will monitor Redis for latest prices and send notifications if no price data is available"
                )
                
                return {
                    "success": True,
                    "message": f"RSP monitoring started for {symbol}",
                    "config": config.__dict__,
                    "lock_info": await self._check_lock_status(symbol)
                }
                
            except Exception as monitoring_error:
                # If monitoring setup fails, clean up properly
                logger.error(f"❌ Failed to start monitoring for {symbol}: {monitoring_error}")
                
                # Clean up monitoring data
                if symbol in self.active_monitors:
                    task = self.active_monitors[symbol]
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    del self.active_monitors[symbol]
                
                if symbol in self.symbol_configs:
                    del self.symbol_configs[symbol]
                
                # Release the lock
                await self._release_monitoring_lock(symbol)
                
                # Re-raise the exception
                raise monitoring_error
            
        except Exception as e:
            logger.error(f"❌ Error starting RSP monitoring for {symbol}: {e}")
            # Ensure lock is released on any error (belt and suspenders approach)
            try:
                await self._release_monitoring_lock(symbol)
                logger.info(f"🧹 Emergency cleanup completed for {symbol}")
            except Exception as cleanup_error:
                logger.error(f"❌ Error during emergency cleanup for {symbol}: {cleanup_error}")
            raise
    
    async def stop_monitoring(self, symbol: str):
        """Stop RSP monitoring for a symbol and force clear lock (works for both active and stale locks)"""
        try:
            symbol = symbol.upper()
            
            # Track if we found an active monitor
            had_active_monitor = False
            
            # Check if there's an active monitor and stop it
            if symbol in self.active_monitors:
                had_active_monitor = True
                
                # Cancel the monitoring task
                task = self.active_monitors[symbol]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logger.info(f"RSP monitoring task cancelled for {symbol}")
                
                # Clean up monitoring data
                del self.active_monitors[symbol]
                if symbol in self.symbol_configs:
                    # Get monitoring_id before deleting config
                    config = self.symbol_configs[symbol]
                    monitoring_id = getattr(config, 'monitoring_id', None)
                    
                    # Update database status
                    if monitoring_id:
                        await self._update_monitoring_status(symbol, 'STOPPED', monitoring_id)
                    else:
                        await self._update_monitoring_status(symbol, 'STOPPED')
                    
                    del self.symbol_configs[symbol]
                
                logger.info(f"🛑 Stopped active RSP monitoring for {symbol}")
            else:
                logger.info(f"⚠️ No active monitoring found for {symbol}, but will force clear lock")
                # Try to update database status even if no active monitor
                await self._update_monitoring_status(symbol, 'STOPPED')
            
            # ALWAYS attempt to release the lock, regardless of active monitor status
            lock_released = await self._release_monitoring_lock(symbol)
            
            # Check lock status before and after for detailed reporting
            lock_status_after = await self._check_lock_status(symbol)
            
            # Check if no more active monitors
            if not self.active_monitors:
                self.is_running = False
            
            # Create appropriate response message
            if had_active_monitor:
                message = f"RSP monitoring stopped and lock cleared for {symbol}"
                notification_title = f"🛑 RSP Monitoring Stopped - {symbol}"
                notification_message = f"Range of Strike Prices monitoring has been stopped for {symbol}\n🔓 Monitoring lock released"
            else:
                message = f"No active monitoring found for {symbol}, but lock cleared"
                notification_title = f"🧹 RSP Lock Cleared - {symbol}"
                notification_message = f"No active monitoring was found for {symbol}, but any existing lock has been cleared\n🔓 Lock forcefully released"
            
            logger.info(f"✅ {message}")
            if lock_released:
                logger.info(f"🔓 Successfully released monitoring lock for {symbol}")
            else:
                logger.info(f"ℹ️ No lock found to release for {symbol} (may have already been cleared)")
            
            # Send notification
            try:
                await self._send_rsp_notification(
                    notification_title,
                    notification_message
                )
            except Exception as notify_error:
                logger.error(f"❌ Failed to send stop notification for {symbol}: {notify_error}")
            
            return {
                "success": True,
                "message": message,
                "had_active_monitor": had_active_monitor,
                "lock_released": lock_released,
                "lock_status_after_cleanup": lock_status_after,
                "force_cleanup": True  # Indicate this endpoint always attempts cleanup
            }
            
        except Exception as e:
            logger.error(f"❌ Error stopping RSP monitoring for {symbol}: {e}")
            # Try to release lock even on error
            try:
                await self._release_monitoring_lock(symbol)
                logger.info(f"🧹 Emergency lock cleanup attempted for {symbol}")
            except Exception as cleanup_error:
                logger.error(f"❌ Emergency lock cleanup failed for {symbol}: {cleanup_error}")
            raise
    
    async def _monitor_symbol_rsp(self, symbol: str, config: RSPConfig):
        """Main monitoring loop for RSP strategy with lock management"""
        logger.info(f"🔍 Starting RSP monitoring loop for {symbol}")
        
        # Wait for market open if started before 9:15 AM
        if is_before_market_open():
            logger.info(f"⏰ RSP monitoring for {symbol} waiting for market open...")
            await wait_for_market_open()
            logger.info(f"🟢 Market open reached - starting active monitoring for {symbol}")
        
        # If strikes weren't calculated before market open, calculate them now
        if not config.calculated_strikes:
            logger.info(f"🔢 Calculating strikes for {symbol} now that market is open...")
            try:
                calculated_strikes = await self._get_rsp_strikes_from_crossover_events(symbol)
                config.calculated_strikes = calculated_strikes
                logger.info(f"✅ Calculated strikes for {symbol}: {calculated_strikes}")
            except Exception as e:
                logger.error(f"❌ Failed to calculate strikes for {symbol}: {e}")
                # Continue with empty strikes - monitoring will handle this
        
        # OPTIMIZATION: Initialize strike cache once at start of monitoring
        await self._initialize_strike_cache(config.monitoring_id)
        
        last_lock_renewal = datetime.now()
        lock_renewal_interval = LOCK_TTL_SECONDS // 2  # Renew at half TTL
        
        try:
            while self.is_running and not asyncio.current_task().cancelled():
                # Check trading hours
                if not is_trading_hours():
                    logger.info(f"⏰ Outside trading hours - pausing RSP monitoring for {symbol}")
                    await asyncio.sleep(60)  # Check again in 1 minute
                    continue
                
                # Step 1: Get latest price from Redis
                latest_price_data = await self.get_latest_price_from_redis(symbol)
                
                if not latest_price_data:
                    # No latest price available - send notification and continue
                    await self._send_no_price_notification(symbol)
                    logger.warning(f"⚠️ No latest price for {symbol} - skipping iteration")
                    await asyncio.sleep(config.monitoring_interval)
                    continue
                
                # Extract price information
                current_price = latest_price_data.get('last_price') or latest_price_data.get('ltp')
                if current_price:
                    pass
                    # logger.debug(f"📊 Current price for {symbol}: {current_price}")
                else:
                    logger.warning(f"⚠️ Price data exists but no 'last_price' or 'ltp' field found for {symbol}")
                    await asyncio.sleep(config.monitoring_interval)
                    continue
                
                # Renew lock periodically to maintain ownership
                current_time = datetime.now()
                if (current_time - last_lock_renewal).total_seconds() > lock_renewal_interval:
                    await self._renew_monitoring_lock(symbol)
                    last_lock_renewal = current_time
                
                # OPTIMIZED: Skip redundant strike_range_data creation
                # All monitoring logic now uses cached strike records directly
                logger.debug(f"🎯 RSP monitoring active for {symbol} at ₹{current_price}")
                
                # Core monitoring logic using cached strike data (NO redundant API calls)
                await self._analyze_strike_range(symbol, [], config, current_price, latest_price_data)
                
                # Wait for next iteration
                await asyncio.sleep(config.monitoring_interval)
                
        except asyncio.CancelledError:
            logger.info(f"📴 RSP monitoring cancelled for {symbol}")
            raise
        except Exception as e:
            logger.error(f"❌ Error in RSP monitoring for {symbol}: {e}")
            
            # Send error notification
            try:
                await self._send_rsp_notification(
                    f"🚨 RSP Monitoring Error - {symbol}",
                    f"RSP monitoring encountered an error and will be stopped\n\n"
                    f"Error: {str(e)}\n"
                    f"Symbol: {symbol}\n"
                    f"Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"🔧 Action: Check logs and restart monitoring if needed"
                )
            except Exception as notify_error:
                logger.error(f"❌ Failed to send error notification: {notify_error}")
            
            # Update database status to ERROR
            await self._update_monitoring_status(symbol, 'ERROR')
            
            raise
        finally:
            # Always release lock when monitoring ends (normal or error)
            await self._release_monitoring_lock(symbol)
            
            # CLEANUP: Clear caches for this monitoring session
            if config and config.monitoring_id:
                monitoring_id = config.monitoring_id
                if monitoring_id in self.strike_cache:
                    del self.strike_cache[monitoring_id]
                    logger.debug(f"🧹 Cleared strike cache for {monitoring_id}")
                if monitoring_id in self.previous_prices:
                    del self.previous_prices[monitoring_id]
                    logger.debug(f"🧹 Cleared previous prices cache for {monitoring_id}")
                if monitoring_id in self.last_cache_refresh:
                    del self.last_cache_refresh[monitoring_id]
                    logger.debug(f"🧹 Cleared cache refresh timestamp for {monitoring_id}")
            
            # Clean up monitoring data and update database status
            if symbol in self.active_monitors:
                del self.active_monitors[symbol]
            if symbol in self.symbol_configs:
                config = self.symbol_configs[symbol]
                monitoring_id = getattr(config, 'monitoring_id', None)
                
                # Update database status to STOPPED if not already ERROR
                if monitoring_id:
                    # Check current status before updating
                    try:
                        db = SessionLocal()
                        session = db.query(RSPMonitoring).filter(
                            RSPMonitoring.monitoring_id == monitoring_id
                        ).first()
                        if session and session.monitoring_status not in ['ERROR', 'STOPPED']:
                            await self._update_monitoring_status(symbol, 'STOPPED', monitoring_id)
                        db.close()
                    except Exception as e:
                        logger.error(f"❌ Error checking final database status: {e}")
                
                del self.symbol_configs[symbol]
                
            logger.info(f"🏁 RSP monitoring completed for {symbol}, lock released")
    
    async def _renew_monitoring_lock(self, symbol: str):
        """Renew monitoring lock to maintain ownership"""
        try:
            if self.redis_client is None:
                logger.warning(f"⚠️ Redis client not available for lock renewal: {symbol}")
                return
            
            lock_key = self._get_lock_key(symbol)
            lock_value = f"rsp_monitor_renewed_{datetime.now().isoformat()}"
            
            # Extend the TTL
            renewed = await self.redis_client.set(
                lock_key, 
                lock_value, 
                ex=LOCK_TTL_SECONDS  # Reset TTL
            )
            
            if renewed:
                logger.debug(f"🔄 Renewed monitoring lock for {symbol} (TTL: {LOCK_TTL_SECONDS}s)")
            else:
                logger.warning(f"⚠️ Failed to renew lock for {symbol}")
                
        except Exception as e:
            logger.error(f"❌ Error renewing lock for {symbol}: {e}")
    
    async def _get_strike_range_data(self, symbol: str, config: RSPConfig) -> List[StrikeRangeData]:
        """Get strike range data for the symbol using pre-calculated strikes from config"""
        try:
            logger.info(f"🔍 Getting RSP strike range data for {symbol}")
            
            # Use pre-calculated strikes from config (calculated once before monitoring starts)
            selected_strikes = config.calculated_strikes
            
            if not selected_strikes:
                logger.error(f"❌ No pre-calculated strikes found in config for {symbol}")
                return []
            
            # Get data for all selected strikes
            strike_range = []
            for i, strike_price in enumerate(selected_strikes):
                # Calculate distance from middle strike for reference
                middle_index = len(selected_strikes) // 2
                distance_from_middle = i - middle_index
                
                # Check if this is the ATM strike
                current_atm = await self._get_current_atm_strike_price(symbol)
                is_atm = (current_atm is not None and abs(strike_price - current_atm) < 1.0)
                
                strike_data = await self._get_strike_data(symbol, strike_price, distance_from_middle)
                if strike_data:
                    # Update the is_atm flag based on actual ATM calculation
                    strike_data.is_atm = is_atm
                    strike_range.append(strike_data)
                    logger.debug(f"📊 Added strike: {strike_price} {'(ATM)' if is_atm else ''}")
            
            logger.info(f"✅ Successfully built strike range with {len(strike_range)} strikes for {symbol}")
            return strike_range
            
        except Exception as e:
            logger.error(f"Error getting RSP strike range data for {symbol}: {e}")
            return []
    
    async def _get_rsp_strikes_from_crossover_events(self, symbol: str) -> List[float]:
        """Get RSP strikes - use manual strikes if provided, otherwise fetch from crossover_events table"""
        try:
            # Step 1: Check if manual meeting point strikes are provided
            config = self.symbol_configs.get(symbol)
            manual_strikes = config.manual_meetingpoint_strikes if config else None
            
            if manual_strikes:
                logger.info(f"🎯 Using manual meeting point strikes for {symbol}: {manual_strikes}")
                logger.info(f"✅ Skipping database query - manual strikes provided")
                yesterday_strikes = sorted([float(strike) for strike in manual_strikes])
            else:
                logger.info(f"🔍 No manual strikes provided - querying crossover_events for {symbol}")
                # Get database session only when needed
                db = SessionLocal()
                try:
                    yesterday_strikes = await self._get_yesterday_crossover_strikes(symbol, db)
                finally:
                    db.close()
            
            # Step 2: Get current ATM strike
            current_atm = await self._get_current_atm_strike_price(symbol)
            if current_atm is None:
                logger.error(f"❌ Could not determine current ATM strike for {symbol}")
                return []
            
            # Step 3: Apply strike-range selection logic
            selected_strikes = self._calculate_strike_range(symbol, current_atm, yesterday_strikes)
            
            logger.info(f"📊 Strike selection summary for {symbol}:")
            logger.info(f"   Current ATM: {current_atm}")
            logger.info(f"   Yesterday strikes: {yesterday_strikes} {'(manual)' if manual_strikes else '(from DB)'}")
            logger.info(f"   Selected strikes: {selected_strikes}")
            
            return selected_strikes
            
        except Exception as e:
            logger.error(f"Error getting RSP strikes for {symbol}: {e}")
            return []
    
    async def _get_yesterday_crossover_strikes(self, symbol: str, db: Session) -> List[float]:
        """Get yesterday's (last trading day) crossover strikes from database"""
        try:
            # Calculate the actual last trading day
            last_trading_date = self._get_last_trading_day()
            logger.info(f"📅 Calculated last trading day: {last_trading_date}")
            
            # Get all unique strikes for that symbol and date
            start_of_day = datetime.combine(last_trading_date, datetime.min.time())
            end_of_day = start_of_day + timedelta(days=1)
            
            strikes_result = db.query(CrossoverEvent.strike).filter(
                CrossoverEvent.symbol == symbol,
                CrossoverEvent.created_at >= start_of_day,
                CrossoverEvent.created_at < end_of_day
            ).distinct().order_by(CrossoverEvent.strike.asc()).all()
            
            # Extract and sort strike prices
            strikes = sorted([float(row[0]) for row in strikes_result])
            
            if strikes:
                logger.info(f"📊 Found {len(strikes)} strikes for {symbol} on last trading day ({last_trading_date}): {strikes}")
            else:
                logger.warning(f"⚠️ No crossover events found for {symbol} on last trading day ({last_trading_date})")
                
                # Try to find the most recent crossover events as fallback
                recent_event = db.query(CrossoverEvent).filter(
                    CrossoverEvent.symbol == symbol
                ).order_by(CrossoverEvent.created_at.desc()).first()
                
                if recent_event:
                    fallback_date = recent_event.created_at.date()
                    days_ago = (datetime.now().date() - fallback_date).days
                    logger.info(f"📊 Fallback: Most recent crossover events were {days_ago} days ago on {fallback_date}")
                    
                    # If recent events are within reasonable range (e.g., 7 days), use them
                    if days_ago <= 7:
                        logger.info(f"📊 Using fallback data from {fallback_date} (within 7 days)")
                        fallback_start = datetime.combine(fallback_date, datetime.min.time())
                        fallback_end = fallback_start + timedelta(days=1)
                        
                        fallback_strikes = db.query(CrossoverEvent.strike).filter(
                            CrossoverEvent.symbol == symbol,
                            CrossoverEvent.created_at >= fallback_start,
                            CrossoverEvent.created_at < fallback_end
                        ).distinct().order_by(CrossoverEvent.strike.asc()).all()
                        
                        strikes = sorted([float(row[0]) for row in fallback_strikes])
                        logger.info(f"📊 Fallback strikes: {strikes}")
                    else:
                        logger.warning(f"⚠️ Recent events too old ({days_ago} days), no fallback used")
                else:
                    logger.warning(f"⚠️ No crossover events found at all for {symbol}")
            
            return strikes
            
        except Exception as e:
            logger.error(f"Error getting yesterday crossover strikes for {symbol}: {e}")
            return []
    
    def _get_last_trading_day(self) -> datetime.date:
        """
        Calculate the last trading day (Monday-Friday, excluding weekends)
        - If today is Monday: last trading day = Friday
        - If today is Tuesday-Friday: last trading day = previous day
        - If today is Saturday/Sunday: last trading day = Friday
        
        Note: This doesn't handle market holidays - you may want to add holiday logic
        """
        today = datetime.now().date()
        current_weekday = today.weekday()  # 0=Monday, 6=Sunday
        
        if current_weekday == 0:  # Monday
            # Last trading day was Friday (3 days ago)
            last_trading_day = today - timedelta(days=3)
            logger.debug(f"📅 Today is Monday, last trading day: Friday ({last_trading_day})")
        elif current_weekday == 6:  # Sunday  
            # Last trading day was Friday (2 days ago)
            last_trading_day = today - timedelta(days=2)
            logger.debug(f"📅 Today is Sunday, last trading day: Friday ({last_trading_day})")
        elif current_weekday == 5:  # Saturday
            # Last trading day was Friday (1 day ago)
            last_trading_day = today - timedelta(days=1)
            logger.debug(f"📅 Today is Saturday, last trading day: Friday ({last_trading_day})")
        else:  # Tuesday-Friday (1-4)
            # Last trading day was previous day
            last_trading_day = today - timedelta(days=1)
            logger.debug(f"📅 Today is weekday, last trading day: {last_trading_day}")
        
        return last_trading_day
    
    async def _get_current_atm_strike_price(self, symbol: str) -> Optional[float]:
        """Get current ATM strike price by calling the ATM API and extracting the strike"""
        try:
            # Get current price from Redis first
            latest_price_data = await self.get_latest_price_from_redis(symbol)
            if not latest_price_data:
                logger.error(f"❌ No current price data available for {symbol}")
                return None
            
            current_price = latest_price_data.get('last_price') or latest_price_data.get('ltp')
            if not current_price:
                logger.error(f"❌ No valid current price found for {symbol}")
                return None
            
            # Calculate ATM strike using current price and strike step
            strike_step = self._get_strike_interval(symbol)
            atm_strike = round(current_price / strike_step) * strike_step
            
            # logger.info(f"📊 Current price for {symbol}: {current_price}")
            # logger.info(f"📊 Strike step for {symbol}: {strike_step}")
            # logger.info(f"📊 Calculated ATM strike: {atm_strike}")
            
            return float(atm_strike)
            
        except Exception as e:
            logger.error(f"Error calculating current ATM strike for {symbol}: {e}")
            return None
    
    def _calculate_strike_range(self, symbol: str, current_atm: float, yesterday_strikes: List[float]) -> List[float]:
        """
        Core algorithm: Determine strike range based on current ATM and yesterday's strikes
        
        Cases:
        1. ATM > y_high: include all strikes from y_high up to ATM (inclusive)
        2. ATM < y_low: include strikes from ATM up to y_low (inclusive)  
        3. ATM == any strike in y_strikes:
           - If >1 yesterday strike: include all between y_low & y_high
           - If single yesterday strike: monitor only that ATM strike
        4. ATM inside yesterday's range but not equal: include between y_low & y_high
        """
        try:
            strike_step = self._get_strike_interval(symbol)
            
            # If no yesterday strikes, use only current ATM
            if not yesterday_strikes:
                logger.info(f"� No yesterday strikes - using current ATM only: {current_atm}")
                return [current_atm]
            
            # Get yesterday's range
            y_low = min(yesterday_strikes)
            y_high = max(yesterday_strikes)
            
            logger.info(f"📊 Yesterday range: {y_low} to {y_high}")
            logger.info(f"📊 Current ATM: {current_atm}")
            
            selected_strikes = []
            
            # Case 1: ATM > y_high
            if current_atm > y_high:
                logger.info(f"� Case 1: ATM ({current_atm}) > y_high ({y_high})")
                # Include all strikes from y_high up to ATM (inclusive)
                strike = y_high
                while strike <= current_atm:
                    selected_strikes.append(strike)
                    strike += strike_step
                logger.info(f"📊 Selected strikes from y_high to ATM: {selected_strikes}")
            
            # Case 2: ATM < y_low  
            elif current_atm < y_low:
                logger.info(f"📉 Case 2: ATM ({current_atm}) < y_low ({y_low})")
                # Include strikes from ATM up to y_low (inclusive)
                strike = current_atm
                while strike <= y_low:
                    selected_strikes.append(strike)
                    strike += strike_step
                logger.info(f"📊 Selected strikes from ATM to y_low: {selected_strikes}")
            
            # Case 3: ATM equals one of yesterday's strikes
            elif current_atm in yesterday_strikes:
                logger.info(f"🎯 Case 3: ATM ({current_atm}) equals yesterday strike")
                
                if len(yesterday_strikes) > 1:
                    # Multiple yesterday strikes: include all between y_low & y_high
                    logger.info(f"📊 Multiple yesterday strikes - including full range")
                    strike = y_low
                    while strike <= y_high:
                        selected_strikes.append(strike)
                        strike += strike_step
                else:
                    # Single yesterday strike: monitor only that ATM strike
                    logger.info(f"📊 Single yesterday strike - monitoring ATM only")
                    selected_strikes = [current_atm]
                
                logger.info(f"📊 Selected strikes for ATM match: {selected_strikes}")
            
            # Case 4: ATM inside yesterday's range but not equal
            elif y_low < current_atm < y_high:
                logger.info(f"🎯 Case 4: ATM ({current_atm}) inside yesterday range ({y_low} to {y_high})")
                # Include all strikes between y_low & y_high (inclusive)
                strike = y_low
                while strike <= y_high:
                    selected_strikes.append(strike)
                    strike += strike_step
                logger.info(f"📊 Selected strikes for range coverage: {selected_strikes}")
            
            else:
                # Fallback case (should not happen with proper logic)
                logger.warning(f"⚠️ Unexpected case - using current ATM as fallback")
                selected_strikes = [current_atm]
            
            # Remove duplicates and sort
            selected_strikes = sorted(list(set(selected_strikes)))
            
            # Ensure all strikes are valid (positive and properly stepped)
            valid_strikes = []
            for strike in selected_strikes:
                if strike > 0 and strike % strike_step == 0:
                    valid_strikes.append(strike)
                else:
                    logger.warning(f"⚠️ Invalid strike {strike} (step: {strike_step}) - skipping")
            
            logger.info(f"✅ Final selected strikes for {symbol}: {valid_strikes}")
            return valid_strikes
            
        except Exception as e:
            logger.error(f"Error calculating strike range for {symbol}: {e}")
            # Fallback to current ATM only
            return [current_atm] if current_atm else []
    
    def _get_strike_interval(self, symbol: str) -> float:
        """Get strike interval for the symbol"""
        intervals = {
            "NIFTY": 50,
            "BANKNIFTY": 100,
            "SENSEX": 100,
            "BANKEX": 100
        }
        return intervals.get(symbol, 50)
    
    async def _get_ltp_price(self, token: int) -> Optional[float]:
        """Get LTP price from streaming API only - no fallback methods"""
        try:
            # Only use streaming tick API - no fallback methods
            url = f"{self.base_url}/api/streaming/tick/latest/{token}"
            response = await self.http_client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success") and data.get("data"):
                    ltp = data["data"].get("last_price")
                    if ltp and ltp > 0:
                        return float(ltp)
                    else:
                        logger.error(f"❌ Invalid LTP data for token {token}: {data}")
                        await self._send_price_fetch_error_notification(token, "Invalid LTP data received")
                        return None
                else:
                    logger.error(f"❌ API response error for token {token}: {data}")
                    await self._send_price_fetch_error_notification(token, "API response error")
                    return None
            else:
                logger.error(f"❌ HTTP error {response.status_code} for token {token}")
                await self._send_price_fetch_error_notification(token, f"HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Exception getting LTP for token {token}: {e}")
            await self._send_price_fetch_error_notification(token, f"Exception: {str(e)}")
            return None
    
    async def _send_price_fetch_error_notification(self, token: int, error_message: str):
        """Send Pushover notification when price fetching fails"""
        try:
            title = "🚨 RSP Price Fetch Error"
            message = f"Failed to get LTP price from streaming API\n\n"
            message += f"🎫 Token: {token}\n"
            message += f"❌ Error: {error_message}\n"
            message += f"🔗 URL: {self.base_url}/api/streaming/tick/latest/{token}\n"
            message += f"⏰ Time: {get_naive_ist_now().strftime('%H:%M:%S')}\n\n"
            message += f"⚠️ No fallback methods - RSP strategy requires streaming API"
            
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=1  # High priority for price fetch errors
            )
            
            if success:
                logger.info(f"📱 Price fetch error notification sent for token {token}")
            else:
                logger.error(f"📱 Failed to send price fetch error notification for token {token}")
                
        except Exception as e:
            logger.error(f"Error sending price fetch error notification: {e}")
    
    async def _get_strike_data(self, symbol: str, strike_price: float, distance_from_atm: int) -> Optional[StrikeRangeData]:
        """Get data for a specific strike using tokens from database and prices from Redis"""
        try:
            # Get CE/PE tokens from database (created during initialization)
            db = SessionLocal()
            try:
                # Find active monitoring session
                session = db.query(RSPMonitoring).filter(
                    RSPMonitoring.symbol == symbol,
                    RSPMonitoring.monitoring_status == 'ACTIVE'
                ).first()
                
                if not session:
                    logger.warning(f"⚠️ No active monitoring session found for {symbol}")
                    return None
                
                # Find strike record for this specific strike
                strike_record = db.query(RSPMonitoringStrike).filter(
                    RSPMonitoringStrike.monitoring_id == session.monitoring_id,
                    RSPMonitoringStrike.strike == strike_price
                ).first()
                
                if not strike_record:
                    logger.warning(f"⚠️ No strike record found for {symbol} strike {strike_price}")
                    return None
                
                # Get real tokens and symbols from database (set during initialization)
                ce_token = int(strike_record.ce_token) if strike_record.ce_token else 0
                pe_token = int(strike_record.pe_token) if strike_record.pe_token else 0
                ce_symbol = strike_record.ce_symbol or f"{symbol}{int(strike_price)}CE"
                pe_symbol = strike_record.pe_symbol or f"{symbol}{int(strike_price)}PE"
                
            finally:
                db.close()
            
            # Get CE and PE prices from Redis using the working method (same as symbol prices)
            ce_price = 0.0
            pe_price = 0.0
            
            if ce_token > 0:
                ce_tick_data = await self.get_latest_tick(ce_token)
                if ce_tick_data:
                    ce_price_result = ce_tick_data.get('last_price') or ce_tick_data.get('ltp')
                    if ce_price_result is not None:
                        ce_price = ce_price_result
                    else:
                        logger.debug(f"⚠️ No valid CE price in tick data for {symbol} {strike_price}")
                else:
                    logger.debug(f"⚠️ Could not get CE tick data from Redis for {symbol} {strike_price}")
            
            if pe_token > 0:
                pe_tick_data = await self.get_latest_tick(pe_token)
                if pe_tick_data:
                    pe_price_result = pe_tick_data.get('last_price') or pe_tick_data.get('ltp')
                    if pe_price_result is not None:
                        pe_price = pe_price_result
                    else:
                        logger.debug(f"⚠️ No valid PE price in tick data for {symbol} {strike_price}")
                else:
                    logger.debug(f"⚠️ Could not get PE tick data from Redis for {symbol} {strike_price}")
            
            return StrikeRangeData(
                strike_price=strike_price,
                ce_token=ce_token,
                pe_token=pe_token,
                ce_symbol=ce_symbol,
                pe_symbol=pe_symbol,
                ce_price=ce_price,
                pe_price=pe_price,
                distance_from_atm=distance_from_atm,
                is_atm=(distance_from_atm == 0)
            )
            
        except Exception as e:
            logger.error(f"Error getting strike data for {symbol} {strike_price}: {e}")
            return None
    
    async def _send_strike_data_error_notification(self, symbol: str, strike_price: float, error_message: str):
        """Send Pushover notification when strike data fetching fails"""
        try:
            title = f"🚨 RSP Strike Data Error - {symbol}"
            message = f"Failed to get strike data for RSP strategy\n\n"
            message += f"📊 Symbol: {symbol}\n"
            message += f"💰 Strike: {strike_price}\n"
            message += f"❌ Error: {error_message}\n"
            message += f"⏰ Time: {get_naive_ist_now().strftime('%H:%M:%S')}\n\n"
            message += f"⚠️ Strike data required for RSP range analysis"
            
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=1  # High priority for strike data errors
            )
            
            if success:
                logger.info(f"📱 Strike data error notification sent for {symbol} {strike_price}")
            else:
                logger.error(f"📱 Failed to send strike data error notification for {symbol} {strike_price}")
                
        except Exception as e:
            logger.error(f"Error sending strike data error notification: {e}")
    
    async def _analyze_strike_range(self, symbol: str, strike_range_data: List[StrikeRangeData], 
                                   config: RSPConfig, current_price: float, price_data: dict):
        """Core RSP monitoring logic - analyze strikes for entry points and crossovers"""
        try:
            monitoring_id = config.monitoring_id
            if not monitoring_id:
                logger.error(f"❌ No monitoring_id found in config for {symbol}")
                return
            
            # OPTIMIZED: No longer use strike_range_data parameter - use cached data instead
            logger.debug(f"🔍 RSP Core Monitoring: {symbol} - Price: ₹{current_price}")
            
            # EFFICIENCY FIX: Use cached strike records instead of DB query every cycle
            await self._refresh_strike_cache_if_needed(monitoring_id)
            
            if monitoring_id not in self.strike_cache or not self.strike_cache[monitoring_id]:
                logger.warning(f"⚠️ No cached strike records found for monitoring_id {monitoring_id}")
                return
            
            strike_records = self.strike_cache[monitoring_id]
            logger.debug(f"📊 Processing {len(strike_records)} cached strikes")
            
            # Process each strike record
            for strike_record in strike_records:
                await self._process_strike_monitoring_optimized(
                    symbol, strike_record, config
                )
                
        except Exception as e:
            logger.error(f"❌ Error in RSP core monitoring for {symbol}: {e}")
    
    async def _process_strike_monitoring_optimized(self, symbol: str, strike_record: RSPMonitoringStrike, 
                                                 config: RSPConfig):
        """OPTIMIZED: Process monitoring for a single strike without DB session"""
        try:
            strike_price = strike_record.strike
            monitoring_id = config.monitoring_id
            
            logger.debug(f"📊 Processing strike {strike_price} for {symbol}")
            
            # Get latest prices for CE and PE tokens
            ce_latest_price = None
            pe_latest_price = None
            ce_previous_price = None
            pe_previous_price = None
            
            if strike_record.ce_token:
                ce_tick_data = await self.get_latest_tick(int(strike_record.ce_token))
                if ce_tick_data:
                    ce_latest_price = ce_tick_data.get('last_price') or ce_tick_data.get('ltp')
                    if ce_latest_price:
                        ce_latest_price = float(ce_latest_price)
                        # Get previous price for crossover detection
                        ce_previous_price = self.previous_prices[monitoring_id].get(strike_record.ce_token)
            
            if strike_record.pe_token:
                pe_tick_data = await self.get_latest_tick(int(strike_record.pe_token))
                if pe_tick_data:
                    pe_latest_price = pe_tick_data.get('last_price') or pe_tick_data.get('ltp')
                    if pe_latest_price:
                        pe_latest_price = float(pe_latest_price)
                        # Get previous price for crossover detection
                        pe_previous_price = self.previous_prices[monitoring_id].get(strike_record.pe_token)
            
            if ce_latest_price is None and pe_latest_price is None:
                logger.warning(f"⚠️ No latest prices available for strike {strike_price}")
                return
            
            logger.debug(f"💰 Strike {strike_price}: CE=₹{ce_latest_price} (prev: ₹{ce_previous_price}), PE=₹{pe_latest_price} (prev: ₹{pe_previous_price})")
            
            # CONDITION 1: Entry-point breach check for CE
            
            if ce_latest_price is not None and strike_record.ce_reentry_point is not None:
                await self._check_entry_point_breach_optimized(
                    symbol, strike_record, 'CE', ce_latest_price, 
                    float(strike_record.ce_reentry_point), config
                )
            
            # CONDITION 1: Entry-point breach check for PE
            if pe_latest_price is not None and strike_record.pe_reentry_point is not None:
                await self._check_entry_point_breach_optimized(
                    symbol, strike_record, 'PE', pe_latest_price,
                    float(strike_record.pe_reentry_point), config
                )
            
            # print(f"CE latest : {ce_latest_price}, CE reentry price : {strike_record.ce_reentry_point}")
            # print(f"PE latest : {pe_latest_price}, PE reentry price : {strike_record.pe_reentry_point}")
            # CONDITION 2: FIXED Crossover/Meetingpoint monitoring using previous prices
            if (ce_latest_price is not None and pe_latest_price is not None and 
                ce_previous_price is not None and pe_previous_price is not None):
                
                await self._check_crossover_meetingpoint_fixed(
                    symbol, strike_record, 
                    ce_latest_price, pe_latest_price, 
                    ce_previous_price, pe_previous_price, 
                    config
                )
            
            # Update previous prices for next cycle
            if ce_latest_price is not None:
                self.previous_prices[monitoring_id][strike_record.ce_token] = ce_latest_price
            if pe_latest_price is not None:
                self.previous_prices[monitoring_id][strike_record.pe_token] = pe_latest_price
                
        except Exception as e:
            logger.error(f"❌ Error processing strike {strike_record.strike} for {symbol}: {e}")
    
    async def _check_entry_point_breach_optimized(self, symbol: str, strike_record: RSPMonitoringStrike, 
                                                option_type: str, latest_price: float, reentry_point: float,
                                                config: RSPConfig):
        """OPTIMIZED: Check entry point breach without pre-created DB session"""
        try:
            # CONDITION 1A: Check if latest_price <= entry_point (reentry_point)
            if latest_price <= reentry_point:
                logger.info(f"🎯 Entry point breach detected for {symbol} {strike_record.strike} {option_type}")
                logger.info(f"   Latest Price: ₹{latest_price} <= Entry Point: ₹{reentry_point}")
                
                # Get instrument token based on option type
                instrument_token = (int(strike_record.ce_token) if option_type == 'CE' 
                                  else int(strike_record.pe_token))
                trading_symbol = (strike_record.ce_symbol if option_type == 'CE' 
                                else strike_record.pe_symbol)
                
                # CONDITION 1B: Check if no open SELL trade exists for this instrument
                if await self._check_no_open_sell_trade_optimized(instrument_token):
                    logger.info(f"✅ No open SELL trade found for {trading_symbol} - proceeding with trade entry")
                    
                    # Execute SELL trade
                    await self._execute_sell_trade_optimized(
                        symbol, strike_record, option_type, latest_price, 
                        instrument_token, trading_symbol, config
                    )
                else:
                    logger.info(f"🚫 Open SELL trade already exists for {trading_symbol} - skipping trade entry")
            else:
                logger.debug(f"📊 No breach for {symbol} {strike_record.strike} {option_type}: ₹{latest_price} > ₹{reentry_point}")
                
        except Exception as e:
            logger.error(f"❌ Error checking entry point breach for {symbol} {strike_record.strike} {option_type}: {e}")
    
    async def _check_no_open_sell_trade_optimized(self, instrument_token: int) -> bool:
        """ATOMIC: Check if no open SELL trade exists for the instrument token with Redis lock"""
        try:
            from database import Trade  # Import Trade model
            
            # ATOMIC LOCK: Prevent race conditions for trade checking
            trade_lock_key = f"trade_check_lock:{instrument_token}"
            
            # Try to acquire atomic lock for this instrument
            async with self.redis_client.lock(trade_lock_key, timeout=5, blocking_timeout=1):
                db = SessionLocal()
                try:
                    # Query for open SELL trades for this instrument
                    open_sell_trade = db.query(Trade).filter(
                        Trade.instrument_token == instrument_token,
                        Trade.transaction_type == 'SELL',
                        Trade.status.in_(['PENDING', 'OPEN', 'ACTIVE'])  # Consider various open status values
                    ).first()
                    
                    if open_sell_trade:
                        logger.debug(f"🚫 Found open SELL trade for token {instrument_token}: {open_sell_trade.id}")
                        return False
                    else:
                        logger.debug(f"✅ No open SELL trade found for token {instrument_token}")
                        return True
                        
                finally:
                    db.close()
                
        except Exception as e:
            logger.error(f"❌ Error checking open SELL trades for token {instrument_token}: {e}")
            return False  # Err on the side of caution - don't trade if we can't verify
    
    async def _execute_sell_trade_optimized(self, symbol: str, strike_record: RSPMonitoringStrike, 
                                          option_type: str, latest_price: float, instrument_token: int,
                                          trading_symbol: str, config: RSPConfig):
        """ATOMIC: Execute a SELL trade with complete race condition prevention"""
        try:
            logger.info(f"🚀 Attempting SELL trade for {symbol} {strike_record.strike} {option_type}")
            
            # ATOMIC TRADE EXECUTION: Prevent all race conditions
            trade_execution_lock_key = f"trade_execution_lock:{instrument_token}"
            
            try:
                # Use Redis distributed lock with timeout
                async with self.redis_client.lock(trade_execution_lock_key, timeout=10, blocking_timeout=2):
                    
                    # DOUBLE-CHECK: Verify no trade was created by another process during lock wait
                    if not await self._final_trade_check_and_insert(
                        symbol, strike_record, option_type, latest_price, 
                        instrument_token, trading_symbol, config
                    ):
                        logger.info(f"� Trade insertion prevented - duplicate detected during atomic check")
                        return
                    
                    logger.info(f"✅ SELL trade atomically executed for {trading_symbol}")
                    
            except Exception as lock_error:
                if "Timeout" in str(lock_error):
                    logger.warning(f"⏱️ Trade execution timeout for {trading_symbol} - another process may be trading")
                else:
                    logger.error(f"❌ Trade execution lock error for {trading_symbol}: {lock_error}")
                return
                
        except Exception as e:
            logger.error(f"❌ Error in atomic trade execution for {symbol} {strike_record.strike} {option_type}: {e}")
    
    async def _final_trade_check_and_insert(self, symbol: str, strike_record: RSPMonitoringStrike, 
                                          option_type: str, latest_price: float, instrument_token: int,
                                          trading_symbol: str, config: RSPConfig) -> bool:
        """ATOMIC: Final check and insert trade record in single database transaction"""
        try:
            from database import Trade  # Import Trade model
            
            db = SessionLocal()
            try:
                # BEGIN TRANSACTION: Check and insert atomically
                
                # FINAL CHECK: Ensure no trade was inserted by another process
                existing_trade = db.query(Trade).filter(
                    Trade.instrument_token == instrument_token,
                    Trade.transaction_type == 'SELL',
                    Trade.status.in_(['PENDING', 'OPEN', 'ACTIVE'])
                ).first()
                
                if existing_trade:
                    logger.info(f"🚫 Final check failed - trade already exists: {existing_trade.id}")
                    return False
                
                # Prepare trade data (simplified without stop loss)
                trade_data = {
                    'symbol': symbol,
                    'strike': float(strike_record.strike),
                    'option_type': option_type,
                    'instrument_token': instrument_token,
                    'trading_symbol': trading_symbol,
                    'transaction_type': 'SELL',
                    'entry_price': latest_price,
                    'quantity': config.quantity_lots,
                    'is_paper': config.is_paper,
                    'strategy_type': 'RSP',
                    'monitoring_id': config.monitoring_id,
                    'rsp_monitoring_strike_id': strike_record.id,  # Add RSP monitoring strike primary key
                    'created_at': datetime.now(),
                    'status': 'PENDING'  # Initial status
                }
                
                # DEBUG: Check if method exists
                logger.info(f"🔍 DEBUG: Available methods: {[method for method in dir(self) if '_execute_trade' in method]}")
                
                # Execute actual trade (paper or live)
                trade_result = await self._execute_trade(trade_data, config)
                
                if trade_result and isinstance(trade_result, dict) and trade_result.get('success'):
                    # INSERT TRADE RECORD: Only if execution succeeded
                    if config.is_paper:
                        # For paper trading, insert with COMPLETED status
                        trade_data['status'] = 'COMPLETED'
                        trade_data['trade_id'] = trade_result.get('trade_id', f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                    else:
                        # For live trading, use broker's trade ID and status
                        trade_data['trade_id'] = trade_result.get('trade_id')
                        trade_data['status'] = trade_result.get('status', 'PENDING')
                    
                    # Insert trade record
                    trade_record = Trade(**trade_data)
                    db.add(trade_record)
                    db.commit()
                    
                    logger.info(f"✅ Trade record inserted: {trade_record.id} for {trading_symbol}")
                    
                    # Send notification
                    await self._send_trade_entry_notification(
                        symbol, strike_record, option_type, latest_price, 
                        trading_symbol, config
                    )
                    
                    return True
                else:
                    logger.error(f"❌ Trade execution failed for {trading_symbol}: {trade_result}")
                    
                    # Send failure notification using existing DRY method
                    title = f"❌ RSP Trade Failed - {symbol}"
                    if isinstance(trade_result, dict):
                        error_msg = trade_result.get('error', 'Unknown error')
                    else:
                        error_msg = str(trade_result)
                    
                    message = f"🚨 Trade Execution Failed\n\n"
                    message += f"📊 Symbol: {symbol}\n"
                    message += f"💰 Strike: {strike_record.strike}\n"
                    message += f"📈 Option: {option_type} ({trading_symbol})\n"
                    message += f"💸 Entry Price: ₹{latest_price}\n"
                    message += f"📦 Quantity: {config.quantity_lots} lot(s)\n"
                    message += f"❌ Error: {error_msg}\n"
                    message += f"💼 Mode: {'Paper Trading' if config.is_paper else 'Live Trading'}\n"
                    message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
                    
                    await self._send_rsp_notification(title, message)
                    
                    return False
                    
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Database transaction failed for {trading_symbol}: {e}")
                return False
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in final trade check and insert: {e}")
            return False
    
    async def _check_crossover_meetingpoint_fixed(self, symbol: str, strike_record: RSPMonitoringStrike,
                                                ce_latest_price: float, pe_latest_price: float,
                                                ce_previous_price: float, pe_previous_price: float,
                                                config: RSPConfig):
        """FIXED: Check for crossover/meetingpoint using previous vs current prices"""
        try:
            strike_price = strike_record.strike
            monitoring_id = config.monitoring_id
            
            # CONDITION 2A: Check for meetingpoint (ce_price == pe_price)
            price_diff = abs(ce_latest_price - pe_latest_price)
            meetingpoint_tolerance = 0.1  # Allow small tolerance for price equality
            
            if price_diff <= meetingpoint_tolerance:
                logger.info(f"🎯 MEETINGPOINT detected for {symbol} strike {strike_price}")
                logger.info(f"   CE: ₹{ce_latest_price}, PE: ₹{pe_latest_price} (diff: ₹{price_diff})")
                
                # Use opening prices to determine which option to update
                ce_open_price = float(strike_record.ce_price) if strike_record.ce_price else 0
                pe_open_price = float(strike_record.pe_price) if strike_record.pe_price else 0
                
                await self._handle_crossover_meetingpoint_update_optimized(
                    symbol, strike_record, ce_latest_price, pe_latest_price,
                    ce_open_price, pe_open_price, 'MEETINGPOINT', config
                )
                return
            
            # CONDITION 2B: FIXED - Check for crossover using previous vs current prices
            # CE crossed above PE: was below/equal before, now above
            ce_crossed_above_pe = (ce_previous_price <= pe_previous_price and ce_latest_price > pe_latest_price)
            # PE crossed above CE: was below/equal before, now above  
            pe_crossed_above_ce = (pe_previous_price <= ce_previous_price and pe_latest_price > ce_latest_price)
            
            if ce_crossed_above_pe or pe_crossed_above_ce:
                crossover_type = 'CE_CROSS_UP' if ce_crossed_above_pe else 'PE_CROSS_UP'
                logger.info(f"🔄 CROSSOVER detected for {symbol} strike {strike_price}: {crossover_type}")
                logger.info(f"   CE: ₹{ce_previous_price} → ₹{ce_latest_price}")
                logger.info(f"   PE: ₹{pe_previous_price} → ₹{pe_latest_price}")
                
                # Use opening prices to determine which option to update
                ce_open_price = float(strike_record.ce_price) if strike_record.ce_price else 0
                pe_open_price = float(strike_record.pe_price) if strike_record.pe_price else 0
                
                await self._handle_crossover_meetingpoint_update_optimized(
                    symbol, strike_record, ce_latest_price, pe_latest_price,
                    ce_open_price, pe_open_price, crossover_type, config
                )
                
        except Exception as e:
            logger.error(f"❌ Error checking crossover/meetingpoint for {symbol} strike {strike_record.strike}: {e}")
    
    async def _handle_crossover_meetingpoint_update_optimized(self, symbol: str, strike_record: RSPMonitoringStrike,
                                                            ce_latest_price: float, pe_latest_price: float,
                                                            ce_open_price: float, pe_open_price: float,
                                                            event_type: str, config: RSPConfig):
        """OPTIMIZED: Handle crossover/meetingpoint by updating levels in both DB and cache"""
        try:
            monitoring_threshold = float(config.monitoring_threshold)
            strike_price = strike_record.strike
            monitoring_id = config.monitoring_id
            
            # Determine which option was higher at opening
            if ce_open_price > pe_open_price:
                # CE was higher at open - update CE reentry point and stop loss
                target_option = 'CE'
                target_price = ce_latest_price
                logger.info(f"📈 CE was higher at open (₹{ce_open_price} > ₹{pe_open_price}) - updating CE levels")
            elif pe_open_price > ce_open_price:
                # PE was higher at open - update PE reentry point and stop loss  
                target_option = 'PE'
                target_price = pe_latest_price
                logger.info(f"📈 PE was higher at open (₹{pe_open_price} > ₹{ce_open_price}) - updating PE levels")
            else:
                # Equal at open - update both (or use latest higher price)
                if ce_latest_price >= pe_latest_price:
                    target_option = 'CE'
                    target_price = ce_latest_price
                else:
                    target_option = 'PE'
                    target_price = pe_latest_price
                logger.info(f"📊 Equal at open - updating {target_option} based on current higher price")
            
            # Calculate new reentry point and stop loss
            new_reentry_point = target_price - (target_price * monitoring_threshold / 100)
            new_stop_loss = target_price + (target_price * monitoring_threshold / 100)
            
            logger.info(f"🔄 Updating {target_option} levels for strike {strike_price}:")
            logger.info(f"   Current Price: ₹{target_price}")
            logger.info(f"   New Reentry Point: ₹{new_reentry_point}")
            logger.info(f"   New Stop Loss: ₹{new_stop_loss}")
            
            # CRITICAL FIX: Update both database and cache atomically
            old_reentry = (strike_record.ce_reentry_point if target_option == 'CE' 
                          else strike_record.pe_reentry_point)
            old_stoploss = (strike_record.ce_stoploss if target_option == 'CE' 
                           else strike_record.pe_stoploss)
            
            update_success = await self._update_strike_cache_and_db(
                monitoring_id, strike_record, target_option, new_reentry_point, new_stop_loss
            )
            
            if update_success:
                logger.info(f"✅ Level update complete for {symbol} strike {strike_price} {target_option}")
                logger.info(f"   Reentry: ₹{old_reentry} → ₹{new_reentry_point}")
                logger.info(f"   Stop Loss: ₹{old_stoploss} → ₹{new_stop_loss}")
                
                # Send notification
                await self._send_crossover_meetingpoint_notification(
                    symbol, strike_record, target_option, target_price,
                    new_reentry_point, new_stop_loss, event_type, config
                )
            else:
                logger.error(f"❌ Failed to update levels for {symbol} strike {strike_price}")
                
        except Exception as e:
            logger.error(f"❌ Error handling crossover/meetingpoint update for {symbol} strike {strike_record.strike}: {e}")
    
    async def _send_trade_entry_notification(self, symbol: str, strike_record: RSPMonitoringStrike,
                                           option_type: str, entry_price: float,
                                           trading_symbol: str, config: RSPConfig):
        """Send notification for trade entry"""
        try:
            title = f"🚀 RSP Trade Entry - {symbol}"
            message = f"SELL trade executed for RSP strategy\n\n"
            message += f"📊 Symbol: {symbol}\n"
            message += f"💰 Strike: {strike_record.strike}\n"
            message += f"📈 Option: {option_type} ({trading_symbol})\n"
            message += f"💸 Entry Price: ₹{entry_price}\n"
            message += f"📦 Quantity: {config.quantity_lots} lot(s)\n"
            message += f"🎯 Strategy: Range of Strike Prices (RSP)\n"
            message += f"💼 Mode: {'Paper Trading' if config.is_paper else 'Live Trading'}\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=1  # High priority for trade entries
            )
            
            if success:
                logger.info(f"📱 Trade entry notification sent for {trading_symbol}")
                
                # Log notification to database (implement based on your pushover_notifications table)
                await self._log_pushover_notification(title, message, 'TRADE_ENTRY', config.monitoring_id)
            else:
                logger.error(f"📱 Failed to send trade entry notification for {trading_symbol}")
                
        except Exception as e:
            logger.error(f"❌ Error sending trade entry notification: {e}")
    
    async def _send_crossover_meetingpoint_notification(self, symbol: str, strike_record: RSPMonitoringStrike,
                                                       target_option: str, target_price: float,
                                                       new_reentry_point: float, new_stop_loss: float,
                                                       event_type: str, config: RSPConfig):
        """Send notification for crossover/meetingpoint events"""
        try:
            title = f"🔄 RSP {event_type} - {symbol}"
            message = f"Crossover/Meetingpoint detected for RSP strategy\n\n"
            message += f"📊 Symbol: {symbol}\n"
            message += f"💰 Strike: {strike_record.strike}\n"
            message += f"🎯 Event: {event_type}\n"
            message += f"📈 Updated Option: {target_option}\n"
            message += f"💸 Current Price: ₹{target_price}\n"
            message += f"🎯 New Reentry Point: ₹{new_reentry_point:.2f}\n"
            message += f"🛑 New Stop Loss: ₹{new_stop_loss:.2f}\n"
            message += f"📊 Threshold: {config.monitoring_threshold}%\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=0  # Normal priority for crossover events
            )
            
            if success:
                logger.info(f"📱 Crossover/meetingpoint notification sent for {symbol} strike {strike_record.strike}")
                
                # Log notification to database
                await self._log_pushover_notification(title, message, 'CROSSOVER_MEETINGPOINT', config.monitoring_id)
            else:
                logger.error(f"📱 Failed to send crossover/meetingpoint notification")
                
        except Exception as e:
            logger.error(f"❌ Error sending crossover/meetingpoint notification: {e}")
    
    async def _log_pushover_notification(self, title: str, message: str, notification_type: str, monitoring_id: str):
        """Log pushover notification to database"""
        try:
            # TODO: Implement based on your pushover_notifications table structure
            # This is a placeholder - you'll need to implement based on your actual table schema
            
            db = SessionLocal()
            try:
                # Example implementation - adjust based on your actual table structure
                notification_record = {
                    'title': title,
                    'message': message,
                    'notification_type': notification_type,
                    'monitoring_id': monitoring_id,
                    'sent_at': datetime.now(),
                    'strategy_type': 'RSP'
                }
                
                # Insert into your pushover_notifications table
                # db.add(PushoverNotification(**notification_record))
                # db.commit()
                
                logger.debug(f"📝 Logged notification to database: {notification_type}")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error logging pushover notification to database: {e}")
    
    async def _send_rsp_notification(self, title: str, message: str):
        """Send RSP strategy notification via Pushover only"""
        try:
            success = await send_pushover_notification(
                title=title,
                message=message,
                priority=0  # Normal priority for general RSP notifications
            )
            
            if success:
                logger.info(f"📱 RSP notification sent: {title}")
            else:
                logger.error(f"📱 RSP notification failed: {title}")
                # No fallback - if Pushover fails, log error only
                logger.error(f"📱 Failed notification content: {title} - {message}")
                
        except Exception as e:
            logger.error(f"Error sending RSP notification: {e}")
            # No fallback - only log the error
            logger.error(f"📱 Failed notification content: {title} - {message}")
    
    def get_monitoring_status(self) -> Dict[str, Any]:
        """Get current RSP monitoring status with lock information"""
        return {
            "system_running": self.is_running,
            "active_symbols": list(self.active_monitors.keys()),
            "active_monitors_count": len(self.active_monitors),
            "configurations": {
                symbol: config.__dict__ for symbol, config in self.symbol_configs.items()
            },
            "monitor_status": {
                symbol: {
                    "active": not task.done(),
                    "done": task.done()
                }
                for symbol, task in self.active_monitors.items()
            },
            "acquired_locks": list(self.acquired_locks),
            "redis_connected": self.redis_client is not None,
            "redis_pool_active": self.redis_pool is not None,
            "redis_initialized": self.redis_initialized
        }
    
    async def get_detailed_monitoring_status(self) -> Dict[str, Any]:
        """Get detailed monitoring status including Redis lock information"""
        status = self.get_monitoring_status()
        
        # Add detailed lock information for each symbol
        lock_details = {}
        for symbol in self.active_monitors.keys():
            lock_details[symbol] = await self._check_lock_status(symbol)
        
        status["lock_details"] = lock_details
        return status
    
    # Database Persistence Methods
    
    async def _cleanup_previous_monitoring_sessions(self, symbol: str, session_date: datetime.date = None) -> bool:
        """Clean up previous monitoring sessions for the same symbol on the same day"""
        try:
            if session_date is None:
                session_date = datetime.now().date()
            
            db = SessionLocal()
            try:
                # Find existing active sessions for this symbol on this date
                existing_sessions = db.query(RSPMonitoring).filter(
                    RSPMonitoring.symbol == symbol,
                    RSPMonitoring.session_date == session_date,
                    RSPMonitoring.monitoring_status.in_(['ACTIVE', 'PENDING'])
                ).all()
                
                if existing_sessions:
                    logger.info(f"🧹 Found {len(existing_sessions)} existing sessions for {symbol} on {session_date}")
                    
                    for session in existing_sessions:
                        # Update status to STOPPED
                        session.monitoring_status = 'STOPPED'
                        session.last_updated = datetime.now()
                        logger.info(f"🛑 Marked session {session.monitoring_id} as STOPPED")
                    
                    db.commit()
                    logger.info(f"✅ Cleaned up {len(existing_sessions)} previous sessions for {symbol}")
                    return True
                else:
                    logger.info(f"ℹ️ No previous sessions found for {symbol} on {session_date}")
                    return True
                    
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Error cleaning up previous sessions for {symbol}: {e}")
                return False
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in cleanup_previous_monitoring_sessions for {symbol}: {e}")
            return False
    
    async def _create_monitoring_session(self, symbol: str, config: RSPConfig) -> Optional[str]:
        """Create a new monitoring session in the database and return monitoring_id"""
        try:
            # Clean up previous sessions first
            cleanup_success = await self._cleanup_previous_monitoring_sessions(symbol)
            if not cleanup_success:
                logger.warning(f"⚠️ Previous session cleanup failed for {symbol}, continuing anyway")
            
            db = SessionLocal()
            try:
                # Prepare strikes data
                manual_strikes_str = None
                calculated_strikes_str = None
                
                if config.manual_meetingpoint_strikes:
                    manual_strikes_str = ','.join(map(str, config.manual_meetingpoint_strikes))
                
                if config.calculated_strikes:
                    calculated_strikes_str = ','.join(map(str, config.calculated_strikes))
                
                # Create new monitoring session
                new_session = RSPMonitoring(
                    symbol=symbol,
                    monitoring_threshold=float(config.monitoring_threshold),
                    monitoring_interval=float(config.monitoring_interval),
                    is_paper=config.is_paper,
                    quantity_lots=config.quantity_lots,
                    monitoring_status='ACTIVE',
                    lock_key=self._get_lock_key(symbol),  # Store the Redis lock key
                    manual_meetingpoint_strikes=manual_strikes_str,
                    calculated_strikes=calculated_strikes_str,
                    current_atm_strike=None,  # Will be updated when calculated
                    session_date=datetime.now().date()
                )
                
                db.add(new_session)
                db.flush()  # Get the monitoring_id
                
                monitoring_id = str(new_session.monitoring_id)
                logger.info(f"📊 Created monitoring session {monitoring_id} for {symbol}")
                
                # Create strike records if calculated strikes are available
                if config.calculated_strikes:
                    await self._create_strike_records(db, monitoring_id, config.calculated_strikes, symbol, config)
                
                db.commit()
                logger.info(f"✅ Successfully created monitoring session and strikes for {symbol}")
                return monitoring_id
                
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Error creating monitoring session for {symbol}: {e}")
                return None
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in create_monitoring_session for {symbol}: {e}")
            return None
    
    async def _create_strike_records(self, db: Session, monitoring_id: str, strikes: List[float], symbol: str, config: RSPConfig):
        """Create strike records for the monitoring session"""
        try:
            if not strikes:
                logger.warning(f"⚠️ No strikes provided for monitoring session {monitoring_id}")
                return
            
            # Calculate ATM and distances
            current_atm = await self._get_current_atm_strike_price(symbol)
            
            # Find middle strike for distance calculation
            middle_index = len(strikes) // 2
            
            for i, strike_price in enumerate(strikes):
                # Calculate distance from middle strike
                distance_from_middle = i - middle_index
                
                # Check if this is ATM
                is_atm_strike = (current_atm is not None and abs(strike_price - current_atm) < 1.0)
                
                # Get options data from options_pairs table
                options_data = await self._get_options_data_from_pairs(symbol, strike_price)
                
                if options_data:
                    # Use real instrument tokens and symbols from options_pairs
                    ce_token = str(options_data["ce_instrument_token"])
                    pe_token = str(options_data["pe_instrument_token"])
                    ce_symbol = options_data["ce_trading_symbol"]
                    pe_symbol = options_data["pe_trading_symbol"]
                    
                    # Get latest prices from Redis - Smart 9:15 AM timing logic
                    ce_price = None
                    pe_price = None
                    
                    # Check current time vs market open (9:15 AM)
                    current_time = datetime.now().time()
                    market_open = datetime.strptime("09:15", "%H:%M").time()
                    
                    if current_time < market_open:
                        # Started before 9:15 AM - Wait until exactly 9:15 AM
                        now = datetime.now()
                        market_open_today = datetime.combine(now.date(), market_open)
                        wait_seconds = (market_open_today - now).total_seconds()
                        
                        if wait_seconds > 0:
                            logger.info(f"⏰ Waiting {wait_seconds:.0f} seconds until market open (9:15 AM) for price fetching...")
                            await asyncio.sleep(wait_seconds)
                        
                        # Now fetch live prices at exactly 9:15 AM
                        ce_tick_data = await self.get_latest_tick(options_data["ce_instrument_token"])
                        if ce_tick_data:
                            ce_price = ce_tick_data.get('last_price') or ce_tick_data.get('ltp')
                        
                        pe_tick_data = await self.get_latest_tick(options_data["pe_instrument_token"])
                        if pe_tick_data:
                            pe_price = pe_tick_data.get('last_price') or pe_tick_data.get('ltp')
                            
                        logger.info(f"🕘 Fetched live prices at 9:15 AM - CE: ₹{ce_price}, PE: ₹{pe_price}")
                        
                    else:
                        # Started after 9:15 AM - Get historical 9:15 AM price from stream
                        logger.info(f"🕘 Started after market open, fetching historical 9:15 AM prices from stream...")
                        
                        # Try to get 9:15 AM price first
                        ce_tick_data = await self.get_tick_at_time(options_data["ce_instrument_token"], "09:15")
                        if ce_tick_data:
                            ce_price = ce_tick_data.get('last_price') or ce_tick_data.get('ltp')
                            logger.info(f"📈 Found CE price at 9:15 AM: ₹{ce_price}")
                        else:
                            # Fallback: Get earliest available price from stream
                            logger.info(f"⚠️ No 9:15 AM price found for CE, fetching earliest available...")
                            ce_tick_data = await self.get_earliest_tick_today(options_data["ce_instrument_token"])
                            if ce_tick_data:
                                ce_price = ce_tick_data.get('last_price') or ce_tick_data.get('ltp')
                                logger.info(f"📈 Using earliest available CE price: ₹{ce_price}")
                        
                        # Try to get 9:15 AM price first
                        pe_tick_data = await self.get_tick_at_time(options_data["pe_instrument_token"], "09:15")
                        if pe_tick_data:
                            pe_price = pe_tick_data.get('last_price') or pe_tick_data.get('ltp')
                            logger.info(f"📈 Found PE price at 9:15 AM: ₹{pe_price}")
                        else:
                            # Fallback: Get earliest available price from stream
                            logger.info(f"⚠️ No 9:15 AM price found for PE, fetching earliest available...")
                            pe_tick_data = await self.get_earliest_tick_today(options_data["pe_instrument_token"])
                            if pe_tick_data:
                                pe_price = pe_tick_data.get('last_price') or pe_tick_data.get('ltp')
                                logger.info(f"📈 Using earliest available PE price: ₹{pe_price}")
                            
                        logger.info(f"📈 Retrieved baseline prices - CE: ₹{ce_price}, PE: ₹{pe_price}")
                    
                    # Use prices from options_pairs as fallback if Redis has no data
                    if ce_price is None:
                        ce_price = options_data.get("ce_last_price", 0)
                        logger.debug(f"📊 Using fallback CE price from options_pairs: ₹{ce_price}")
                    if pe_price is None:
                        pe_price = options_data.get("pe_last_price", 0)
                        logger.debug(f"📊 Using fallback PE price from options_pairs: ₹{pe_price}")
                    
                    # Calculate stop loss levels using monitoring threshold for selling strategy
                    ce_stoploss, pe_stoploss = await self._calculate_stop_loss_levels(
                        ce_price or 0, pe_price or 0, config.monitoring_threshold
                    )
                    
                    # Calculate reentry points using monitoring threshold for selling strategy
                    ce_reentry, pe_reentry = await self._calculate_reentry_points(
                        ce_price or 0, pe_price or 0, config.monitoring_threshold
                    )
                    
                    logger.info(f"📊 Strike {strike_price}: CE={ce_symbol} (₹{ce_price}), PE={pe_symbol} (₹{pe_price})")
                    
                else:
                    # Fallback to generated symbols if options_pairs lookup fails
                    ce_token = None
                    pe_token = None
                    ce_symbol = f"{symbol}{int(strike_price)}CE"
                    pe_symbol = f"{symbol}{int(strike_price)}PE"
                    ce_price = 0
                    pe_price = 0
                    ce_stoploss = None
                    pe_stoploss = None
                    ce_reentry = None
                    pe_reentry = None
                    
                    logger.warning(f"⚠️ Using fallback symbols for strike {strike_price}: {ce_symbol}, {pe_symbol}")
                
                # Create strike record
                strike_record = RSPMonitoringStrike(
                    monitoring_id=monitoring_id,
                    strike=float(strike_price),
                    distance_from_atm=distance_from_middle,
                    is_atm=is_atm_strike,
                    ce_token=ce_token,
                    ce_symbol=ce_symbol,
                    ce_price=float(ce_price) if ce_price else 0,
                    ce_reentry_point=float(ce_reentry) if ce_reentry else None,
                    ce_stoploss=float(ce_stoploss) if ce_stoploss else None,
                    ce_last_updated=datetime.now() if ce_price else None,  # 9:15 AM baseline timestamp
                    pe_token=pe_token,
                    pe_symbol=pe_symbol,
                    pe_price=float(pe_price) if pe_price else 0,
                    pe_reentry_point=float(pe_reentry) if pe_reentry else None,
                    pe_stoploss=float(pe_stoploss) if pe_stoploss else None,
                    pe_last_updated=datetime.now() if pe_price else None,  # 9:15 AM baseline timestamp
                    baseline_price_915=float(ce_price or 0),  # Store 9:15 AM baseline price
                    monitoring_threshold=float(config.monitoring_threshold),  # Inherit from main config
                    monitoring_status='PENDING'
                )
                
                db.add(strike_record)
                logger.debug(f"📊 Created strike record: {strike_price} {'(ATM)' if is_atm_strike else ''} - CE: {ce_symbol} (₹{ce_price}, Reentry: ₹{ce_reentry}), PE: {pe_symbol} (₹{pe_price}, Reentry: ₹{pe_reentry})")
            
            logger.info(f"✅ Created {len(strikes)} strike records for monitoring {monitoring_id}")
            
        except Exception as e:
            logger.error(f"❌ Error creating strike records for {monitoring_id}: {e}")
            raise
    
    async def _get_options_data_from_pairs(self, symbol: str, strike_price: float) -> Optional[dict]:
        """Get CE/PE instrument tokens and details from options_pairs table"""
        try:
            db = SessionLocal()
            try:
                # Find the closest expiry for this symbol and strike
                options_pair = db.query(OptionsPairs).filter(
                    OptionsPairs.symbol == symbol,
                    OptionsPairs.strike == float(strike_price),
                    OptionsPairs.is_active == True
                ).order_by(OptionsPairs.expiry.asc()).first()
                
                if options_pair:
                    logger.debug(f"📊 Found options pair for {symbol} {strike_price}: {options_pair.ce_trading_symbol}, {options_pair.pe_trading_symbol}")
                    
                    return {
                        "expiry": options_pair.expiry,
                        "ce_instrument_token": options_pair.ce_instrument_token,
                        "ce_trading_symbol": options_pair.ce_trading_symbol,
                        "ce_lot_size": options_pair.ce_lot_size,
                        "pe_instrument_token": options_pair.pe_instrument_token,
                        "pe_trading_symbol": options_pair.pe_trading_symbol,
                        "pe_lot_size": options_pair.pe_lot_size,
                        "ce_last_price": options_pair.ce_last_price,
                        "pe_last_price": options_pair.pe_last_price
                    }
                else:
                    logger.warning(f"⚠️ No options pair found for {symbol} strike {strike_price}")
                    return None
                    
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error getting options data for {symbol} {strike_price}: {e}")
            return None
    
    async def _calculate_stop_loss_levels(self, ce_price: float, pe_price: float, stop_loss_percentage: float = 20.0) -> tuple:
        """
        Calculate stop loss levels for CE and PE options (SELLING STRATEGY)
        
        For selling strategy: Stop loss = Entry Price + Threshold%
        This protects against losses when the option price increases beyond our threshold
        """
        try:
            ce_stoploss = None
            pe_stoploss = None
            
            if ce_price and ce_price > 0:
                # Selling strategy: Add threshold percentage to entry price
                ce_stoploss = ce_price * (1 + stop_loss_percentage / 100)
                logger.debug(f"📊 Calculated CE stop loss: ₹{ce_stoploss:.2f} (Entry: ₹{ce_price}, Threshold: +{stop_loss_percentage}%)")
            
            if pe_price and pe_price > 0:
                # Selling strategy: Add threshold percentage to entry price
                pe_stoploss = pe_price * (1 + stop_loss_percentage / 100)
                logger.debug(f"📊 Calculated PE stop loss: ₹{pe_stoploss:.2f} (Entry: ₹{pe_price}, Threshold: +{stop_loss_percentage}%)")
            
            return ce_stoploss, pe_stoploss
            
        except Exception as e:
            logger.error(f"❌ Error calculating stop loss levels: {e}")
            return None, None
    
    async def _calculate_reentry_points(self, ce_price: float, pe_price: float, threshold_percentage: float = 20.0) -> tuple:
        """
        Calculate reentry points for CE and PE options (SELLING STRATEGY)
        
        For selling strategy: Reentry point = Entry Price - Threshold%
        This allows reentry when option prices fall below the profitable level
        """
        try:
            ce_reentry = None
            pe_reentry = None
            
            if ce_price and ce_price > 0:
                # Selling strategy: Subtract threshold percentage from entry price
                ce_reentry = ce_price * (1 - threshold_percentage / 100)
                logger.debug(f"📊 Calculated CE reentry point: ₹{ce_reentry:.2f} (Entry: ₹{ce_price}, Threshold: -{threshold_percentage}%)")
            
            if pe_price and pe_price > 0:
                # Selling strategy: Subtract threshold percentage from entry price
                pe_reentry = pe_price * (1 - threshold_percentage / 100)
                logger.debug(f"📊 Calculated PE reentry point: ₹{pe_reentry:.2f} (Entry: ₹{pe_price}, Threshold: -{threshold_percentage}%)")
            
            return ce_reentry, pe_reentry
            
        except Exception as e:
            logger.error(f"❌ Error calculating reentry points: {e}")
            return None, None
    
    async def _update_monitoring_status(self, symbol: str, status: str, monitoring_id: str = None):
        """Update monitoring status in database"""
        try:
            db = SessionLocal()
            try:
                session = None
                
                if monitoring_id:
                    # Update specific session by ID (preferred method)
                    session = db.query(RSPMonitoring).filter(
                        RSPMonitoring.monitoring_id == monitoring_id
                    ).first()
                    logger.debug(f"🔍 Found session by monitoring_id {monitoring_id}: {session is not None}")
                else:
                    # Fallback: Find the most recent session for symbol (any status)
                    session = db.query(RSPMonitoring).filter(
                        RSPMonitoring.symbol == symbol
                    ).order_by(RSPMonitoring.created_at.desc()).first()
                    logger.debug(f"🔍 Found most recent session for {symbol}: {session is not None}")
                
                if session:
                    old_status = session.monitoring_status
                    session.monitoring_status = status
                    session.last_updated = datetime.now()
                    db.commit()
                    logger.info(f"✅ Updated monitoring status for {symbol}: {old_status} → {status} (ID: {session.monitoring_id})")
                    return True
                else:
                    logger.warning(f"⚠️ No monitoring session found for {symbol} (monitoring_id: {monitoring_id})")
                    
                    # Debug: Check what sessions exist for this symbol
                    all_sessions = db.query(RSPMonitoring).filter(
                        RSPMonitoring.symbol == symbol
                    ).all()
                    logger.debug(f"📊 All sessions for {symbol}: {[(s.monitoring_id, s.monitoring_status) for s in all_sessions]}")
                    return False
                    
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Database error updating monitoring status for {symbol}: {e}")
                return False
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in update_monitoring_status for {symbol}: {e}")
            return False
    
    async def _update_strike_prices(self, symbol: str, strike_range_data: List[StrikeRangeData]):
        """
        DEPRECATED: Update strike price data in database using real-time prices from Redis
        
        ⚠️ This method is no longer used in the monitoring loop to follow the principle:
        "Database updates should happen only once when we invoke the start-monitoring API, 
        not as part of the monitoring loop"
        
        The monitoring loop now reads prices directly from Redis without database writes.
        """
        try:
            db = SessionLocal()
            try:
                # Find active monitoring session
                session = db.query(RSPMonitoring).filter(
                    RSPMonitoring.symbol == symbol,
                    RSPMonitoring.monitoring_status == 'ACTIVE'
                ).first()
                
                if not session:
                    logger.warning(f"⚠️ No active monitoring session found for {symbol}")
                    return False
                
                # Get all strike records for this session
                strike_records = db.query(RSPMonitoringStrike).filter(
                    RSPMonitoringStrike.monitoring_id == session.monitoring_id
                ).all()
                
                if not strike_records:
                    logger.warning(f"⚠️ No strike records found for session {session.monitoring_id}")
                    return False
                
                # Update strike prices using Redis data
                updated_count = 0
                for strike_record in strike_records:
                    updated_ce = False
                    updated_pe = False
                    
                    # Update CE price from Redis
                    if strike_record.ce_token:
                        try:
                            ce_token = int(strike_record.ce_token)
                            ce_price = await self._get_latest_option_price_from_redis(ce_token)
                            
                            if ce_price is not None and ce_price > 0:
                                strike_record.ce_price = float(ce_price)
                                strike_record.ce_last_updated = datetime.now()
                                updated_ce = True
                                
                                # Update stop loss if needed
                                if strike_record.ce_stoploss is None:
                                    ce_stoploss, _ = await self._calculate_stop_loss_levels(ce_price, 0)
                                    if ce_stoploss:
                                        strike_record.ce_stoploss = float(ce_stoploss)
                                
                                logger.debug(f"📊 Updated CE price for {strike_record.strike}: ₹{ce_price}")
                            
                        except (ValueError, TypeError) as e:
                            logger.error(f"❌ Invalid CE token for strike {strike_record.strike}: {strike_record.ce_token}")
                    
                    # Update PE price from Redis
                    if strike_record.pe_token:
                        try:
                            pe_token = int(strike_record.pe_token)
                            pe_price = await self._get_latest_option_price_from_redis(pe_token)
                            
                            if pe_price is not None and pe_price > 0:
                                strike_record.pe_price = float(pe_price)
                                strike_record.pe_last_updated = datetime.now()
                                updated_pe = True
                                
                                # Update stop loss if needed
                                if strike_record.pe_stoploss is None:
                                    _, pe_stoploss = await self._calculate_stop_loss_levels(0, pe_price)
                                    if pe_stoploss:
                                        strike_record.pe_stoploss = float(pe_stoploss)
                                
                                logger.debug(f"📊 Updated PE price for {strike_record.strike}: ₹{pe_price}")
                            
                        except (ValueError, TypeError) as e:
                            logger.error(f"❌ Invalid PE token for strike {strike_record.strike}: {strike_record.pe_token}")
                    
                    # Update record timestamp if any price was updated
                    if updated_ce or updated_pe:
                        strike_record.updated_at = datetime.now()
                        updated_count += 1
                
                db.commit()
                logger.info(f"✅ Updated {updated_count} strike records for {symbol} using Redis prices")
                return True
                
            except Exception as e:
                db.rollback()
                logger.error(f"❌ Error updating strike prices for {symbol}: {e}")
                return False
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in update_strike_prices for {symbol}: {e}")
            return False
    
    async def cleanup(self):
        """Cleanup resources and release all locks"""
        logger.info("🧹 Cleaning up RSP strategy resources...")
        
        # Stop all monitoring tasks
        for symbol in list(self.active_monitors.keys()):
            await self.stop_monitoring(symbol)
        
        # Release any remaining locks
        for symbol in list(self.acquired_locks):
            await self._release_monitoring_lock(symbol)
        
        # Close HTTP client
        if hasattr(self, 'http_client'):
            await self.http_client.aclose()
        
        # Close Redis connection
        if self.redis_client:
            await self.redis_client.aclose()
        if self.redis_pool:
            await self.redis_pool.aclose()
            logger.info("✅ Redis connection pool closed")
        
        # Reset Redis initialization state
        self.redis_initialized = False
        self.redis_client = None
        self.redis_pool = None
        
        self.is_running = False
        logger.info("✅ RSP strategy cleanup completed")

    async def _execute_trade(self, trade_data: dict, config: RSPConfig) -> dict:
        """Execute trade using KiteService - simplified without stop loss"""
        try:
            from database import Trade, Instrument, User
            
            kite_service = KiteService()
            
            # Get admin user for trading
            with SessionLocal() as db:
                admin_user = db.query(User).filter(User.username == "admin").first()
                if not admin_user:
                    logger.error("❌ Admin user not found")
                    return {
                        "success": False,
                        "error": "Admin user not found",
                        "trade_id": None
                    }
            
            # Load Kite session from database
            session_loaded = kite_service.load_session_from_db(admin_user)
            if not session_loaded:
                logger.error("❌ Failed to load Kite session from database")
                return {
                    "success": False,
                    "error": "Failed to load Kite session",
                    "trade_id": None
                }
            
            logger.info("✅ Kite session loaded successfully")
            
            # Extract trade parameters
            trading_symbol = trade_data["trading_symbol"]
            instrument_token = trade_data["instrument_token"]
            entry_price = trade_data["entry_price"]
            option_type = trade_data["option_type"]
            strike_price = trade_data["strike"]  # Fixed: use 'strike' not 'strike_price'
            quantity = trade_data["quantity"]
            
            # Extract underlying symbol from trading symbol to determine exchange
            # Trading symbols like "SENSEX25SEP2550025000CE" need to be parsed to get "SENSEX"
            underlying_symbol = config.symbol  # Use the symbol from config which is the underlying
            exchange = self._get_exchange_for_symbol(underlying_symbol)
            
            logger.info(f"📊 Trade details: {trading_symbol} on {exchange} exchange")
            
            # Fetch lot size from instruments table
            with SessionLocal() as db:
                instrument = db.query(Instrument).filter(
                    Instrument.instrument_token == instrument_token
                ).first()
                
                if not instrument or not instrument.lot_size:
                    logger.error(f"❌ Could not find lot size for instrument token {instrument_token}")
                    return {
                        "success": False,
                        "error": f"Lot size not found for instrument {trading_symbol}",
                        "trade_id": None
                    }
                
                lot_size = instrument.lot_size
                logger.info(f"📊 Fetched lot size: {lot_size} for {trading_symbol}")
            
            # Calculate quantity in units (lots * lot_size)
            quantity_units = config.quantity_lots * lot_size
            
            if config.is_paper:
                logger.info(f"📝 PAPER TRADE: Would execute SELL order for {trading_symbol}")
                logger.info(f"   Quantity: {quantity_units} units ({config.quantity_lots} lots)")
                logger.info(f"   Entry Price: ₹{entry_price}")
                
                # Create paper trade record
                with SessionLocal() as db:
                    # For paper trades, create a simplified record since the Trade model is basic
                    # Use RSP monitoring strike ID for trade identification
                    rsp_strike_id = trade_data.get("rsp_monitoring_strike_id", "unknown")
                    paper_order_id = f"paper_rsp_{rsp_strike_id}_{int(get_naive_ist_now().timestamp())}"
                    trade_record = Trade(
                        user_id=1,  # Default user for now
                        order_id=paper_order_id,  # Generate unique paper order ID
                        trade_id=f"rsp_{rsp_strike_id}",
                        tradingsymbol=trading_symbol,
                        instrument_token=instrument_token,
                        exchange=exchange,
                        transaction_type="SELL",
                        quantity=quantity_units,
                        price=entry_price,
                        value=quantity_units * entry_price,
                        status="open",
                        trade_time=get_naive_ist_now(),
                        created_at=get_naive_ist_now()
                    )
                    db.add(trade_record)
                    db.commit()
                    db.refresh(trade_record)
                    
                    logger.info(f"✅ Paper trade record created with ID: {trade_record.id}")
                    
                    return {
                        "success": True,
                        "is_paper": True,
                        "trade_id": trade_record.id,
                        "order_id": paper_order_id,
                        "order_id": trade_record.trade_id,
                        "message": "Paper trade executed successfully"
                    }
            
            else:
                # LIVE TRADING - Place actual order
                logger.info(f"🔴 LIVE TRADE: Executing SELL order for {trading_symbol}")
                logger.info(f"   Quantity: {quantity_units} units ({config.quantity_lots} lots)")
                logger.info(f"   Entry Price: ₹{entry_price}")
                
                # Map exchange string to Kite exchange constant
                kite_exchange = kite_service.kite.EXCHANGE_BFO if exchange == "BFO" else kite_service.kite.EXCHANGE_NFO
                
                # Place SELL order using KiteService
                order_result = kite_service.place_order(
                    variety=kite_service.kite.VARIETY_REGULAR,
                    exchange=kite_exchange,
                    tradingsymbol=trading_symbol,
                    transaction_type=kite_service.kite.TRANSACTION_TYPE_SELL,
                    quantity=quantity_units,
                    product=kite_service.kite.PRODUCT_MIS,  # Intraday
                    order_type=kite_service.kite.ORDER_TYPE_MARKET,
                    validity=kite_service.kite.VALIDITY_DAY
                )
                
                # Handle both string (order_id) and dict responses
                order_id = self._extract_order_id(order_result)
                
                if order_id:
                    logger.info(f"✅ SELL order placed successfully. Order ID: {order_id}")
                    
                    # Wait briefly for order execution
                    await asyncio.sleep(2)
                    
                    # Get order details to confirm execution
                    try:
                        order_history = kite_service.kite.order_history(order_id)
                        last_order_status = order_history[-1] if order_history else {}
                        
                        order_status = last_order_status.get("status", "UNKNOWN")
                        filled_quantity = last_order_status.get("filled_quantity", 0)
                        average_price = last_order_status.get("average_price", entry_price)
                        
                        logger.info(f"📊 Order Status: {order_status}, Filled: {filled_quantity}, Avg Price: ₹{average_price}")
                        
                    except Exception as e:
                        logger.warning(f"⚠️ Could not fetch order history: {e}")
                        order_status = "COMPLETE"  # Assume success
                        filled_quantity = quantity_units
                        average_price = entry_price
                    
                    # Create live trade record
                    with SessionLocal() as db:
                        # Use RSP monitoring strike ID for trade identification
                        rsp_strike_id = trade_data.get("rsp_monitoring_strike_id", "unknown")
                        trade_record = Trade(
                            user_id=1,  # Default user for now
                            order_id=order_id,  # Use actual Kite order ID for live trades
                            trade_id=f"rsp_{rsp_strike_id}",
                            tradingsymbol=trading_symbol,
                            instrument_token=instrument_token,
                            exchange=exchange,
                            transaction_type="SELL",
                            quantity=filled_quantity,
                            price=average_price,
                            value=filled_quantity * average_price,
                            status="open",
                            trade_time=get_naive_ist_now(),
                            created_at=get_naive_ist_now()
                        )
                        db.add(trade_record)
                        db.commit()
                        db.refresh(trade_record)
                        
                        logger.info(f"✅ Live trade record created with ID: {trade_record.id}")
                        
                        return {
                            "success": True,
                            "is_paper": False,
                            "trade_id": trade_record.id,
                            "order_id": order_id,
                            "order_status": order_status,
                            "filled_quantity": filled_quantity,
                            "average_price": average_price,
                            "message": "Live trade executed successfully"
                        }
                
                else:
                    logger.error(f"❌ Failed to place SELL order for {trading_symbol}")
                    return {
                        "success": False,
                        "error": "Failed to place order",
                        "trade_id": None
                    }
                    
        except Exception as e:
            logger.error(f"❌ Error executing trade: {e}")
            return {
                "success": False,
                "error": str(e),
                "trade_id": None
            }
    
    def _extract_order_id(self, order_result) -> Optional[str]:
        """DRY: Extract order ID from various Kite API response formats"""
        if not order_result:
            return None
            
        if isinstance(order_result, dict) and "order_id" in order_result:
            return order_result["order_id"]
        elif isinstance(order_result, str) and order_result.isdigit():
            return order_result
        elif isinstance(order_result, (int, str)):
            return str(order_result)
        return None

# Initialize strategy service
rsp_strategy = None

async def get_rsp_strategy():
    """Dependency to get RSP strategy instance with pre-initialized Redis"""
    global rsp_strategy
    if not rsp_strategy:
        # Initialize services (these would come from your dependency injection)
        kite_service = KiteService()  # You'd get this from your DI container
        notification_service = NotificationService()
        
        rsp_strategy = RSPStrategy(kite_service, notification_service)
        
        # Pre-initialize Redis connection for better performance
        try:
            await rsp_strategy.initialize_redis_startup()
        except Exception as e:
            logger.warning(f"⚠️ Redis pre-initialization failed: {e}")
            # Continue without Redis pre-initialization - will be initialized on first use
            pass
    
    return rsp_strategy

# API Endpoints

@router.post("/start-monitoring/{symbol}")
async def start_monitoring(
    symbol: str = Path(..., description="Symbol to start RSP monitoring"),
    monitoring_threshold: float = Query(2.0, description="Threshold percentage for price movement monitoring"),
    monitoring_interval: float = Query(0.4, description="Monitoring interval in seconds"),
    isPaper: bool = Query(True, description="True for paper trading (notifications only), False for live trading"),
    quantity_lots: int = Query(1, description="Number of lots to trade (1 lot = lot_size units)"),
    manual_meetingpoint_strikes: str = Query("", description="Comma-separated manual meeting point strikes (e.g., '24450,24500,24550')"),
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Start Range of Strike Prices monitoring for a symbol with atomic locking"""
    try:
        symbol = symbol.upper()
        
        # Parse comma-separated strikes into list of integers
        parsed_strikes = None
        if manual_meetingpoint_strikes.strip():
            try:
                parsed_strikes = [int(strike.strip()) for strike in manual_meetingpoint_strikes.split(',') if strike.strip()]
                if not parsed_strikes:  # Empty list after parsing
                    parsed_strikes = None
            except ValueError as e:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Invalid manual_meetingpoint_strikes format. Use comma-separated integers (e.g., '24450,24500,24550'). Error: {str(e)}"
                )
        # print(f"parsed Strike {parsed_strikes}\n\n")
        
        # Create temporary config for strike calculation preview
        temp_config = RSPConfig(
            symbol=symbol,
            monitoring_threshold=monitoring_threshold,
            monitoring_interval=monitoring_interval,
            is_paper=isPaper,
            quantity_lots=quantity_lots,
            manual_meetingpoint_strikes=parsed_strikes
        )
        
        # Preview strike calculation before starting monitoring
        try:
            logger.info(f"🔍 Previewing strike calculation for {symbol}")
            
            # Store temp config temporarily for strike calculation
            strategy.symbol_configs[symbol] = temp_config
            
            # Check if we're before market open
            if is_before_market_open():
                # Before market hours - use manual strikes or provide default message
                if parsed_strikes:
                    preview_strikes = sorted([float(strike) for strike in parsed_strikes])
                    logger.info(f"🕘 Before market open - using manual strikes: {preview_strikes}")
                else:
                    logger.info(f"🕘 Before market open - strike calculation will be done after 9:15 AM")
                    preview_strikes = []  # Empty strikes - will be calculated after market open
            else:
                # During/after market hours - calculate normally
                preview_strikes = await strategy._get_rsp_strikes_from_crossover_events(symbol)
            
            logger.info(f"📊 Strike calculation preview for {symbol}:")
            logger.info(f"   Manual strikes input: {parsed_strikes}")
            logger.info(f"   Selected strikes: {preview_strikes}")
            
            if not preview_strikes and not is_before_market_open():
                # Only fail if we're not before market open
                # Clean up temp config
                if symbol in strategy.symbol_configs:
                    del strategy.symbol_configs[symbol]
                raise HTTPException(
                    status_code=400,
                    detail=f"Strike calculation failed for {symbol}. No strikes could be determined from current price and yesterday's data."
                )
            
            # If before market open and no preview strikes, that's okay
            if not preview_strikes and is_before_market_open():
                logger.info(f"🕘 Strike calculation deferred until market open for {symbol}")
            
            # Clean up temp config (will be recreated in start_monitoring)
            if symbol in strategy.symbol_configs:
                del strategy.symbol_configs[symbol]
                
        except HTTPException:
            raise  # Re-raise HTTP exceptions
        except Exception as e:
            # Clean up temp config
            if symbol in strategy.symbol_configs:
                del strategy.symbol_configs[symbol]
            logger.error(f"❌ Strike calculation preview failed for {symbol}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Strike calculation preview failed: {str(e)}"
            )
        
        # Create final config after successful preview with calculated strikes
        config = RSPConfig(
            symbol=symbol,
            monitoring_threshold=monitoring_threshold,
            monitoring_interval=monitoring_interval,
            is_paper=isPaper,
            quantity_lots=quantity_lots,
            manual_meetingpoint_strikes=parsed_strikes,
            calculated_strikes=preview_strikes  # Store one-time calculated strikes
        )
        
        result = await strategy.start_monitoring(symbol, config)
        
        if result["success"]:
            # Determine message based on timing
            if is_before_market_open():
                message = f"RSP monitoring queued for {symbol} - will start at 9:15 AM"
                timing_info = "Monitoring will begin automatically when market opens at 9:15 AM"
            else:
                message = f"Started RSP monitoring for {symbol}"
                timing_info = "Monitoring is now active"
            
            return {
                "success": True,
                "message": message,
                "timing_info": timing_info,
                "config": {
                    "symbol": symbol,
                    "monitoring_threshold": monitoring_threshold,
                    "monitoring_interval": monitoring_interval,
                    "isPaper": isPaper,
                    "quantity_lots": quantity_lots,
                    "manual_meetingpoint_strikes": parsed_strikes,
                    "selected_strikes": preview_strikes if preview_strikes else "Will be calculated at market open",
                    "trading_mode": "Paper Trading" if isPaper else "Live Trading",
                    "quantity_description": f"{quantity_lots} lot(s) will be traded per signal",
                    "strategy_description": "Range of Strike Prices monitoring - analyzes multiple strikes around ATM for opportunities"
                },
                "lock_info": result.get("lock_info")
            }
        else:
            # Handle specific error codes
            error_code = result.get("code", 400)
            if error_code == 409:
                # Conflict - monitoring already active
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "Monitoring Already Active",
                        "message": result["message"],
                        "suggestion": "Stop existing monitoring before starting new one",
                        "lock_info": result.get("lock_info"),
                        "action_required": f"POST /api/rsp/stop-monitoring/{symbol}"
                    }
                )
            else:
                raise HTTPException(status_code=error_code, detail=result["message"])
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error starting RSP monitoring for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop-monitoring/{symbol}")
async def stop_monitoring(
    symbol: str = Path(..., description="Symbol to stop RSP monitoring and force clear lock"),
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Stop Range of Strike Prices monitoring for a symbol and force clear lock (works for both active and stale locks)"""
    try:
        symbol = symbol.upper()
        result = await strategy.stop_monitoring(symbol)
        
        if result["success"]:
            return {
                "success": True,
                "message": result["message"],
                "symbol": symbol,
                "details": {
                    "had_active_monitor": result.get("had_active_monitor", False),
                    "lock_released": result.get("lock_released", False),
                    "force_cleanup": result.get("force_cleanup", True),
                    "lock_status_after_cleanup": result.get("lock_status_after_cleanup", {}),
                    "description": "This endpoint always attempts to clear locks, whether monitoring is active or not"
                }
            }
        else:
            raise HTTPException(status_code=400, detail=result["message"])
        
    except Exception as e:
        logger.error(f"Error stopping RSP monitoring for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/monitoring-status")
async def get_monitoring_status(
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Get current RSP monitoring status with lock information"""
    try:
        status = await strategy.get_detailed_monitoring_status()
        return {
            "success": True,
            "data": status
        }
        
    except Exception as e:
        logger.error(f"Error getting RSP monitoring status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/lock-status/{symbol}")
async def get_lock_status(
    symbol: str = Path(..., description="Symbol to check lock status"),
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Check Redis lock status for a specific symbol"""
    try:
        symbol = symbol.upper()
        lock_status = await strategy._check_lock_status(symbol)
        
        return {
            "success": True,
            "symbol": symbol,
            "lock_status": lock_status
        }
        
    except Exception as e:
        logger.error(f"Error checking lock status for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/cleanup-stale-locks")
async def cleanup_stale_locks(
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Cleanup stale locks for inactive monitors (admin endpoint for interruption recovery)"""
    try:
        logger.info("🧹 API: Starting comprehensive stale lock cleanup...")
        
        # Run the comprehensive cleanup
        cleanup_results = await strategy._cleanup_stale_locks()
        
        # Send notification about cleanup results
        try:
            title = "🧹 RSP Lock Cleanup Results"
            message = f"Stale lock cleanup completed via API\n\n"
            message += f"📊 Total locks found: {cleanup_results['total_locks_found']}\n"
            message += f"🧹 Locks cleaned: {cleanup_results['locks_cleaned']}\n"
            message += f"❌ Errors: {len(cleanup_results['errors'])}\n"
            message += f"⏰ Time: {get_naive_ist_now().strftime('%H:%M:%S')}\n\n"
            
            if cleanup_results['locks_cleaned'] > 0:
                message += f"✅ Cleaned locks:\n"
                for detail in cleanup_results['cleanup_details']:
                    if "cleaned" in detail['action']:
                        message += f"• {detail['symbol']}: {detail['action']}\n"
            
            if cleanup_results['errors']:
                message += f"\n⚠️ Errors encountered:\n"
                for error in cleanup_results['errors'][:3]:  # Limit to first 3 errors
                    message += f"• {error}\n"
            
            await send_pushover_notification(
                title=title,
                message=message,
                priority=0  # Normal priority
            )
            
        except Exception as notify_error:
            logger.error(f"❌ Failed to send cleanup notification: {notify_error}")
        
        return {
            "success": True,
            "message": "Comprehensive stale lock cleanup completed",
            "cleanup_results": cleanup_results,
            "api_endpoint": "/api/rsp/cleanup-stale-locks",
            "timestamp": get_naive_ist_now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"❌ Error during API stale lock cleanup: {e}")
        
        # Send error notification
        try:
            await send_pushover_notification(
                title="🚨 RSP Lock Cleanup Error",
                message=f"API lock cleanup failed\n\nError: {str(e)}\nTime: {get_naive_ist_now().strftime('%H:%M:%S')}",
                priority=1  # High priority for errors
            )
        except:
            pass  # Don't fail the API response if notification fails
        
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")

@router.get("/monitoring-history/{symbol}")
async def get_monitoring_history(
    symbol: str = Path(..., description="Symbol to get monitoring history"),
    days: int = Query(7, description="Number of days of history to retrieve"),
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Get monitoring history for a symbol from database"""
    try:
        symbol = symbol.upper()
        
        db = SessionLocal()
        try:
            # Get monitoring sessions for the symbol
            from_date = datetime.now().date() - timedelta(days=days)
            
            sessions = db.query(RSPMonitoring).filter(
                RSPMonitoring.symbol == symbol,
                RSPMonitoring.session_date >= from_date
            ).order_by(RSPMonitoring.created_at.desc()).all()
            
            monitoring_history = []
            for session in sessions:
                # Get strikes for this session
                strikes = db.query(RSPMonitoringStrike).filter(
                    RSPMonitoringStrike.monitoring_id == session.monitoring_id
                ).order_by(RSPMonitoringStrike.strike.asc()).all()
                
                strike_data = []
                for strike in strikes:
                    strike_data.append({
                        "strike": float(strike.strike),
                        "distance_from_atm": strike.distance_from_atm,
                        "is_atm": strike.is_atm,
                        "ce_symbol": strike.ce_symbol,
                        "pe_symbol": strike.pe_symbol,
                        "ce_price": float(strike.ce_price) if strike.ce_price else None,
                        "pe_price": float(strike.pe_price) if strike.pe_price else None,
                        "ce_stoploss": float(strike.ce_stoploss) if strike.ce_stoploss else None,
                        "pe_stoploss": float(strike.pe_stoploss) if strike.pe_stoploss else None,
                        "monitoring_status": strike.monitoring_status,
                        "last_updated": strike.updated_at.isoformat() if strike.updated_at else None
                    })
                
                session_data = {
                    "monitoring_id": str(session.monitoring_id),
                    "created_at": session.created_at.isoformat(),
                    "session_date": session.session_date.isoformat(),
                    "monitoring_threshold": float(session.monitoring_threshold),
                    "monitoring_interval": float(session.monitoring_interval),
                    "isPaper": session.is_paper,
                    "quantity_lots": session.quantity_lots,
                    "monitoring_status": session.monitoring_status,
                    "manual_meetingpoint_strikes": session.manual_meetingpoint_strikes,
                    "calculated_strikes": session.calculated_strikes,
                    "current_atm_strike": float(session.current_atm_strike) if session.current_atm_strike else None,
                    "last_updated": session.last_updated.isoformat() if session.last_updated else None,
                    "strikes": strike_data,
                    "strike_count": len(strike_data)
                }
                
                monitoring_history.append(session_data)
            
            return {
                "success": True,
                "symbol": symbol,
                "days_requested": days,
                "from_date": from_date.isoformat(),
                "sessions_found": len(monitoring_history),
                "monitoring_history": monitoring_history
            }
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error getting monitoring history for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/current-monitoring-data/{symbol}")
async def get_current_monitoring_data(
    symbol: str = Path(..., description="Symbol to get current monitoring data"),
    strategy: RSPStrategy = Depends(get_rsp_strategy)
):
    """Get current active monitoring session data from database"""
    try:
        symbol = symbol.upper()
        
        db = SessionLocal()
        try:
            # Get active monitoring session
            session = db.query(RSPMonitoring).filter(
                RSPMonitoring.symbol == symbol,
                RSPMonitoring.monitoring_status == 'ACTIVE'
            ).first()
            
            if not session:
                return {
                    "success": False,
                    "message": f"No active monitoring session found for {symbol}",
                    "symbol": symbol
                }
            
            # Get strikes for this session
            strikes = db.query(RSPMonitoringStrike).filter(
                RSPMonitoringStrike.monitoring_id == session.monitoring_id
            ).order_by(RSPMonitoringStrike.strike.asc()).all()
            
            strike_data = []
            for strike in strikes:
                strike_data.append({
                    "strike": float(strike.strike),
                    "distance_from_atm": strike.distance_from_atm,
                    "is_atm": strike.is_atm,
                    "ce_symbol": strike.ce_symbol,
                    "pe_symbol": strike.pe_symbol,
                    "ce_token": strike.ce_token,
                    "pe_token": strike.pe_token,
                    "ce_price": float(strike.ce_price) if strike.ce_price else None,
                    "pe_price": float(strike.pe_price) if strike.pe_price else None,
                    "ce_stoploss": float(strike.ce_stoploss) if strike.ce_stoploss else None,
                    "pe_stoploss": float(strike.pe_stoploss) if strike.pe_stoploss else None,
                    "ce_last_updated": strike.ce_last_updated.isoformat() if strike.ce_last_updated else None,
                    "pe_last_updated": strike.pe_last_updated.isoformat() if strike.pe_last_updated else None,
                    "monitoring_status": strike.monitoring_status,
                    "baseline_price_915": float(strike.baseline_price_915) if strike.baseline_price_915 else None,
                    "last_price": float(strike.last_price) if strike.last_price else None,
                    "last_price_updated": strike.last_price_updated.isoformat() if strike.last_price_updated else None,
                    "created_at": strike.created_at.isoformat(),
                    "updated_at": strike.updated_at.isoformat() if strike.updated_at else None
                })
            
            return {
                "success": True,
                "symbol": symbol,
                "monitoring_session": {
                    "monitoring_id": str(session.monitoring_id),
                    "created_at": session.created_at.isoformat(),
                    "session_date": session.session_date.isoformat(),
                    "monitoring_threshold": float(session.monitoring_threshold),
                    "monitoring_interval": float(session.monitoring_interval),
                    "isPaper": session.is_paper,
                    "quantity_lots": session.quantity_lots,
                    "monitoring_status": session.monitoring_status,
                    "manual_meetingpoint_strikes": session.manual_meetingpoint_strikes,
                    "calculated_strikes": session.calculated_strikes,
                    "current_atm_strike": float(session.current_atm_strike) if session.current_atm_strike else None,
                    "last_updated": session.last_updated.isoformat() if session.last_updated else None,
                    "strikes": strike_data,
                    "strike_count": len(strike_data)
                }
            }
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error getting current monitoring data for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def health_check():
    """Health check for RSP strategy"""
    return {
        "success": True,
        "message": "RSP Strategy API is running",
        "timestamp": get_naive_ist_now().isoformat(),
        "strategy": "Range of Strike Prices (RSP)"
    }
