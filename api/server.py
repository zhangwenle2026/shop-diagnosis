"""
Store Diagnosis API Server
FastAPI backend for shop health diagnosis
Port: 8765
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Store Diagnosis API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BI_SCRIPTS = os.path.expanduser("~/.openclaw/skills/bi-query-sql/scripts/mt-mcp-client.js")
executor = ThreadPoolExecutor(max_workers=4)


# ─── Hive Query ───────────────────────────────────────────────────────────────

def hive_query(sql: str, group: str = "keeta-br-b-ops") -> list:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
        f.write(sql)
        sql_file = f.name
    try:
        result = subprocess.run(
            ["node", BI_SCRIPTS, "submit", "--server", "fra_hive",
             "--sql-file", sql_file, "--group", group],
            capture_output=True, text=True, timeout=30
        )
        data = None
        for line in result.stdout.strip().split('\n'):
            if line.strip().startswith('{"code"'):
                data = json.loads(line.strip())
                break
        if data is None:
            raise Exception(f"No JSON from submit. stdout={result.stdout[:300]}")
        if data.get('code') != 1:
            raise Exception(f"Submit failed: {data}")
        qid = data['data']

        for _ in range(40):
            time.sleep(3)
            info_r = subprocess.run(
                ["node", BI_SCRIPTS, "query-info", "--server", "fra_hive", "--qid", qid],
                capture_output=True, text=True, timeout=15
            )
            info = None
            for line in info_r.stdout.strip().split('\n'):
                if line.strip().startswith('{"code"'):
                    info = json.loads(line.strip())
                    break
            if info is None:
                continue
            st = info.get('data', {}).get('status', '')
            if st == 'FINISHED':
                break
            if st in ('FAILED', 'CANCELLED'):
                raise Exception(f"Query {st}: {info}")
        else:
            raise TimeoutError("Hive query timed out after 120s")

        fetch_r = subprocess.run(
            ["node", BI_SCRIPTS, "query-result", "--server", "fra_hive",
             "--qid", qid, "--offset", "0", "--length", "1000"],
            capture_output=True, text=True, timeout=15
        )
        fetch_data = None
        for line in fetch_r.stdout.strip().split('\n'):
            if line.strip().startswith('{"code"'):
                fetch_data = json.loads(line.strip())
                break
        if fetch_data is None:
            raise Exception("No JSON from query-result")
        inner = json.loads(fetch_data['data']['data'])
        return inner['data']['data']
    finally:
        try:
            os.unlink(sql_file)
        except Exception:
            pass


# ─── Score Logic ──────────────────────────────────────────────────────────────

def calc_health_score(metrics: dict) -> tuple:
    score = 100
    deductions = []

    if metrics['avg_prep_min'] > 40:
        score -= 10
        deductions.append({"key": "prep_time", "label": "Prep Time", "pts": 10,
                            "value": f"{metrics['avg_prep_min']:.1f} min", "threshold": "> 40 min"})
    elif metrics['avg_prep_min'] > 30:
        score -= 5
        deductions.append({"key": "prep_time", "label": "Prep Time", "pts": 5,
                            "value": f"{metrics['avg_prep_min']:.1f} min", "threshold": "> 30 min"})

    if metrics['avg_delivery_min'] > 50:
        score -= 10
        deductions.append({"key": "delivery_time", "label": "Delivery Time", "pts": 10,
                            "value": f"{metrics['avg_delivery_min']:.1f} min", "threshold": "> 50 min"})
    elif metrics['avg_delivery_min'] > 40:
        score -= 5
        deductions.append({"key": "delivery_time", "label": "Delivery Time", "pts": 5,
                            "value": f"{metrics['avg_delivery_min']:.1f} min", "threshold": "> 40 min"})

    if metrics['cancel_rate'] > 10:
        score -= 15
        deductions.append({"key": "cancel_rate", "label": "Cancellation Rate", "pts": 15,
                            "value": f"{metrics['cancel_rate']:.1f}%", "threshold": "> 10%"})
    elif metrics['cancel_rate'] > 5:
        score -= 8
        deductions.append({"key": "cancel_rate", "label": "Cancellation Rate", "pts": 8,
                            "value": f"{metrics['cancel_rate']:.1f}%", "threshold": "> 5%"})

    if metrics['avg_rating'] < 3.0:
        score -= 15
        deductions.append({"key": "rating", "label": "Rating", "pts": 15,
                            "value": f"{metrics['avg_rating']:.2f}", "threshold": "< 3.0"})
    elif metrics['avg_rating'] < 4.0:
        score -= 8
        deductions.append({"key": "rating", "label": "Rating", "pts": 8,
                            "value": f"{metrics['avg_rating']:.2f}", "threshold": "< 4.0"})

    if metrics['spu_count'] < 10:
        score -= 10
        deductions.append({"key": "spu_count", "label": "Menu Items", "pts": 10,
                            "value": str(metrics['spu_count']), "threshold": "< 10 items"})
    elif metrics['spu_count'] < 20:
        score -= 5
        deductions.append({"key": "spu_count", "label": "Menu Items", "pts": 5,
                            "value": str(metrics['spu_count']), "threshold": "< 20 items"})

    return max(0, score), sorted(deductions, key=lambda x: -x['pts'])


def get_health_level(score: int) -> dict:
    if score >= 85:
        return {"level": "Excellent", "color": "#22c55e", "emoji": "🌟"}
    elif score >= 70:
        return {"level": "Good", "color": "#84cc16", "emoji": "✅"}
    elif score >= 55:
        return {"level": "Fair", "color": "#f59e0b", "emoji": "⚠️"}
    else:
        return {"level": "Needs Attention", "color": "#ef4444", "emoji": "🔴"}


def build_diagnosis_items(metrics: dict) -> list:
    items = []
    items.append({
        "category": "Operations", "metric": "Avg Prep Time",
        "value": f"{metrics['avg_prep_min']:.1f} min",
        "status": "ok" if metrics['avg_prep_min'] <= 30 else ("warn" if metrics['avg_prep_min'] <= 40 else "bad"),
        "benchmark": "≤ 30 min",
        "suggestion": "Review kitchen workflow and peak-hour staffing" if metrics['avg_prep_min'] > 30 else None
    })
    items.append({
        "category": "Operations", "metric": "Avg Delivery Time",
        "value": f"{metrics['avg_delivery_min']:.1f} min",
        "status": "ok" if metrics['avg_delivery_min'] <= 40 else ("warn" if metrics['avg_delivery_min'] <= 50 else "bad"),
        "benchmark": "≤ 40 min",
        "suggestion": "Adjust delivery radius or courier fleet" if metrics['avg_delivery_min'] > 40 else None
    })
    items.append({
        "category": "Quality", "metric": "Cancellation Rate",
        "value": f"{metrics['cancel_rate']:.1f}%",
        "status": "ok" if metrics['cancel_rate'] <= 5 else ("warn" if metrics['cancel_rate'] <= 10 else "bad"),
        "benchmark": "≤ 5%",
        "suggestion": "Investigate top cancellation reasons" if metrics['cancel_rate'] > 5 else None
    })
    items.append({
        "category": "Quality", "metric": "Avg Customer Rating",
        "value": f"{metrics['avg_rating']:.2f} ⭐",
        "status": "ok" if metrics['avg_rating'] >= 4.0 else ("warn" if metrics['avg_rating'] >= 3.0 else "bad"),
        "benchmark": "≥ 4.0",
        "suggestion": "Follow up on negative reviews, improve food/packaging" if metrics['avg_rating'] < 4.0 else None
    })
    items.append({
        "category": "Menu", "metric": "Menu Items (SPU)",
        "value": str(metrics['spu_count']),
        "status": "ok" if metrics['spu_count'] >= 20 else ("warn" if metrics['spu_count'] >= 10 else "bad"),
        "benchmark": "≥ 20 items",
        "suggestion": "Expand menu to attract more customer segments" if metrics['spu_count'] < 20 else None
    })
    items.append({
        "category": "Menu", "metric": "Popular Items",
        "value": str(metrics['hot_spu_count']),
        "status": "ok" if metrics['hot_spu_count'] >= 3 else "warn",
        "benchmark": "≥ 3 popular",
        "suggestion": "Promote best-sellers and bundle deals" if metrics['hot_spu_count'] < 3 else None
    })
    items.append({
        "category": "Volume", "metric": "Total Orders",
        "value": str(metrics['total_orders']),
        "status": "info", "benchmark": f"Last {metrics['days']} days", "suggestion": None
    })
    items.append({
        "category": "Volume", "metric": "Total Revenue",
        "value": f"R$ {metrics['total_income']:.2f}",
        "status": "info", "benchmark": f"Last {metrics['days']} days", "suggestion": None
    })
    return items


# ─── HTML Report Template ─────────────────────────────────────────────────────

def render_report_html(shop_data: dict) -> str:
    metrics = shop_data['metrics']
    diagnosis = shop_data['diagnosis']
    score = shop_data['health_score']
    level_color = shop_data['health_color']
    level_name = shop_data['health_level']
    level_emoji = shop_data['health_emoji']
    shop_name = shop_data.get('shop_name', f"Shop #{shop_data['shop_id']}")
    deductions = shop_data.get('deductions', [])

    sc_color = {"ok": "#22c55e", "warn": "#f59e0b", "bad": "#ef4444", "info": "#6366f1"}
    sc_icon = {"ok": "✓", "warn": "⚠", "bad": "✗", "info": "ℹ"}

    diag_rows = ""
    for item in diagnosis:
        sc = item['status']
        color = sc_color.get(sc, "#6b7280")
        icon = sc_icon.get(sc, "•")
        sug = (f'<div class="suggestion">💡 {item["suggestion"]}</div>'
               if item.get('suggestion') else '')
        diag_rows += f"""
        <div class="diag-row status-{sc}">
          <div class="diag-header">
            <span class="diag-icon" style="color:{color}">{icon}</span>
            <span class="diag-metric">{item['metric']}</span>
            <span class="diag-value" style="color:{color}">{item['value']}</span>
          </div>
          <div class="diag-meta">
            <span class="diag-bench">Benchmark: {item['benchmark']}</span>
            <span class="diag-cat">{item['category']}</span>
          </div>
          {sug}
        </div>"""

    ded_tags = "".join(
        f'<span class="ded-tag">-{d["pts"]} {d["label"]}</span>'
        for d in deductions[:4]
    )

    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Store Diagnosis – {shop_name}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;color:#1a1a2e;width:390px}}
.header{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 60%,#0f3460 100%);padding:20px 16px 24px;color:#fff}}
.header-top{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
.keeta-badge{{background:#FFD700;color:#000;font-size:10px;font-weight:800;padding:2px 7px;border-radius:4px;letter-spacing:.5px}}
.report-label{{font-size:11px;color:rgba(255,255,255,.6);letter-spacing:1px;text-transform:uppercase}}
.shop-name{{font-size:22px;font-weight:700;margin:8px 0 2px;line-height:1.2}}
.shop-id{{font-size:12px;color:rgba(255,255,255,.5)}}
.score-card{{background:rgba(255,255,255,.08);border-radius:12px;padding:16px;margin:16px 0 0;display:flex;align-items:center;gap:16px}}
.score-circle{{width:72px;height:72px;border-radius:50%;background:{level_color};display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 4px 16px rgba(0,0,0,.3)}}
.score-num{{font-size:26px;font-weight:800;color:#fff}}
.score-info{{flex:1}}
.score-level{{font-size:18px;font-weight:700;color:{level_color}}}
.score-emoji{{font-size:20px;margin-left:4px}}
.score-date{{font-size:11px;color:rgba(255,255,255,.5);margin-top:4px}}
.deductions{{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}}
.ded-tag{{background:rgba(239,68,68,.2);color:#fca5a5;font-size:10px;padding:2px 7px;border-radius:20px;border:1px solid rgba(239,68,68,.3)}}
.section{{padding:0 12px;margin:16px 0}}
.section-title{{font-size:13px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;padding-left:4px}}
.diag-row{{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:8px;border-left:3px solid #e5e7eb}}
.diag-row.status-ok{{border-left-color:#22c55e}}
.diag-row.status-warn{{border-left-color:#f59e0b}}
.diag-row.status-bad{{border-left-color:#ef4444}}
.diag-row.status-info{{border-left-color:#6366f1}}
.diag-header{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
.diag-icon{{font-size:14px;font-weight:700}}
.diag-metric{{font-size:13px;font-weight:600;flex:1}}
.diag-value{{font-size:13px;font-weight:700}}
.diag-meta{{display:flex;justify-content:space-between;margin-top:2px}}
.diag-bench{{font-size:11px;color:#9ca3af}}
.diag-cat{{font-size:10px;color:#d1d5db;background:#f3f4f6;padding:1px 6px;border-radius:3px}}
.suggestion{{margin-top:8px;font-size:11px;color:#6b7280;background:#fffbeb;border-radius:6px;padding:6px 8px;border-left:2px solid #f59e0b}}
.footer{{text-align:center;padding:16px;font-size:10px;color:#9ca3af}}
</style>
</head>
<body>
<div class="header">
  <div class="header-top">
    <span class="keeta-badge">KEETA</span>
    <span class="report-label">Store Diagnosis Report</span>
  </div>
  <div class="shop-name">{shop_name}</div>
  <div class="shop-id">Shop ID: {shop_data['shop_id']} · Last {metrics['days']} days</div>
  <div class="score-card">
    <div class="score-circle"><span class="score-num">{score}</span></div>
    <div class="score-info">
      <div><span class="score-level">{level_name}</span><span class="score-emoji">{level_emoji}</span></div>
      <div class="score-date">Generated {now_str}</div>
      <div class="deductions">{ded_tags}</div>
    </div>
  </div>
</div>
<div class="section">
  <div class="section-title">📊 Diagnosis Details</div>
  {diag_rows}
</div>
<div class="footer">Keeta Brazil · SP Metropolitan · Auto-generated by AI Eric</div>
</body>
</html>"""


# ─── Image Generation ─────────────────────────────────────────────────────────

def generate_report_image(shop_data: dict) -> Optional[str]:
    try:
        os.makedirs("/tmp/files", exist_ok=True)
        html = render_report_html(shop_data)
        html_path = f"/tmp/files/diagnosis_{shop_data['shop_id']}.html"
        img_path = f"/tmp/files/diagnosis_{shop_data['shop_id']}.jpg"

        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)

        subprocess.run(["agent-browser", "open", f"file://{html_path}"],
                       capture_output=True, timeout=10)
        subprocess.run(["agent-browser", "set", "device", "iPhone 14"],
                       capture_output=True, timeout=10)
        subprocess.run(["agent-browser", "screenshot", img_path],
                       capture_output=True, timeout=15)

        if not os.path.exists(img_path):
            return None

        result = subprocess.run([
            "curl", "-s", "-X", "POST",
            "https://qa.service.test.sankuai.com/api/flowCopilot/oversea/file/uploadImage",
            "-F", f"file=@{img_path}"
        ], capture_output=True, text=True, timeout=30)

        url_data = json.loads(result.stdout)
        return url_data.get('data', {}).get('url')
    except Exception as e:
        print(f"[image-gen] Error: {e}")
        return None


# ─── Core Diagnosis ───────────────────────────────────────────────────────────

def run_diagnosis(shop_id: int, days: int) -> dict:
    end_dt = datetime.utcnow().date()
    start_dt = end_dt - timedelta(days=days - 1)
    start_str = start_dt.strftime('%Y-%m-%d')
    end_str = end_dt.strftime('%Y-%m-%d')

    order_sql = (
        f"SELECT shop_id, shop_name, shop_order_status, shop_income, shop_review_score,"
        f" meal_prep_dura_minutes, ord_estimated_delivery_dura, order_cancel_reason_id"
        f" FROM mart_sailor_global.app_shop_order_analysis_d"
        f" WHERE region = 'BR' AND shop_id = {shop_id}"
        f" AND dt >= '{start_str}' AND dt <= '{end_str}' LIMIT 500"
    )
    spu_sql = (
        f"SELECT shop_id, COUNT(DISTINCT spu_id) AS spu_count,"
        f" SUM(CASE WHEN is_popular_spu=1 THEN 1 ELSE 0 END) AS hot_spu_count"
        f" FROM mart_sailor_global.aggr_product_spu_info_d"
        f" WHERE region='BR' AND shop_id={shop_id} AND dt='{end_str}' GROUP BY shop_id"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        order_fut = pool.submit(hive_query, order_sql)
        spu_fut = pool.submit(hive_query, spu_sql)
        order_rows = order_fut.result(timeout=150)
        spu_rows = spu_fut.result(timeout=150)

    if not order_rows:
        raise HTTPException(status_code=404,
                            detail=f"No data for shop_id={shop_id} in last {days} days")

    shop_name = None
    total_income = 0.0
    prep_times, delivery_times, ratings = [], [], []
    cancel_count = 0
    total_orders = len(order_rows)

    for row in order_rows:
        try:
            if shop_name is None and row[1]:
                shop_name = str(row[1])
            total_income += float(row[3]) if row[3] else 0
            rating = float(row[4]) if row[4] else None
            if rating:
                ratings.append(rating)
            prep = float(row[5]) if row[5] else None
            if prep and prep > 0:
                prep_times.append(prep)
            delv = float(row[6]) if row[6] else None
            if delv and delv > 0:
                delivery_times.append(delv)
            cr = row[7]
            if cr and str(cr) not in ('0', 'None', '', 'null'):
                cancel_count += 1
        except (IndexError, TypeError, ValueError):
            continue

    if shop_name is None:
        shop_name = f"Shop #{shop_id}"

    metrics = {
        "avg_prep_min": round(sum(prep_times) / len(prep_times) if prep_times else 0, 1),
        "avg_delivery_min": round(sum(delivery_times) / len(delivery_times) if delivery_times else 0, 1),
        "cancel_rate": round(cancel_count / total_orders * 100 if total_orders > 0 else 0, 1),
        "avg_rating": round(sum(ratings) / len(ratings) if ratings else 0, 2),
        "spu_count": 0,
        "hot_spu_count": 0,
        "total_orders": total_orders,
        "total_income": round(total_income, 2),
        "days": days,
    }

    if spu_rows:
        try:
            metrics["spu_count"] = int(spu_rows[0][1]) if spu_rows[0][1] else 0
            metrics["hot_spu_count"] = int(spu_rows[0][2]) if spu_rows[0][2] else 0
        except (IndexError, TypeError, ValueError):
            pass

    health_score, deductions = calc_health_score(metrics)
    level_info = get_health_level(health_score)
    diagnosis_items = build_diagnosis_items(metrics)

    shop_data = {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "health_score": health_score,
        "health_level": level_info['level'],
        "health_color": level_info['color'],
        "health_emoji": level_info['emoji'],
        "metrics": metrics,
        "deductions": deductions,
        "diagnosis": diagnosis_items,
        "report_url": f"https://zhangwenle2026.github.io/shop-diagnosis/?shop_id={shop_id}",
        "image_url": None,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    shop_data["image_url"] = generate_report_image(shop_data)
    return shop_data


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "store-diagnosis-api", "version": "1.0.0"}


@app.get("/diagnose")
async def diagnose(shop_id: int, days: int = 7):
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be 1-30")
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, run_diagnosis, shop_id, days),
            timeout=120
        )
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Hive query timed out. Retry.")
    except Exception as e:
        msg = str(e)
        code = 404 if "No data" in msg else 503
        raise HTTPException(status_code=code, detail=f"Diagnosis failed: {msg}")


class BotDiagnoseRequest(BaseModel):
    shop_id: int
    requester: Optional[str] = "unknown"
    days: Optional[int] = 7


@app.post("/bot/diagnose")
async def bot_diagnose(req: BotDiagnoseRequest):
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, run_diagnosis, req.shop_id, req.days or 7),
            timeout=120
        )
        return {
            "image_url": result.get("image_url"),
            "report_url": result.get("report_url"),
            "shop_name": result.get("shop_name"),
            "health_score": result.get("health_score"),
            "health_level": result.get("health_level"),
            "health_emoji": result.get("health_emoji"),
            "requester": req.requester,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
