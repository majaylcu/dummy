"""
RSP Trade Monitoring System

Continuously monitors the trades table for new rsp_* trade insertions.
For each trade:
1. Extracts rsp_id from trade_id 
2. Fetches ce_stoploss or pe_stoploss depending on the situation from rsp_monitoring_strike table
3. Monitors trade price against stop loss
4. Closes trade and sends notification when SL is hit

Requirements Implementation:
- Monitor trades table for rsp_* trade_id patterns
- Extract rsp_id from trade_id (e.g. rsp_134 → 134)
- Fetch SL from rsp_monitoring_strike using rsp_id
- Continuously monitor trade price vs SL
- Close trade and send Pushover notification when SL hit
"""

import asyncio
import logging
import re
import httpx
import json
import redis.asyncio as redis
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Set, Any, Tuple
from decimal import Decimal
import traceback

from database import SessionLocal, PostCrossoverTradeSignal, get_naive_ist_now
from trading_models import Trade, TradingOrder
from pushover_service import send_pushover_notification
from datetime_utils import get_naive_ist_now

# Trading session configuration
TRADING_SESSIONS = [
    {"start": "09:15", "end": "15:31"}   # Full day monitoring from 09:15 onwards (no time restriction)
]

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RSPTradeMonitor:
    """
    Monitors RSP trades for stop loss execution
    """
    
    def __init__(self):
        self.monitored_trades: Set[int] = set()  # Track which trade IDs are being monitored
        self.trade_crossover_events: Dict[int, int] = {}  # trade_id -> crossover_event_id (for fresh SL lookup)
        self.monitoring_active = False
        
        # Trading session management
        self.last_session_check: Optional[datetime] = None
        self.session_close_processed: Set[str] = set()  # Track processed session closes to avoid duplicates
        
        # HTTP client for price fetching (keeping for fallback)
        self.http_client = httpx.AsyncClient(timeout=10.0)
        self.base_url = "http://localhost:8000"  # Adjust if your API runs on different port
        
        # Redis connection for tick data
        self.redis_client = None
        self.redis_initialized = False
        
        # Kite service for order management
        self.kite_service = None
        
    async def start_monitoring(self):
        """Start the main monitoring loop"""
        logger.info("🚀 Starting RSP Trade Monitoring System")
        logger.info("=" * 60)
        
        self.monitoring_active = True
        
        # Initialize Redis connection
        await self._ensure_redis_connection()
        
        # Initialize Kite service
        await self._initialize_kite_service()
        
        try:
            while self.monitoring_active:
                await self._monitoring_cycle()
                await asyncio.sleep(0.4)  # Check every 0.4 seconds

        except KeyboardInterrupt:
            logger.info("📴 Monitoring stopped by user")
        except Exception as e:
            logger.error(f"❌ Critical error in monitoring: {e}")
            logger.error(traceback.format_exc())
        finally:
            self.monitoring_active = False
            logger.info("🛑 RSP Trade Monitoring System stopped")
    
    async def stop_monitoring(self):
        """Stop the monitoring system"""
        self.monitoring_active = False
        
        # Close HTTP client properly
        try:
            await self.http_client.aclose()
            logger.info("✅ HTTP client closed successfully")
        except Exception as e:
            logger.warning(f"⚠️ Error closing HTTP client: {e}")
        
        # Close Redis client if connected
        try:
            if self.redis_client:
                await self.redis_client.aclose()
                logger.info("✅ Redis client closed successfully")
        except Exception as e:
            logger.warning(f"⚠️ Error closing Redis client: {e}")
    
    async def _ensure_redis_connection(self):
        """Ensure Redis connection is established"""
        if self.redis_initialized and self.redis_client:
            return
        
        try:
            # Create Redis connection
            self.redis_client = redis.Redis(
                host='localhost',
                port=6379,
                db=0,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            
            # Test connection
            await self.redis_client.ping()
            self.redis_initialized = True
            logger.info("✅ Redis connection established successfully")
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to connect to Redis: {e}")
            self.redis_initialized = False
            self.redis_client = None
        
    async def _monitoring_cycle(self):
        """Single monitoring cycle - check for new trades and monitor existing ones"""
        try:
            # Step 1: Check for session end times and force close trades
            await self._check_trading_session_end()
            
            # Step 2: Check for new postcrossover trades
            await self._check_for_new_postcrossover_trades()
            
            # Step 3: Monitor existing trades for stop loss
            await self._monitor_existing_trades()
            
        except Exception as e:
            logger.error(f"❌ Error in monitoring cycle: {e}")
    
    async def _check_trading_session_end(self):
        """
        Check if we're at the end of a trading session and force-close all postcrossover trades
        """
        try:
            current_time = datetime.now().time()
            current_date = datetime.now().date()
            
            # Check each trading session for end time
            for session in TRADING_SESSIONS:
                session_end_time = time.fromisoformat(session["end"])
                
                # Create a unique key for this session close on this date
                session_close_key = f"{current_date}_{session['end']}"
                
                # Check if we're at or past the session end time
                if current_time >= session_end_time:
                    # Check if we haven't already processed this session close today
                    if session_close_key not in self.session_close_processed:
                        logger.warning(f"⏰ Trading session ending at {session['end']} - force closing all postcrossover trades")
                        logger.info(f"🕐 Current time: {current_time.strftime('%H:%M:%S')}, Session end: {session_end_time.strftime('%H:%M:%S')}")

                        # Force close all postcrossover trades (no time window restriction)
                        await self._force_close_all_postcrossover_trades(session['end'])

                        # Mark this session close as processed
                        self.session_close_processed.add(session_close_key)

                        logger.info(f"✅ Session end processing completed for {session['end']}")
            
            # Clean up old session close records (keep only today's)
            self.session_close_processed = {
                key for key in self.session_close_processed 
                if key.startswith(str(current_date))
            }
            
        except Exception as e:
            logger.error(f"❌ Error checking trading session end: {e}")
    
    async def _force_close_all_postcrossover_trades(self, session_end_time: str):
        """
        Force close all open RSP trades at session end
        """
        try:
            db = SessionLocal()
            
            # Get all open RSP trades
            open_trades = db.query(Trade).filter(
                Trade.trade_id.like("rsp_%"),
                Trade.status == "open"
            ).all()
            
            if not open_trades:
                logger.info(f"📝 No open RSP trades to close at session end {session_end_time}")
                return
            
            logger.warning(f"🚨 FORCE CLOSING {len(open_trades)} RSP trades at session end {session_end_time}")
            
            closed_trades = []
            failed_trades = []
            
            for trade in open_trades:
                try:
                    # Get current price for P&L calculation
                    current_price = await self._get_current_price(trade.instrument_token)
                    
                    
                    
                    if current_price is None:
                        current_price = float(trade.price)  # Use entry price as fallback
                    
                    # Try to place close order
                    close_success = await self._place_session_end_close_order(trade, current_price)
                    
                    # Update trade status regardless of order success
                    trade.status = "closed"
                    trade.close_reason = f"session_end_{session_end_time}"
                    trade.closed_at = get_naive_ist_now()
                    
                    if close_success:
                        closed_trades.append((trade, current_price, "order_placed"))
                    else:
                        closed_trades.append((trade, current_price, "manual_close_required"))
                        failed_trades.append(trade)
                    
                    # Remove from monitoring
                    self.monitored_trades.discard(trade.id)
                    self.trade_crossover_events.pop(trade.id, None)
                    
                except Exception as trade_error:
                    logger.error(f"❌ Error closing trade {trade.id}: {trade_error}")
                    failed_trades.append(trade)
                    
                    # Still update status and remove from monitoring
                    trade.status = "closed"
                    trade.close_reason = f"session_end_{session_end_time}_error"
                    trade.closed_at = get_naive_ist_now()
                    
                    self.monitored_trades.discard(trade.id)
                    self.trade_crossover_events.pop(trade.id, None)
            
            # Commit all database changes
            db.commit()
            
            # Send summary notification
            await self._send_session_end_notification(session_end_time, closed_trades, failed_trades)
            
            logger.info(f"✅ Session end force close completed: {len(closed_trades)} trades processed")
            
        except Exception as e:
            logger.error(f"❌ Error in force close all postcrossover trades: {e}")
        finally:
            if 'db' in locals():
                db.close()
    
    async def _place_session_end_close_order(self, trade: Trade, current_price: float) -> bool:
        """
        Try to place session end close order for RSP trade, return success status
        RSP trades are SELL trades, so we place BUY orders to close them
        """
        try:
            if not self.kite_service:
                return False
            
            # RSP trades are SELL trades, so we always place BUY orders to close
            close_transaction_type = "BUY"
            
            order_params = {
                "variety": "regular",
                "exchange": trade.exchange,
                "tradingsymbol": trade.tradingsymbol,
                "transaction_type": close_transaction_type,
                "quantity": trade.quantity,
                "product": "MIS",
                "order_type": "MARKET",
                "validity": "DAY",
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "tag": f"RSP_SessionEnd_{trade.id}"
            }
            
            order_result = self.kite_service.place_order(**order_params)
            logger.info(f"✅ Session end close order placed for trade {trade.id}: {order_result}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to place session end close order for trade {trade.id}: {e}")
            return False
    
    async def _send_session_end_notification(self, session_end_time: str, closed_trades: List[Tuple], failed_trades: List[Trade]):
        """
        Send Pushover notification summarizing session end forced closures
        """
        try:
            title = f"🔚 Session End Force Close - {session_end_time}"
            
            message = f"⏰ Trading Session Ended - RSP Trades Closed\n\n"
            message += f"📅 Session End Time: {session_end_time}\n"
            message += f"📊 Total Trades Closed: {len(closed_trades)}\n"
            
            if failed_trades:
                message += f"⚠️ Manual Close Required: {len(failed_trades)}\n\n"
            else:
                message += f"✅ All Orders Placed Successfully\n\n"
            
            # Calculate total P&L
            total_pnl = 0
            successful_orders = 0
            manual_closes = 0
            
            message += f"📋 Trade Summary:\n"
            for trade, current_price, status in closed_trades:
                # Calculate P&L for RSP trades (which are SELL trades)
                pnl = (float(trade.price) - current_price) * trade.quantity
                
                total_pnl += pnl
                
                if status == "order_placed":
                    successful_orders += 1
                    status_emoji = "✅"
                else:
                    manual_closes += 1
                    status_emoji = "⚠️"
                
                message += f"   {status_emoji} {trade.tradingsymbol}: ₹{pnl:+.2f}\n"
            
            message += f"\n💰 Total P&L: ₹{total_pnl:+.2f}\n"
            message += f"✅ Automatic Orders: {successful_orders}\n"
            
            if manual_closes > 0:
                message += f"⚠️ Manual Close Required: {manual_closes}\n"
                message += f"\n🔧 Action Required:\n"
                message += f"   • Check Kite/Zerodha for failed orders\n"
                message += f"   • Manually close positions if needed\n"
                message += f"   • Verify all positions are closed\n"
            
            message += f"\n⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            
            # Send notification with high priority
            result = await send_pushover_notification(title, message, priority=1)
            
            if result:
                logger.info(f"✅ Session end notification sent for {session_end_time}")
            else:
                logger.error(f"❌ Failed to send session end notification for {session_end_time}")
                
        except Exception as e:
            logger.error(f"❌ Error sending session end notification: {e}")
    
    async def _check_for_new_postcrossover_trades(self):
        """
        Check trades table for new rsp_* trade insertions
        """
        try:
            db = SessionLocal()
            
            # Query for new rsp trades that aren't being monitored yet
            new_trades = db.query(Trade).filter(
                Trade.trade_id.like("rsp_%"),
                Trade.status == "open",
                ~Trade.id.in_(self.monitored_trades) if self.monitored_trades else True
            ).all()
            
            if new_trades:
                logger.info(f"🔍 Found {len(new_trades)} new rsp trades to monitor")
                
                for trade in new_trades:
                    await self._process_new_trade(trade)
            
        except Exception as e:
            logger.error(f"❌ Error checking for new trades: {e}")
        finally:
            if 'db' in locals():
                db.close()
    
    async def _process_new_trade(self, trade: Trade):
        """
        Process a new RSP trade:
        1. Extract rsp_id from trade_id
        2. Fetch stop loss from rsp_monitoring_strike table
        3. Add to monitoring
        """
        try:
            # Extract rsp_id from trade_id
            # Pattern: rsp_134 → extract 134
            rsp_id = self._extract_rsp_id(trade.trade_id)
            
            if not rsp_id:
                logger.warning(f"⚠️ Could not extract rsp_id from trade_id: {trade.trade_id}")
                return
            
            # Fetch stop loss from rsp_monitoring_strike table
            stop_loss_price = await self._fetch_rsp_stop_loss(rsp_id, trade)
            
            if stop_loss_price is None:
                logger.warning(f"⚠️ Could not find stop loss for rsp_id: {rsp_id}")
                return
            
            # Add to monitoring (store rsp_id for fresh SL lookups)
            self.monitored_trades.add(trade.id)
            self.trade_crossover_events[trade.id] = rsp_id  # Reusing this dict to store rsp_id
            
            logger.info(f"✅ Added RSP trade to monitoring:")
            logger.info(f"   Trade ID: {trade.id}")
            logger.info(f"   Trade Symbol: {trade.tradingsymbol}")
            logger.info(f"   Entry Price: ₹{trade.price}")
            logger.info(f"   Stop Loss: ₹{stop_loss_price} (will fetch fresh on each check)")
            logger.info(f"   RSP ID: {rsp_id}")
            logger.info(f"   Quantity: {trade.quantity}")
            logger.info(f"   Transaction: {trade.transaction_type}")
            
        except Exception as e:
            logger.error(f"❌ Error processing new trade {trade.id}: {e}")
    
    def _extract_rsp_id(self, trade_id: str) -> Optional[int]:
        """
        Extract rsp_id from trade_id
        Pattern: rsp_134 → returns 134
        """
        try:
            # Use regex to extract the number after rsp_
            pattern = r"rsp_(\d+)"
            match = re.match(pattern, trade_id)
            
            if match:
                return int(match.group(1))
            else:
                logger.warning(f"⚠️ Trade ID doesn't match expected pattern: {trade_id}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error extracting rsp_id from {trade_id}: {e}")
            return None
    
    async def _fetch_rsp_stop_loss(self, rsp_id: int, trade: Trade) -> Optional[float]:
        """
        Fetch stop loss price from rsp_monitoring_strike table
        Choose ce_stoploss or pe_stoploss based on the trade's option type
        """
        try:
            from models.rsp_monitoring import RSPMonitoringStrike # Import the table model
            
            db = SessionLocal()
            
            # Find the monitoring strike record for this rsp_id
            monitoring_strike = db.query(RSPMonitoringStrike).filter(
                RSPMonitoringStrike.id == rsp_id
            ).first()
            
            if not monitoring_strike:
                logger.warning(f"⚠️ No monitoring strike found for rsp_id: {rsp_id}")
                return None
            
            # Determine if this is a CE or PE trade based on trading symbol
            trading_symbol = trade.tradingsymbol.upper()
            
            if 'CE' in trading_symbol:
                # This is a CE trade, use ce_stoploss
                stop_loss = monitoring_strike.ce_stoploss
                logger.info(f"📊 Using CE stop loss for {trade.tradingsymbol}: ₹{stop_loss}")
                
            elif 'PE' in trading_symbol:
                # This is a PE trade, use pe_stoploss  
                stop_loss = monitoring_strike.pe_stoploss
                logger.info(f"📊 Using PE stop loss for {trade.tradingsymbol}: ₹{stop_loss}")
                
            else:
                logger.warning(f"⚠️ Cannot determine option type (CE/PE) from symbol: {trading_symbol}")
                return None
            
            if stop_loss and stop_loss > 0:
                return float(stop_loss)
            else:
                logger.warning(f"⚠️ Invalid stop loss value for rsp_id {rsp_id}: {stop_loss}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error fetching RSP stop loss for rsp_id {rsp_id}: {e}")
            return None
        finally:
            if 'db' in locals():
                db.close()
    
    async def _monitor_existing_trades(self):
        """
        Monitor existing trades for stop loss hits
        """
        if not self.monitored_trades:
            return
            
        try:
            db = SessionLocal()
            
            # Get all monitored trades that are still open
            trades_to_monitor = db.query(Trade).filter(
                Trade.id.in_(self.monitored_trades),
                Trade.status == "open"
            ).all()
            
            for trade in trades_to_monitor:
                await self._check_trade_against_stop_loss(trade)
            
        except Exception as e:
            logger.error(f"❌ Error monitoring existing trades: {e}")
        finally:
            if 'db' in locals():
                db.close()
    
    async def _check_trade_against_stop_loss(self, trade: Trade):
        """
        Check individual RSP trade against its stop loss (fetch fresh SL from database)
        """
        try:
            # Get rsp_id for this trade
            rsp_id = self.trade_crossover_events.get(trade.id)
            if not rsp_id:
                logger.warning(f"⚠️ No rsp_id found for trade {trade.id}")
                return
            
            # Fetch FRESH stop loss price from database (in case it was updated)
            stop_loss_price = await self._fetch_rsp_stop_loss(rsp_id, trade)
            if not stop_loss_price:
                logger.warning(f"⚠️ No stop loss found for trade {trade.id} (rsp_id {rsp_id})")
                return
            
            # Get current market price
            current_price = await self._get_current_price(trade.instrument_token)
            
            if current_price is None:
                logger.warning(f"⚠️ Could not fetch current price for {trade.tradingsymbol}")
                # Send pushover notification for price fetch failure
                await self._send_price_fetch_failure_notification(trade, stop_loss_price)
                return
            
            # Check if stop loss is hit
            # RSP trades are SELL trades: SL hit when current_price > stop_loss_price
            # (For SELL trades, we lose money when the price goes UP beyond our stop loss)
            sl_hit = False
                        
            if current_price > stop_loss_price:
                sl_hit = True
            
            if sl_hit:
                logger.warning(f"🚨 RSP STOP LOSS HIT for Trade {trade.id}!")
                logger.warning(f"   Symbol: {trade.tradingsymbol}")
                logger.warning(f"   Current Price: ₹{current_price}")
                logger.warning(f"   Stop Loss: ₹{stop_loss_price} (fresh from RSP monitoring)")
                logger.warning(f"   Entry Price: ₹{trade.price}")
                logger.warning(f"   RSP ID: {rsp_id}")
                
                await self._close_trade_with_stop_loss(trade, current_price, stop_loss_price)
            else:
                # Log periodic status (every 10th check to avoid spam)
                if trade.id % 10 == 0:  # Simple way to reduce logging
                    logger.debug(f"📊 RSP Trade {trade.id} monitoring: Current ₹{current_price}, SL ₹{stop_loss_price} (fresh)")
                
        except Exception as e:
            logger.error(f"❌ Error checking trade {trade.id} against stop loss: {e}")
    
    async def _get_current_price(self, instrument_token: int) -> Optional[float]:
        """
        Get current market price for instrument token using Redis tick data
        """
        try:
            tick_data = await self.get_latest_tick(instrument_token)
            
            if tick_data and tick_data.get("last_price"):
                return float(tick_data["last_price"])
            else:
                logger.warning(f"⚠️ No valid tick data for token {instrument_token}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error fetching price for token {instrument_token}: {e}")
            return None
    
    async def get_latest_tick(self, instrument_token: int) -> Optional[dict]:
        """Get latest tick for a specific instrument from Redis"""
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
    
    async def _close_trade_with_stop_loss(self, trade: Trade, current_price: float, stop_loss_price: float):
        """
        Close RSP trade when stop loss is hit with proper error handling
        RSP trades are SELL trades, so we place BUY orders to close them
        """
        try:
            # RSP trades are SELL trades, so we always place BUY orders to close
            close_transaction_type = "BUY"
            
            # Place market order to close position
            order_params = {
                "variety": "regular",
                "exchange": trade.exchange,
                "tradingsymbol": trade.tradingsymbol,
                "transaction_type": close_transaction_type,
                "quantity": trade.quantity,
                "product": "MIS",
                "order_type": "MARKET",
                "validity": "DAY",
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "tag": f"RSP_SL_Close_{trade.id}"
            }
            
            # Execute close order with error handling
            order_success = False
            order_error = None
            
            if self.kite_service:
                try:
                    order_result = self.kite_service.place_order(**order_params)
                    logger.info(f"✅ Stop loss close order placed: {order_result}")
                    order_success = True
                    
                    # Update trade status in database
                    await self._update_trade_status(trade.id, "closed", "stop_loss")
                    
                    # Send success notification
                    await self._send_stop_loss_notification(trade, current_price, stop_loss_price)
                    
                except Exception as order_error_exc:
                    order_error = str(order_error_exc)
                    logger.error(f"❌ Order placement failed: {order_error}")
                    
                    # Handle specific AMO error or other trading errors
                    if "AMO" in order_error or "After Market Order" in order_error:
                        logger.warning("⚠️ AMO restriction detected - marking for manual close")
                    
                    # Update trade status to require manual intervention
                    await self._update_trade_status(trade.id, "closed", "manual_close_required")
                    
                    # Send manual close notification
                    await self._send_manual_close_notification(trade, current_price, stop_loss_price, order_error)
                    
            else:
                order_error = "Kite service not available"
                logger.error("❌ Cannot place close order - Kite service not available")
                
                # Update trade status to require manual intervention
                await self._update_trade_status(trade.id, "closed", "manual_close_required")
                
                # Send manual close notification
                await self._send_manual_close_notification(trade, current_price, stop_loss_price, order_error)
            
            # ALWAYS remove from monitoring regardless of order success/failure
            # This prevents infinite loops of failed attempts
            self.monitored_trades.discard(trade.id)
            self.trade_crossover_events.pop(trade.id, None)
            
            if order_success:
                logger.info(f"🔒 Trade {trade.id} closed successfully and removed from monitoring")
            else:
                logger.info(f"⚠️ Trade {trade.id} marked for manual close and removed from monitoring")
                logger.info(f"   Reason: {order_error}")
            
        except Exception as e:
            logger.error(f"❌ Critical error closing trade {trade.id}: {e}")
            
            # Even on critical error, remove from monitoring to prevent loops
            self.monitored_trades.discard(trade.id)
            self.trade_crossover_events.pop(trade.id, None)
            
            # Try to send error notification
            try:
                await self._send_manual_close_notification(trade, current_price, stop_loss_price, str(e))
            except Exception as notify_error:
                logger.error(f"❌ Failed to send error notification: {notify_error}")
    
    async def _update_trade_status(self, trade_id: int, status: str, close_reason: str):
        """
        Update trade status in database
        """
        try:
            db = SessionLocal()
            
            trade = db.query(Trade).filter(Trade.id == trade_id).first()
            if trade:
                trade.status = status
                trade.close_reason = close_reason
                trade.closed_at = get_naive_ist_now()
                
                db.commit()
                logger.info(f"✅ Updated trade {trade_id} status to {status}")
            else:
                logger.error(f"❌ Trade {trade_id} not found for status update")
                
        except Exception as e:
            logger.error(f"❌ Error updating trade status: {e}")
        finally:
            if 'db' in locals():
                db.close()
    
    async def _send_stop_loss_notification(self, trade: Trade, current_price: float, stop_loss_price: float):
        """
        Send Pushover notification when stop loss is hit for RSP trade
        """
        try:
            # Calculate P&L for RSP trades (which are SELL trades)
            # For SELL trades: P&L = (entry_price - current_price) * quantity
            # Positive PnL when current price is below entry price (good for SELL trades)
            pnl = (float(trade.price) - current_price) * trade.quantity
            
            pnl_percent = (pnl / float(trade.value)) * 100
            
            title = f"🛑 RSP Stop Loss Hit - {trade.tradingsymbol}"
            
            message = f"🚨 RSP Stop Loss Triggered\n\n"
            message += f"📊 Symbol: {trade.tradingsymbol}\n"
            message += f"🔄 RSP Transaction: SELL {trade.quantity} units\n"
            message += f"💰 Entry Price: ₹{trade.price}\n"
            message += f"� Current Price: ₹{current_price:.2f}\n"
            message += f"🛡️ Stop Loss: ₹{stop_loss_price:.2f}\n"
            message += f"💸 P&L: ₹{pnl:.2f} ({pnl_percent:+.2f}%)\n"
            message += f"💼 Trade Value: ₹{trade.value}\n"
            message += f"🔄 Closing with: BUY order\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n"
            message += f"🆔 Trade ID: {trade.id}"
            
            # Send notification
            result = await send_pushover_notification(title, message)
            
            if result:
                logger.info(f"✅ Stop loss notification sent for trade {trade.id}")
            else:
                logger.error(f"❌ Failed to send stop loss notification for trade {trade.id}")
                
        except Exception as e:
            logger.error(f"❌ Error sending stop loss notification: {e}")
    
    async def _send_price_fetch_failure_notification(self, trade: Trade, stop_loss_price: float):
        """
        Send Pushover notification when price fetching fails
        """
        try:
            title = f"⚠️ Price Fetch Failed - {trade.tradingsymbol}"
            
            message = f"🚨 Trade Monitoring Alert\n\n"
            message += f"📊 Symbol: {trade.tradingsymbol}\n"
            message += f"🔄 Transaction: {trade.transaction_type} {trade.quantity} units\n"
            message += f"💰 Entry Price: ₹{trade.price}\n"
            message += f"🛡️ Stop Loss: ₹{stop_loss_price} (fresh from DB)\n"
            message += f"💼 Trade Value: ₹{trade.value}\n"
            message += f"⚠️ Issue: Unable to fetch current market price\n"
            message += f"🔧 Action: Check API connectivity and streaming service\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n"
            message += f"🆔 Trade ID: {trade.id}\n"
            message += f"🔗 Token: {trade.instrument_token}"
            
            # Send notification
            result = await send_pushover_notification(title, message)
            
            if result:
                logger.info(f"✅ Price fetch failure notification sent for trade {trade.id}")
            else:
                logger.error(f"❌ Failed to send price fetch failure notification for trade {trade.id}")
                
        except Exception as e:
            logger.error(f"❌ Error sending price fetch failure notification: {e}")
    
    async def _send_manual_close_notification(self, trade: Trade, current_price: float, stop_loss_price: float, error_reason: str):
        """
        Send Pushover notification when RSP stop loss order fails and manual intervention is required
        """
        try:
            # Calculate P&L for RSP trades (which are SELL trades)
            pnl = (float(trade.price) - current_price) * trade.quantity
            
            pnl_percent = (pnl / float(trade.value)) * 100
            
            title = f"🚨 RSP MANUAL CLOSE REQUIRED - {trade.tradingsymbol}"
            
            message = f"⚠️ RSP Stop Loss Order Failed - Manual Action Required\n\n"
            message += f"📊 Symbol: {trade.tradingsymbol}\n"
            message += f"🔄 RSP Transaction: SELL {trade.quantity} units\n"
            message += f"💰 Entry Price: ₹{trade.price}\n"
            message += f"� Current Price: ₹{current_price:.2f}\n"
            message += f"🛡️ Stop Loss: ₹{stop_loss_price:.2f}\n"
            message += f"💸 Current P&L: ₹{pnl:.2f} ({pnl_percent:+.2f}%)\n"
            message += f"💼 Trade Value: ₹{trade.value}\n\n"
            message += f"❌ Error: {error_reason}\n\n"
            message += f"🔧 REQUIRED ACTIONS:\n"
            message += f"   1. Manually close position in Kite/Zerodha\n"
            message += f"   2. Update trade record status to 'CLOSED'\n"
            message += f"   3. Record actual close price and P&L\n\n"
            message += f"📝 Trade Details:\n"
            message += f"   Trade ID: {trade.id}\n"
            message += f"   Token: {trade.instrument_token}\n"
            message += f"   Required Action: BUY {trade.quantity} units (to close RSP SELL position)\n"
            message += f"   ⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
            message += f"⚠️ This trade has been removed from automatic monitoring to prevent repeated failures."
            
            # Send high-priority notification
            result = await send_pushover_notification(title, message, priority=2)  # Emergency priority
            
            if result:
                logger.info(f"✅ Manual close notification sent for trade {trade.id}")
            else:
                logger.error(f"❌ Failed to send manual close notification for trade {trade.id}")
                
        except Exception as e:
            logger.error(f"❌ Error sending manual close notification: {e}")
    
    async def _initialize_kite_service(self):
        """
        Initialize Kite service for price fetching and order placement
        """
        try:
            from kite_services.kite_service import kite_service
            from database import User
            
            db = SessionLocal()
            
            # Get admin user
            admin_user = db.query(User).filter(User.username == "admin").first()
            if not admin_user:
                logger.error("❌ Admin user not found - cannot initialize Kite service")
                return
            
            # Load Kite session
            if kite_service.load_session_from_db(admin_user):
                self.kite_service = kite_service
                logger.info("✅ Kite service initialized successfully")
            else:
                logger.error("❌ Failed to load Kite session")
                
        except Exception as e:
            logger.error(f"❌ Error initializing Kite service: {e}")
        finally:
            if 'db' in locals():
                db.close()

# Global monitor instance
monitor = RSPTradeMonitor()

async def start_trade_monitoring():
    """Start the trade monitoring system"""
    await monitor.start_monitoring()

async def stop_trade_monitoring():
    """Stop the trade monitoring system"""
    await monitor.stop_monitoring()

if __name__ == "__main__":
    # Run the monitor directly
    asyncio.run(start_trade_monitoring())
