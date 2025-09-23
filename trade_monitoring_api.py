"""
Trade Monitoring API Endpoints

Provides API endpoints to monitor and control the trade monitoring system
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Dict, List, Any, Optional
from datetime import datetime, date
from sqlalchemy import func
from trade_monitoring.monitor_trade import monitor as trade_monitor
from database import SessionLocal
from trading_models import Trade
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trade-monitoring", tags=["trade-monitoring"])

@router.get("/status")
async def get_monitoring_status() -> Dict[str, Any]:
    """Get current trade monitoring status"""
    try:
        return {
            "monitoring_active": trade_monitor.monitoring_active,
            "monitored_trades_count": len(trade_monitor.monitored_trades),
            "monitored_trade_ids": list(trade_monitor.monitored_trades),
            "stop_losses": trade_monitor.trade_stop_losses,
            "kite_service_available": trade_monitor.kite_service is not None
        }
    except Exception as e:
        logger.error(f"Error getting monitoring status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/trades")
async def get_monitored_trades() -> List[Dict[str, Any]]:
    """Get all trades currently being monitored"""
    try:
        db = SessionLocal()
        
        # Get trades being monitored
        trades = db.query(Trade).filter(
            Trade.id.in_(trade_monitor.monitored_trades),
            Trade.status == "open"
        ).all()
        
        result = []
        for trade in trades:
            stop_loss = trade_monitor.trade_stop_losses.get(trade.id)
            result.append({
                "trade_id": trade.id,
                "trade_id_string": trade.trade_id,
                "symbol": trade.tradingsymbol,
                "transaction_type": trade.transaction_type,
                "quantity": trade.quantity,
                "entry_price": float(trade.price),
                "stop_loss_price": stop_loss,
                "trade_value": float(trade.value),
                "trade_time": trade.trade_time.isoformat(),
                "status": trade.status
            })
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting monitored trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'db' in locals():
            db.close()

@router.get("/trades/all")
async def get_all_trades(
    limit: Optional[int] = Query(100, description="Number of trades to return (max 1000)"),
    offset: Optional[int] = Query(0, description="Number of trades to skip"),
    status: Optional[str] = Query(None, description="Filter by trade status (open, closed)"),
    trade_type: Optional[str] = Query(None, description="Filter by trade ID pattern (e.g., 'postcrossover', 'rsp')"),
    trade_date: Optional[str] = Query("today", description="Filter by trade date (YYYY-MM-DD format, use 'today' for current date, 'all' for all dates)")
) -> Dict[str, Any]:
    """
    Get all trades from the trades table with specified fields:
    - tradingsymbol
    - instrument_token  
    - transaction_type
    - quantity
    - price
    - status
    
    By default, shows only TODAY'S trades.
    
    Additional filters:
    - status: Filter by 'open' or 'closed' trades
    - trade_type: Filter by trade ID pattern ('postcrossover', 'rsp', etc.)
    - trade_date: Filter by date ('today' [default], 'all', or 'YYYY-MM-DD' format)
    """
    try:
        db = SessionLocal()
        
        # Validate limit
        if limit and limit > 1000:
            limit = 1000
        
        # Build query
        query = db.query(Trade)
        
        # Apply filters
        if status:
            query = query.filter(Trade.status == status.lower())
        
        if trade_type:
            query = query.filter(Trade.trade_id.like(f"{trade_type}_%"))
        
        # Apply date filter (default to today's trades)
        if trade_date and trade_date.lower() != 'all':
            if trade_date.lower() == 'today':
                # Filter for today's trades
                today = date.today()
                query = query.filter(
                    func.date(Trade.trade_time) == today
                )
            else:
                try:
                    # Parse date string (YYYY-MM-DD format)
                    filter_date = datetime.strptime(trade_date, '%Y-%m-%d').date()
                    query = query.filter(
                        func.date(Trade.trade_time) == filter_date
                    )
                except ValueError:
                    raise HTTPException(
                        status_code=400, 
                        detail="Invalid date format. Use YYYY-MM-DD, 'today', or 'all'"
                    )
        
        # Apply ordering, limit and offset
        trades = query.order_by(Trade.trade_time.desc()).offset(offset).limit(limit).all()
        
        # Get total count for pagination info
        total_count = db.query(Trade).count()
        # Since we now have a default date filter, always use query.count() for filtered_count
        filtered_count = query.count()
        
        # Format response with requested fields
        result = []
        for trade in trades:
            result.append({
                "id": trade.id,
                "trade_id": trade.trade_id,
                "tradingsymbol": trade.tradingsymbol,
                "instrument_token": trade.instrument_token,
                "transaction_type": trade.transaction_type,
                "quantity": trade.quantity,
                "price": float(trade.price),
                "status": getattr(trade, 'status', 'unknown'),
                # Additional useful fields
                "value": float(trade.value) if trade.value else 0.0,
                "trade_time": trade.trade_time.isoformat() if trade.trade_time else None,
                "closed_at": trade.closed_at.isoformat() if getattr(trade, 'closed_at', None) else None,
                "close_reason": getattr(trade, 'close_reason', None),
                "exchange": getattr(trade, 'exchange', None)
            })
        
        return {
            "trades": result,
            "pagination": {
                "total": total_count,
                "filtered_total": filtered_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < filtered_count
            },
            "filters_applied": {
                "status": status,
                "trade_type": trade_type,
                "trade_date": trade_date
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting all trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'db' in locals():
            db.close()

@router.get("/trades/postcrossover")
async def get_all_postcrossover_trades() -> List[Dict[str, Any]]:
    """Get all postcrossover trades (monitored and completed)"""
    try:
        db = SessionLocal()
        
        trades = db.query(Trade).filter(
            Trade.trade_id.like("postcrossover_%")
        ).order_by(Trade.trade_time.desc()).limit(50).all()
        
        result = []
        for trade in trades:
            result.append({
                "trade_id": trade.id,
                "trade_id_string": trade.trade_id,
                "symbol": trade.tradingsymbol,
                "transaction_type": trade.transaction_type,
                "quantity": trade.quantity,
                "entry_price": float(trade.price),
                "trade_value": float(trade.value),
                "trade_time": trade.trade_time.isoformat(),
                "status": getattr(trade, 'status', 'unknown'),
                "close_reason": getattr(trade, 'close_reason', None),
                "closed_at": trade.closed_at.isoformat() if getattr(trade, 'closed_at', None) else None,
                "is_monitored": trade.id in trade_monitor.monitored_trades,
                "stop_loss_price": trade_monitor.trade_stop_losses.get(trade.id)
            })
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting postcrossover trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'db' in locals():
            db.close()

@router.post("/start")
async def start_monitoring() -> Dict[str, str]:
    """Manually start trade monitoring"""
    try:
        if trade_monitor.monitoring_active:
            return {"message": "Trade monitoring is already active"}
        
        import asyncio
        asyncio.create_task(trade_monitor.start_monitoring())
        
        return {"message": "Trade monitoring started successfully"}
        
    except Exception as e:
        logger.error(f"Error starting trade monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop")
async def stop_monitoring() -> Dict[str, str]:
    """Manually stop trade monitoring"""
    try:
        if not trade_monitor.monitoring_active:
            return {"message": "Trade monitoring is already stopped"}
        
        await trade_monitor.stop_monitoring()
        
        return {"message": "Trade monitoring stopped successfully"}
        
    except Exception as e:
        logger.error(f"Error stopping trade monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check for trade monitoring system"""
    try:
        db = SessionLocal()
        
        # Count trades
        total_trades = db.query(Trade).count()
        postcrossover_trades = db.query(Trade).filter(
            Trade.trade_id.like("postcrossover_%")
        ).count()
        open_trades = db.query(Trade).filter(
            Trade.status == "open",
            Trade.trade_id.like("postcrossover_%")
        ).count()
        
        return {
            "status": "healthy",
            "monitoring_active": trade_monitor.monitoring_active,
            "total_trades": total_trades,
            "postcrossover_trades": postcrossover_trades,
            "open_postcrossover_trades": open_trades,
            "monitored_trades": len(trade_monitor.monitored_trades),
            "kite_service_available": trade_monitor.kite_service is not None
        }
        
    except Exception as e:
        logger.error(f"Error in health check: {e}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "monitoring_active": trade_monitor.monitoring_active
        }
    finally:
        if 'db' in locals():
            db.close()
