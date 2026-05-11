"""
Store Diagnosis API Server
FastAPI backend — calls gen_shop_report.py for data + render
Port: 8765
"""

import asyncio, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Setup ────────────────────────────────────────────────────────────────────
BRT = lambda: datetime.utcnow() - timedelta(hours=3)
SCRIPT = Path("/root/.openclaw/workspace/shop-diagnosis/gen_shop_report.py")
OUTPUT_DIR = Path("/tmp/files")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_URL = "https://qa.service.test.sankuai.com/api/flowCopilot/oversea/file/uploadImage"

# In-memory cache: key → {ts, data}
_cache: dict = {}
_CACHE_TTL = 1800  # 30 min

def cache_get(key):
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < _CACHE_TTL else None

def cache_set(key, data):
    if len(_cache) > 200:
        del _cache[min(_cache, key=lambda k: _cache[k]["ts"])]
    _cache[key] = {"ts": time.time(), "data": data}

app = FastAPI(title="Keeta Shop Diagnosis API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

executor = ThreadPoolExecutor(max_workers=3)

# ─── Core diagnosis runner ────────────────────────────────────────────────────

def run_diagnosis(shop_id: int, days: int = 7, lang: str = "pt") -> dict:
    """Run gen_shop_report.py and return structured result with img_url."""
    cache_key = f"{shop_id}:{days}:{BRT().strftime('%Y-%m-%d')}:{lang}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    img_path = OUTPUT_DIR / f"shop_report_{shop_id}.jpg"
    json_path = OUTPUT_DIR / f"shop_report_{shop_id}.json"

    r = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--shop_id", str(shop_id),
         "--days", str(days),
         "--lang", lang],
        capture_output=True, text=True, timeout=120,
        cwd=str(SCRIPT.parent)
    )

    if r.returncode != 0 or not img_path.exists():
        stderr = r.stderr[:500] if r.stderr else "unknown error"
        if "not found" in stderr.lower() or "no data" in stderr.lower():
            raise HTTPException(status_code=404, detail=f"Shop {shop_id} not found or no data")
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {stderr}")

    # Upload image
    upload_r = subprocess.run(
        ["curl", "-s", "-X", "POST", UPLOAD_URL, "-F", f"file=@{img_path}"],
        capture_output=True, text=True, timeout=30
    )
    img_url = None
    try:
        resp = json.loads(upload_r.stdout)
        img_url = resp.get("data", {}).get("url") or resp.get("url")
    except Exception:
        pass

    # Read raw data
    raw_data = {}
    if json_path.exists():
        try:
            raw_data = json.loads(json_path.read_text())
        except Exception:
            pass

    result = {
        "shop_id": shop_id,
        "shop_name": raw_data.get("shop_name", f"Shop #{shop_id}"),
        "start_date": raw_data.get("start_date", ""),
        "end_date": raw_data.get("end_date", ""),
        "img_url": img_url,
        "img_path": str(img_path),
        "metrics": {
            "total_orders": raw_data.get("total_orders"),
            "gmv_brl": raw_data.get("gmv_brl"),
            "aov_brl": raw_data.get("aov_brl"),
            "cancel_rate": raw_data.get("cancel_rate"),
            "avg_rating": raw_data.get("avg_rating"),
            "avg_delivery_min": raw_data.get("avg_delivery_min"),
            "active_spus": raw_data.get("active_spus"),
        },
        "generated_at": BRT().strftime("%Y-%m-%d %H:%M BRT"),
    }
    cache_set(cache_key, result)
    return result


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": BRT().isoformat(), "version": "2.0.0"}

@app.get("/diagnose")
async def diagnose(
    shop_id: int = Query(..., description="Keeta shop ID"),
    days: int = Query(7, description="Lookback days"),
    lang: str = Query("pt", description="Language: pt/en/zh"),
):
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, run_diagnosis, shop_id, days, lang),
            timeout=130
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Diagnosis timed out, please retry")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


class BotRequest(BaseModel):
    shop_id: int
    days: Optional[int] = 7
    lang: Optional[str] = "pt"

@app.post("/bot/diagnose")
async def bot_diagnose(req: BotRequest):
    return await diagnose(shop_id=req.shop_id, days=req.days, lang=req.lang)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
