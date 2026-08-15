"""Friendly REST API endpoints for common operations."""
from fastapi import APIRouter, HTTPException, Query, Depends

from astock_api.serializer import normalize_result
from astock_api.registry import get_function, is_available
from astock_api.security import verify_api_key

router = APIRouter(
    prefix="/api/v1",
    tags=["market"],
    dependencies=[Depends(verify_api_key)]
)


# ── Market Data ──────────────────────────────────────────────────────

@router.get("/market/quote/{symbol}")
async def quote(symbol: str):
    """Real-time quote for a stock/ETF/index."""
    func = get_function("tencent_quote")
    if not func:
        raise HTTPException(status_code=503, detail="tencent_quote unavailable")

    result = func([symbol])
    return normalize_result(result)


@router.get("/market/bars/{symbol}")
async def bars(
    symbol: str,
    count: int = Query(default=100, ge=1, le=800),
    frequency: str = Query(default="daily", pattern=r"^(daily|1min|5min|15min|30min|1hour)$")
):
    """K-line data with MA from Baidu."""
    func = get_function("baidu_kline_with_ma")
    if not func:
        raise HTTPException(status_code=503, detail="baidu_kline_with_ma unavailable")

    import datetime
    start_time = (datetime.datetime.now() - datetime.timedelta(days=count+10)).strftime("%Y%m%d")
    result = func(symbol, start_time)
    return normalize_result(result)


@router.get("/market/orderbook/{symbol}")
async def orderbook(symbol: str):
    """Order book (五档盘口) via mootdx."""
    from astock_api.upstream.common import tdx_client, norm_ticker

    try:
        client = tdx_client()
        ticker = norm_ticker(symbol)
        # mootdx doesn't have direct orderbook API; use tencent as fallback
        result = get_function("tencent_quote")([symbol])
        return normalize_result(result)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/market/transactions/{symbol}")
async def transactions(symbol: str):
    """Tick-by-tick transactions via mootdx."""
    from astock_api.upstream.common import tdx_client, norm_ticker

    try:
        client = tdx_client()
        ticker = norm_ticker(symbol)
        # mootdx transaction data
        result = client.transactions(ticker, count=100)
        return normalize_result(result)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Valuation ────────────────────────────────────────────────────────

@router.get("/valuation/{symbol}")
async def valuation(symbol: str):
    """Comprehensive valuation analysis."""
    func = get_function("full_valuation")
    if not func:
        raise HTTPException(status_code=503, detail="full_valuation unavailable")

    result = func(symbol)
    return normalize_result(result)


# ── Fundamentals ─────────────────────────────────────────────────────

@router.get("/fundamentals/{symbol}")
async def fundamentals(symbol: str):
    """Financial reports (income/cashflow/balance) from Sina."""
    func = get_function("sina_financial_report")
    if not func:
        raise HTTPException(status_code=503, detail="sina_financial_report unavailable")

    result = func(symbol)
    return normalize_result(result)


@router.get("/f10/{symbol}")
async def f10(symbol: str):
    """F10 stock info from Eastmoney."""
    func = get_function("eastmoney_stock_info")
    if not func:
        raise HTTPException(status_code=503, detail="eastmoney_stock_info unavailable")

    result = func(symbol)
    return normalize_result(result)


# ── Announcements ───────────────────────────────────────────────────

@router.get("/announcements/{symbol}")
async def announcements(
    symbol: str,
    page_size: int = Query(default=10, ge=1, le=50)
):
    """CNInfo announcements."""
    func = get_function("cninfo_announcements")
    if not func:
        raise HTTPException(status_code=503, detail="cninfo_announcements unavailable")

    result = func(symbol, page_size)
    return normalize_result(result)


# ── News ─────────────────────────────────────────────────────────────

@router.get("/news/{symbol}")
async def news(
    symbol: str,
    page_size: int = Query(default=10, ge=1, le=50)
):
    """Stock news from Eastmoney."""
    func = get_function("eastmoney_stock_news")
    if not func:
        raise HTTPException(status_code=503, detail="eastmoney_stock_news unavailable")

    result = func(symbol, page_size)
    return normalize_result(result)


# ── Capital Flow ─────────────────────────────────────────────────────

@router.get("/fund-flow/{symbol}")
async def fund_flow(symbol: str):
    """120-day fund flow."""
    func = get_function("stock_fund_flow_120d")
    if not func:
        raise HTTPException(status_code=503, detail="stock_fund_flow_120d unavailable")

    result = func(symbol)
    return normalize_result(result)


@router.get("/board-fund-flow")
async def board_fund_flow(
    board_type: str = Query(default="concept", pattern=r"^(concept|region)$"),
    period: str = Query(default="5d", pattern=r"^(1d|3d|5d|10d)$"),
    top_n: int = Query(default=20, ge=1, le=100)
):
    """Board fund flow (concept or region)."""
    func = get_function("board_fund_flow")
    if not func:
        raise HTTPException(status_code=503, detail="board_fund_flow unavailable")

    result = func(board_type, period, top_n)
    return normalize_result(result)


# ── Limit Up/Down ───────────────────────────────────────────────────

@router.get("/limit-up")
async def limit_up_pool(date: str = Query(default=None)):
    """Limit-up pool from Eastmoney."""
    func = get_function("em_zt_pool")
    if not func:
        raise HTTPException(status_code=503, detail="em_zt_pool unavailable")

    result = func(date)
    return normalize_result(result)


# ── Signals ─────────────────────────────────────────────────────────

@router.get("/signals/dragon-tiger/{symbol}")
async def dragon_tiger(symbol: str):
    """Dragon-tiger board data."""
    func = get_function("dragon_tiger_board")
    if not func:
        raise HTTPException(status_code=503, detail="dragon_tiger_board unavailable")

    result = func(symbol)
    return normalize_result(result)


@router.get("/signals/margin/{symbol}")
async def margin(symbol: str):
    """Margin trading data."""
    func = get_function("margin_trading")
    if not func:
        raise HTTPException(status_code=503, detail="margin_trading unavailable")

    result = func(symbol)
    return normalize_result(result)


# ── Sentiment ───────────────────────────────────────────────────────

@router.get("/sentiment/hot")
async def hot_stocks(top: int = Query(default=20, ge=1, le=100)):
    """Hot stock ranking."""
    func = get_function("em_hot_rank")
    if not func:
        raise HTTPException(status_code=503, detail="em_hot_rank unavailable")

    result = func(top)
    return normalize_result(result)


@router.get("/sentiment/cls")
async def cls_news(page_size: int = Query(default=20, ge=1, le=50)):
    """CLS telegraph news."""
    func = get_function("cls_telegraph")
    if not func:
        raise HTTPException(status_code=503, detail="cls_telegraph unavailable")

    result = func(page_size)
    return normalize_result(result)


# ── Research ────────────────────────────────────────────────────────

@router.get("/research/reports/{symbol}")
async def reports(
    symbol: str,
    max_pages: int = Query(default=3, ge=1, le=10)
):
    """Research reports from Eastmoney."""
    func = get_function("eastmoney_reports")
    if not func:
        raise HTTPException(status_code=503, detail="eastmoney_reports unavailable")

    result = func(symbol, max_pages)
    return normalize_result(result)


@router.get("/research/eps-forecast/{symbol}")
async def eps_forecast(symbol: str):
    """EPS forecast from Tonghuashun."""
    func = get_function("ths_eps_forecast")
    if not func:
        raise HTTPException(status_code=503, detail="ths_eps_forecast unavailable")

    result = func(symbol)
    return normalize_result(result)


# ── Options ─────────────────────────────────────────────────────────

@router.get("/options/codes/{underlying}")
async def option_codes(underlying: str, call: bool = True):
    """Option contract codes from Sina."""
    func = get_function("sina_option_codes")
    if not func:
        raise HTTPException(status_code=503, detail="sina_option_codes unavailable")

    result = func(underlying, call)
    return normalize_result(result)


@router.get("/options/tquote/{code}")
async def option_tquote(code: str):
    """Option T-quote from Sina."""
    func = get_function("sina_option_tquote")
    if not func:
        raise HTTPException(status_code=503, detail="sina_option_tquote unavailable")

    result = func(code)
    return normalize_result(result)


@router.get("/options/greeks/{code}")
async def option_greeks(code: str):
    """Option Greeks from Sina."""
    func = get_function("sina_option_greeks")
    if not func:
        raise HTTPException(status_code=503, detail="sina_option_greeks unavailable")

    result = func(code)
    return normalize_result(result)


# ── Concept Blocks ──────────────────────────────────────────────────

@router.get("/concept-blocks/{symbol}")
async def concept_blocks(symbol: str):
    """Concept block membership from Eastmoney."""
    func = get_function("eastmoney_concept_blocks")
    if not func:
        raise HTTPException(status_code=503, detail="eastmoney_concept_blocks unavailable")

    result = func(symbol)
    return normalize_result(result)


# ── Backup Sources ──────────────────────────────────────────────────

@router.get("/backup/dragon-tiger")
async def backup_dragon_tiger(date: str = Query(default="")):
    """Backup dragon-tiger source."""
    func = get_function("dragon_tiger_backup")
    if not func:
        raise HTTPException(status_code=503, detail="dragon_tiger_backup unavailable")

    result = func(date) if date else func()
    return normalize_result(result)


@router.get("/backup/fund-flow/{symbol}")
async def backup_fund_flow(symbol: str, days: int = 20):
    """Backup fund flow source."""
    func = get_function("fund_flow_backup")
    if not func:
        raise HTTPException(status_code=503, detail="fund_flow_backup unavailable")

    result = func(symbol, days)
    return normalize_result(result)


@router.get("/backup/announcements/{symbol}")
async def backup_announcements(symbol: str, page_size: int = 10):
    """Backup announcements source."""
    func = get_function("announcements_backup")
    if not func:
        raise HTTPException(status_code=503, detail="announcements_backup unavailable")

    result = func(symbol, page_size)
    return normalize_result(result)


# ── Universe (read-only, no auth required) ───────────────────────────

universe_router = APIRouter(prefix="/api/v1")

@universe_router.get("/universe")
async def get_universe():
    """Read-only: return A-share security universe from the latest active snapshot."""
    import duckdb as dd

    try:
        conn = dd.connect("/app/data/astock_data.duckdb")

        # Get latest snapshot
        snap = conn.execute(
            "SELECT snapshot_id FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        if not snap:
            conn.close()
            return {"total": 0, "securities": [], "error": "no_snapshot_found"}

        snapshot_id = snap[0]

        # Get all securities from latest snapshot
        rows = conn.execute(
            "SELECT security_id, code, exchange, name, security_type FROM security_master WHERE snapshot_id=? ORDER BY code",
            [snapshot_id]
        ).fetchall()

        conn.close()

        securities = []
        for row in rows:
            securities.append({
                "security_id": row[0],
                "code": row[1],
                "exchange": row[2],
                "name": row[3],
                "security_type": row[4]
            })

        return {
            "snapshot_id": snapshot_id,
            "total": len(securities),
            "securities": securities
        }

    except Exception as e:
        raise HTTPException(status_code=503, detail=f"universe query failed: {str(e)}")
