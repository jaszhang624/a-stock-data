"""FastAPI application entry point."""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.openapi.utils import get_openapi

from astock_api.config import (
    UPSTREAM_VERSION, UPSTREAM_COMMIT, API_VERSION, ASTOCK_API_KEY
)
from astock_api.registry import register, list_functions, validate_registry
from astock_api.serializer import normalize_result
from astock_api.health import router as health_router
from astock_api.security import verify_api_key

# ── Register upstream functions ──────────────────────────────────────
from astock_api.upstream.common import tdx_client, get_prefix, norm_ticker
register("tdx_client", "market", "mootdx", "Create validated mootdx client", func=tdx_client, public=False)
register("get_prefix", "market", "local", "Get market prefix for stock code", func=get_prefix, public=False)
register("norm_ticker", "market", "local", "Normalize ticker to prefixed format", func=norm_ticker, public=False)

from astock_api.upstream.tencent import tencent_quote, baidu_kline_with_ma, full_valuation
register("tencent_quote", "market", "tencent", "Real-time quotes from Tencent", func=tencent_quote)
register("baidu_kline_with_ma", "market", "baidu", "K-line with MA from Baidu", func=baidu_kline_with_ma)
register("full_valuation", "valuation", "tencent+eastmoney", "Comprehensive valuation analysis", func=full_valuation)

from astock_api.upstream.eastmoney import (
    em_get, eastmoney_datacenter, eastmoney_concept_blocks,
    eastmoney_fund_flow_minute, board_fund_flow, stock_fund_flow_120d,
    eastmoney_stock_news, eastmoney_global_news, eastmoney_stock_info,
    em_zt_pool, em_zb_pool, em_dt_pool, em_yzt_pool,
    eastmoney_reports, download_pdf, eastmoney_industry_reports,
    industry_comparison
)

from astock_api.upstream.ths import em_hot_rank, em_hot_concept
register("em_get", "market", "eastmoney", "Eastmoney HTTP request wrapper", func=em_get, public=False)
register("eastmoney_datacenter", "market", "eastmoney", "Eastmoney datacenter API", func=eastmoney_datacenter)
register("eastmoney_concept_blocks", "market", "eastmoney", "Concept block membership", func=eastmoney_concept_blocks)
register("eastmoney_fund_flow_minute", "capital", "eastmoney", "Minute-level fund flow", func=eastmoney_fund_flow_minute)
register("board_fund_flow", "capital", "eastmoney", "Board fund flow (concept/region)", func=board_fund_flow)
register("stock_fund_flow_120d", "capital", "eastmoney", "120-day fund flow", func=stock_fund_flow_120d)
register("eastmoney_stock_news", "news", "eastmoney", "Stock news from Eastmoney", func=eastmoney_stock_news)
register("eastmoney_global_news", "news", "eastmoney", "Global market news", func=eastmoney_global_news)
register("eastmoney_stock_info", "fundamentals", "eastmoney", "F10 stock info", func=eastmoney_stock_info)
register("em_zt_pool", "limitup", "eastmoney", "Limit-up pool", func=em_zt_pool)
register("em_zb_pool", "limitup", "eastmoney", "Limit-down pool", func=em_zb_pool)
register("em_dt_pool", "limitup", "eastmoney", "Di-tian-ban pool", func=em_dt_pool)
register("em_yzt_pool", "limitup", "eastmoney", "Yesterday limit-up pool", func=em_yzt_pool)
register("eastmoney_reports", "research", "eastmoney", "Research reports list", func=eastmoney_reports)
register("download_pdf", "research", "eastmoney", "Download report PDF", func=download_pdf)
register("eastmoney_industry_reports", "research", "eastmoney", "Industry reports", func=eastmoney_industry_reports)
register("industry_comparison", "signals", "eastmoney", "Industry comparison", func=industry_comparison)
register("em_hot_rank", "sentiment", "eastmoney", "Hot stock ranking", func=em_hot_rank)
register("em_hot_concept", "sentiment", "eastmoney", "Hot concept ranking", func=em_hot_concept)

from astock_api.upstream.ths import (
    ths_eps_forecast, ths_hot_reason, ths_limit_up_pool, ths_hot_list
)
register("ths_eps_forecast", "fundamentals", "ths", "EPS forecast from Tonghuashun", func=ths_eps_forecast)
register("ths_hot_reason", "sentiment", "ths", "Hot stock reason analysis", func=ths_hot_reason)
register("ths_limit_up_pool", "limitup", "ths", "Limit-up attribution from THS", func=ths_limit_up_pool)
register("ths_hot_list", "sentiment", "ths", "Hot stock list from THS", func=ths_hot_list)

from astock_api.upstream.cninfo import cninfo_announcements, _cninfo_orgid, cninfo_irm
register("cninfo_announcements", "announcements", "cninfo", "CNInfo announcements", func=cninfo_announcements)
register("cninfo_irm", "announcements", "cninfo", "Investor relations interaction", func=cninfo_irm)

from astock_api.upstream.sina import (
    sina_financial_report, sina_option_codes, sina_option_tquote, sina_option_greeks
)
register("sina_financial_report", "fundamentals", "sina", "Financial reports (income/cashflow/balance)", func=sina_financial_report)
register("sina_option_codes", "options", "sina", "Option contract codes", func=sina_option_codes)
register("sina_option_tquote", "options", "sina", "Option T-quote", func=sina_option_tquote)
register("sina_option_greeks", "options", "sina", "Option Greeks", func=sina_option_greeks)

from astock_api.upstream.cls import cls_telegraph
register("cls_telegraph", "news", "cls", "CLS telegraph news", func=cls_telegraph)

from astock_api.upstream.other import (
    iwencai_search, iwencai_query, dedup_articles,
    hsgt_realtime, dragon_tiger_board, lockup_expiry, daily_dragon_tiger,
    margin_trading, block_trade, holder_num_change, dividend_history,
    limit_up_sentiment, forward_pe, pe_digestion, calc_peg,
    dragon_tiger_backup, fund_flow_backup, announcements_backup
)
register("iwencai_search", "research", "iwencai", "Semantic search for research reports", func=iwencai_search, requires_key=True)
register("iwencai_query", "research", "iwencai", "Natural language query", func=iwencai_query, requires_key=True)
register("dedup_articles", "research", "iwencai", "Deduplicate articles", func=dedup_articles)
register("hsgt_realtime", "signals", "eastmoney", "HK-Shenzhen/Shanghai connect realtime", func=hsgt_realtime)
register("dragon_tiger_board", "signals", "eastmoney", "Dragon-tiger board", func=dragon_tiger_board)
register("lockup_expiry", "signals", "eastmoney", "Lock-up expiry schedule", func=lockup_expiry)
register("daily_dragon_tiger", "signals", "eastmoney", "Daily dragon-tiger filtered", func=daily_dragon_tiger)
register("margin_trading", "capital", "eastmoney", "Margin trading data", func=margin_trading)
register("block_trade", "capital", "eastmoney", "Block trade data", func=block_trade)
register("holder_num_change", "signals", "eastmoney", "Holder count changes", func=holder_num_change)
register("dividend_history", "fundamentals", "eastmoney", "Dividend history", func=dividend_history)
register("limit_up_sentiment", "sentiment", "eastmoney", "Limit-up sentiment", func=limit_up_sentiment)
register("forward_pe", "valuation", "local", "Forward PE calculation", func=forward_pe)
register("pe_digestion", "valuation", "local", "PE digestion time", func=pe_digestion)
register("calc_peg", "valuation", "local", "PEG calculation", func=calc_peg)
register("dragon_tiger_backup", "signals", "backup", "Dragon-tiger backup source", func=dragon_tiger_backup)
register("fund_flow_backup", "capital", "backup", "Fund flow backup source", func=fund_flow_backup)
register("announcements_backup", "announcements", "backup", "Announcements backup source", func=announcements_backup)

# ── App lifecycle ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    import os, logging
    from astock_api.config import DATA_DIR, CACHE_DIR, LOG_LEVEL

    # Ensure data/cache dirs exist
    for d in [DATA_DIR, CACHE_DIR]:
        os.makedirs(d, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Validate registry at startup
    try:
        total, public_count, internal_count = validate_registry()
        logging.getLogger(__name__).info(
            f"Registry OK: {total} total, {public_count} public, {internal_count} internal"
        )
    except RuntimeError as e:
        logging.getLogger(__name__).error(f"Registry validation failed: {e}")
        raise

    # Initialize and start job engine
    from astock_api.job_engine import JobEngine
    from astock_api.job_handlers import get_handler

    db_path = os.path.join(DATA_DIR, "astock_jobs.db")
    engine = JobEngine(db_path=db_path, data_dir=DATA_DIR)
    engine.initialize()

    # Start worker with handler dispatcher
    def job_handler(payload):
        job_type = payload.get("job_type", "market_bars_snapshot")
        handler = get_handler(job_type)
        if not handler:
            from astock_api.job_engine import PermanentJobError
            raise PermanentJobError(f"No handler for job_type: {job_type}")
        return handler(payload)

    engine.start(job_handler)
    app.state.job_engine = engine
    logging.getLogger(__name__).info("Job engine started")

    try:
        yield
    finally:
        engine.stop()


def get_engine():
    """Return the job engine instance from app state."""
    return getattr(app.state, "job_engine", None)


# ── App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="A-Stock Data API",
    description="Dockerized A-share data service. Thin wrapper around upstream SKILL.md functions.",
    version=API_VERSION,
    lifespan=lifespan,
)

# OpenAPI patch for API key auth
if ASTOCK_API_KEY:
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, openapi_version=app.openapi_version,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "ApiKeyAuth"
        ] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
        schema["security"] = [{"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return schema
    app.openapi = custom_openapi

# Mount health routes (direct append — include_router doesn't work in this FastAPI version)
app.router.routes.extend(health_router.routes)

# Mount API routes
from astock_api.api.routes import router as api_routes, universe_router
app.router.routes.extend(api_routes.routes)
app.router.routes.extend(universe_router.routes)

# Mount job routes (job engine started in lifespan)
from astock_api.job_routes import router as job_router
app.router.routes.extend(job_router.routes)

# Store app reference for job routes to access engine
job_router.app = app


@app.get("/")
async def root():
    return {
        "service": "a-stock-data-api",
        "status": "running",
        "api_version": API_VERSION,
        "upstream_version": UPSTREAM_VERSION,
        "upstream_commit": UPSTREAM_COMMIT,
        "docs_url": "/docs",
    }


@app.get("/api/v1/meta", dependencies=[Depends(verify_api_key)])
async def meta():
    funcs = list_functions()
    sources = set(f["source"] for f in funcs)
    return {
        "api_version": API_VERSION,
        "upstream_repo": "https://github.com/simonlin1212/a-stock-data",
        "upstream_version": UPSTREAM_VERSION,
        "upstream_commit": UPSTREAM_COMMIT,
        "available_functions_count": len(funcs),
        "available_sources": sorted(sources),
    }


@app.get("/api/v1/functions", dependencies=[Depends(verify_api_key)])
async def functions():
    return list_functions()


# ── Generic call API ────────────────────────────────────────────────
@app.post("/api/v1/call/{function_name}", dependencies=[Depends(verify_api_key)])
async def call_function(function_name: str, body: dict = None):
    from astock_api.registry import get_function, is_available

    func = get_function(function_name)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Function not found or not publicly callable: {function_name}")

    if not is_available(function_name):
        raise HTTPException(
            status_code=503,
            detail="Function requires configuration (e.g., IWENCAI_API_KEY)"
        )

    kwargs = body.get("kwargs", {}) if body else {}

    # Validate parameters before calling
    import inspect
    try:
        sig = inspect.signature(func)
        sig.bind(**kwargs)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    start = time.time()
    try:
        result = func(**kwargs)
        elapsed_ms = int((time.time() - start) * 1000)

        return {
            "ok": True,
            "function": function_name,
            "data": normalize_result(result),
            "meta": {
                "elapsed_ms": elapsed_ms,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "upstream_version": UPSTREAM_VERSION,
                "upstream_commit": UPSTREAM_COMMIT,
            }
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        import logging
        logging.getLogger(__name__).error(
            f"call_function error: {function_name}({kwargs}) -> {e}"
        )

        # Classify error type
        if isinstance(e, (TimeoutError,)):
            status = 504
        elif "connection" in str(e).lower() or "reset" in str(e).lower():
            status = 503
        else:
            status = 502

        raise HTTPException(
            status_code=status,
            detail={
                "ok": False,
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                    "source": function_name,
                },
            }
        )
