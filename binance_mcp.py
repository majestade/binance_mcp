import os, time, hmac, hashlib, asyncio
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from pydantic import BaseModel, Field, validator

# -------------------------
# Config (env-driven)
# -------------------------
BINANCE_ENV       = os.getenv("BINANCE_ENV", "mainnet").lower()  # "mainnet" or "testnet"

# Auto-pick REST base from ENV unless explicitly overridden
if os.getenv("BINANCE_HTTP_BASE"):
    BINANCE_HTTP_BASE = os.getenv("BINANCE_HTTP_BASE")
else:
    BINANCE_HTTP_BASE = (
        "https://api.binance.com" if BINANCE_ENV == "mainnet"
        else "https://testnet.binance.vision"
    )

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

AGENT_KEY          = os.getenv("AGENT_KEY", "")
RECV_WINDOW_MS     = int(os.getenv("RECV_WINDOW_MS", "5000"))

# Optional guardrails (0 disables)
MAX_NOTIONAL_PER_ORDER  = float(os.getenv("MAX_NOTIONAL_PER_ORDER", "0"))
MAX_QTY_PER_ORDER       = float(os.getenv("MAX_QTY_PER_ORDER", "0"))
MAX_PRICE_DEVIATION_PCT = float(os.getenv("MAX_PRICE_DEVIATION_PCT", "0"))

if not AGENT_KEY:
    print("WARNING: AGENT_KEY is empty; set it to protect /api/*")

# -------------------------
# App & HTTP client
# -------------------------
app = FastAPI(title="Binance MCP", version="1.1.0")
client = httpx.AsyncClient(timeout=15.0)
time_offset_ms = 0  # Binance time - local time (ms)

# -------------------------
# Auth dependency
# -------------------------
def require_agent_key(x_agent_key: str = Header(None, convert_underscores=False)):
    if not AGENT_KEY or x_agent_key != AGENT_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# -------------------------
# Models
# -------------------------
class LimitOrderIn(BaseModel):
    symbol: str
    side: str = Field(..., regex="^(BUY|SELL)$")
    price: float
    qty: float
    tif: str = Field("GTC", regex="^(GTC|IOC|FOK)$")
    client_id: Optional[str] = None

    @validator("price", "qty")
    def pos(cls, v):
        if v <= 0: raise ValueError("must be > 0")
        return v

class OCOOrderIn(BaseModel):
    symbol: str
    side: str = Field(..., regex="^(BUY|SELL)$")
    quantity: float
    price: float
    stop: float
    stop_limit: float
    tif: str = Field("GTC", regex="^(GTC|IOC|FOK)$")
    client_id: Optional[str] = None

    @validator("quantity", "price", "stop", "stop_limit")
    def pos(cls, v):
        if v <= 0: raise ValueError("must be > 0")
        return v

    @validator("stop_limit")
    def oco_relation(cls, v, values):
        stop  = values.get("stop")
        price = values.get("price")
        side  = values.get("side")
        if not (stop and price and side):
            return v
        if side == "SELL":
            if not (price > stop and v <= stop):
                raise ValueError("SELL OCO requires price > stop and stop_limit <= stop")
        else:  # BUY OCO (rare)
            if not (price < stop and v >= stop):
                raise ValueError("BUY OCO requires price < stop and stop_limit >= stop")
        return v

class CancelOrderIn(BaseModel):
    symbol: str
    orderId: Optional[int] = None
    clientOrderId: Optional[str] = None

    @validator("symbol")
    def sym(cls, v):
        if not v: raise ValueError("symbol required")
        return v

# -------------------------
# Binance helpers
# -------------------------
def _sign(params: Dict[str, Any]) -> str:
    query = urlencode(params, doseq=True)
    sig = hmac.new(BINANCE_API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return sig

async def _timestamp_ms() -> int:
    return int(time.time() * 1000) + time_offset_ms

async def _sync_time():
    global time_offset_ms
    try:
        r = await client.get(f"{BINANCE_HTTP_BASE}/api/v3/time")
        r.raise_for_status()
        server_time = r.json()["serverTime"]
        local = int(time.time() * 1000)
        time_offset_ms = int(server_time) - local
        print(f"[time-sync] env={BINANCE_ENV} base={BINANCE_HTTP_BASE} offset_ms={time_offset_ms}")
    except Exception as e:
        print(f"[time-sync] failed: {e}")

@app.on_event("startup")
async def on_start():
    await _sync_time()
    async def refresher():
        while True:
            await asyncio.sleep(600)
            await _sync_time()
    asyncio.create_task(refresher())

async def _public_get(path: str, params: Dict[str, Any] = None):
    url = f"{BINANCE_HTTP_BASE}{path}"
    r = await client.get(url, params=params or {})
    r.raise_for_status()
    return r.json()

async def _signed_request(method: str, path: str, params: Dict[str, Any]):
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise HTTPException(500, "BINANCE_API_KEY/BINANCE_API_SECRET not set")

    params = dict(params or {})
    params["timestamp"] = await _timestamp_ms()
    params["recvWindow"] = RECV_WINDOW_MS
    params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BINANCE_HTTP_BASE}{path}"

    try:
        if method == "GET":
            r = await client.get(url, params=params, headers=headers)
        elif method == "POST":
            r = await client.post(url, params=params, headers=headers)
        elif method == "DELETE":
            r = await client.delete(url, params=params, headers=headers)
        else:
            raise HTTPException(500, f"Unsupported method {method}")
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = {"status_code": e.response.status_code, "text": e.response.text}
        raise HTTPException(status_code=e.response.status_code, detail=detail)

# Guardrails
def _enforce_limits(symbol: str, side: str, price: float, qty: float):
    notional = price * qty
    if MAX_NOTIONAL_PER_ORDER > 0 and notional > MAX_NOTIONAL_PER_ORDER:
        raise HTTPException(400, f"Notional {notional:.8f} exceeds MAX_NOTIONAL_PER_ORDER={MAX_NOTIONAL_PER_ORDER}")
    if MAX_QTY_PER_ORDER > 0 and qty > MAX_QTY_PER_ORDER:
        raise HTTPException(400, f"Quantity {qty} exceeds MAX_QTY_PER_ORDER={MAX_QTY_PER_ORDER}")

async def _get_last_price(symbol: str) -> Optional[float]:
    try:
        d = await _public_get("/api/v3/ticker/price", {"symbol": symbol})
        return float(d["price"])
    except Exception:
        return None

async def _enforce_price_deviation(symbol: str, user_price: float):
    if MAX_PRICE_DEVIATION_PCT <= 0:
        return
    last = await _get_last_price(symbol)
    if last is None:
        return
    deviation = abs(user_price - last) / last * 100.0
    if deviation > MAX_PRICE_DEVIATION_PCT:
        raise HTTPException(400, f"Price deviation {deviation:.2f}% > limit {MAX_PRICE_DEVIATION_PCT}% (last={last})")

# -------------------------
# Routes
# -------------------------
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "env": BINANCE_ENV,
        "base": BINANCE_HTTP_BASE,
        "time_offset_ms": time_offset_ms
    }

@app.get("/api/exchangeInfo")
async def exchange_info(symbol: Optional[str] = Query(None)):
    params = {"symbol": symbol} if symbol else None
    return await _public_get("/api/v3/exchangeInfo", params)

@app.get("/api/balances", dependencies=[Depends(require_agent_key)])
async def balances(assets: Optional[str] = Query(None, description="CSV e.g. USDT,BTC,SOL")):
    data = await _signed_request("GET", "/api/v3/account", {})
    bals = data.get("balances", [])
    if assets:
        wanted = {a.strip().upper() for a in assets.split(",") if a.strip()}
        bals = [b for b in bals if b.get("asset","").upper() in wanted]
    out = []
    for b in bals:
        free, locked = float(b["free"]), float(b["locked"])
        if free != 0 or locked != 0:
            out.append({"asset": b["asset"], "free": free, "locked": locked, "total": free+locked})
    return {"balances": out}

@app.get("/api/open-orders", dependencies=[Depends(require_agent_key)])
async def open_orders(symbol: Optional[str] = Query(None)):
    params = {"symbol": symbol} if symbol else {}
    return await _signed_request("GET", "/api/v3/openOrders", params)

class LimitOrderBody(LimitOrderIn): pass

@app.post("/api/order/limit", dependencies=[Depends(require_agent_key)])
async def place_limit(order: LimitOrderBody):
    _enforce_limits(order.symbol, order.side, order.price, order.qty)
    await _enforce_price_deviation(order.symbol, order.price)
    params = {
        "symbol": order.symbol.upper(),
        "side": order.side,
        "type": "LIMIT",
        "timeInForce": order.tif,
        "quantity": f"{order.qty}",
        "price": f"{order.price}",
        "newOrderRespType": "RESULT",
    }
    if order.client_id:
        params["newClientOrderId"] = order.client_id
    res = await _signed_request("POST", "/api/v3/order", params)
    return {"ok": True, "binance": res}

class OCOBody(OCOOrderIn): pass

@app.post("/api/order/oco", dependencies=[Depends(require_agent_key)])
async def place_oco(oco: OCOBody):
    _enforce_limits(oco.symbol, oco.side, oco.price, oco.quantity)
    await _enforce_price_deviation(oco.symbol, oco.price)
    params = {
        "symbol": oco.symbol.upper(),
        "side": oco.side,
        "quantity": f"{oco.quantity}",
        "price": f"{oco.price}",
        "stopPrice": f"{oco.stop}",
        "stopLimitPrice": f"{oco.stop_limit}",
        "stopLimitTimeInForce": oco.tif,
        "newOrderRespType": "RESULT",
    }
    if oco.client_id:
        params["listClientOrderId"] = oco.client_id
    res = await _signed_request("POST", "/api/v3/order/oco", params)
    return {"ok": True, "binance": res}

@app.delete("/api/order", dependencies=[Depends(require_agent_key)])
async def cancel_order(body: CancelOrderIn):
    if not body.orderId and not body.clientOrderId:
        raise HTTPException(400, "Provide orderId or clientOrderId")
    params = {"symbol": body.symbol.upper()}
    if body.orderId:
        params["orderId"] = body.orderId
    if body.clientOrderId:
        params["origClientOrderId"] = body.clientOrderId
    res = await _signed_request("DELETE", "/api/v3/order", params)
    return {"ok": True, "binance": res}