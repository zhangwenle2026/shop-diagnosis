#!/usr/bin/env python3
"""
gen_shop_report.py — Shop Health Diagnosis Report Generator
Usage:
  python3 gen_shop_report.py --shop_id 159409277
  python3 gen_shop_report.py --shop_id 159409277 --period this_month
  python3 gen_shop_report.py --shop_id 159409277 --lang en

Output:
  /tmp/files/shop_report_{shop_id}.jpg
  /tmp/files/shop_report_{shop_id}.json
"""

import argparse, json, os, subprocess, sys, tempfile, time, math
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTPUT_DIR = Path("/tmp/files")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BRT = timezone(timedelta(hours=-3))
BI_SCRIPT = os.path.expanduser("~/.openclaw/skills/bi-query-sql/scripts/mt-mcp-client.js")


# ─── Hive SQL Helper (fixed: status is in data.status) ───────────────────────

def hive_query(sql: str, group: str = "keeta-br-b-ops", timeout: int = 60) -> list:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False, encoding='utf-8') as f:
        f.write(sql)
        sql_file = f.name
    try:
        # Submit
        r = subprocess.run(
            ["node", BI_SCRIPT, "submit", "--server", "fra_hive",
             "--sql-file", sql_file, "--group", group],
            capture_output=True, text=True, timeout=30
        )
        qid = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("code") == 1 and obj.get("data"):
                    d = obj["data"]
                    if isinstance(d, str) and len(d) > 10:
                        qid = d
                        break
            except Exception:
                pass
        if not qid:
            print(f"  ⚠️ No QID. Submit output: {r.stdout[:300]}", file=sys.stderr)
            return []

        # Poll — status 在 data.status，不是顶层
        for _ in range(timeout // 3):
            time.sleep(3)
            rq = subprocess.run(
                ["node", BI_SCRIPT, "query-info", "--server", "fra_hive", "--qid", qid],
                capture_output=True, text=True, timeout=15
            )
            try:
                info = json.loads(rq.stdout)
                # 正确路径：info["data"]["status"]
                status = ""
                if isinstance(info.get("data"), dict):
                    status = info["data"].get("status", "").upper()
                elif isinstance(info.get("status"), str):
                    status = info["status"].upper()
                if status in ("SUCCEEDED", "FINISHED"):
                    break
                elif status in ("FAILED", "CANCELLED", "ERROR"):
                    print(f"  ⚠️ Query {status}", file=sys.stderr)
                    return []
            except Exception:
                pass

        # Fetch result
        rr = subprocess.run(
            ["node", BI_SCRIPT, "query-result", "--server", "fra_hive",
             "--qid", qid, "--offset", "0", "--length", "1000"],
            capture_output=True, text=True, timeout=30
        )
        try:
            result = json.loads(rr.stdout)
            # 结构: {"code":1,"data":{"data":"{\"data\":{\"data\":[[...]]}}","length":N}}
            inner = result.get("data", {})
            if isinstance(inner.get("data"), str):
                inner2 = json.loads(inner["data"])
                rows = inner2["data"]["data"]
                return rows  # list of lists
            # fallback
            for key in ("rows", "data", "results", "records"):
                if isinstance(result.get(key), list):
                    return result[key]
        except Exception as e:
            print(f"  ⚠️ Result parse error: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  ⚠️ hive_query exception: {e}", file=sys.stderr)
        return []
    finally:
        try:
            os.unlink(sql_file)
        except Exception:
            pass


# ─── Date helpers ─────────────────────────────────────────────────────────────

def get_date_range(period=None, days=None, start=None, end=None):
    today = datetime.now(BRT).date()
    yesterday = today - timedelta(days=1)
    if start and end:
        return str(start), str(end)
    if period == "this_month":
        return str(today.replace(day=1)), str(yesterday)
    if period == "this_week":
        return str(today - timedelta(days=today.weekday())), str(yesterday)
    if period == "last_30":
        return str(today - timedelta(days=30)), str(yesterday)
    n = int(days) if days else 7
    return str(today - timedelta(days=n)), str(yesterday)

def fmt_dt(d: str) -> str:
    return d.replace("-", "")


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_shop_data(shop_id: int, start_date: str, end_date: str) -> dict:
    sd, ed = fmt_dt(start_date), fmt_dt(end_date)
    print(f"  📡 Fetching shop {shop_id} ({start_date} → {end_date})")

    order_sql = f"""
SELECT
    shop_id,
    MAX(shop_name)                                                              AS shop_name,
    COUNT(CASE WHEN shop_order_status NOT IN (55,57) THEN 1 END)               AS total_orders,
    COUNT(CASE WHEN shop_order_status = 55 THEN 1 END)                         AS cancel_orders,
    SUM(CASE WHEN shop_order_status NOT IN (55,57) THEN CAST(shop_income AS DOUBLE) ELSE 0 END) / 100.0  AS gmv_brl,
    AVG(CASE WHEN shop_order_status NOT IN (55,57) AND CAST(shop_income AS DOUBLE) > 0
             THEN CAST(shop_income AS DOUBLE) END) / 100.0                     AS aov_brl,
    AVG(CASE WHEN shop_order_status NOT IN (55,57)
             AND CAST(ord_estimated_delivery_dura AS DOUBLE) > 0
             THEN CAST(ord_estimated_delivery_dura AS DOUBLE) END) / 60000.0   AS avg_delivery_min,
    AVG(CASE WHEN shop_order_status NOT IN (55,57)
             AND CAST(shop_review_score AS DOUBLE) > 0
             THEN CAST(shop_review_score AS DOUBLE) END)                       AS avg_rating
FROM mart_sailor_global.app_shop_order_analysis_d
WHERE region = 'BR'
  AND shop_id = {shop_id}
  AND dt >= '{sd}' AND dt <= '{ed}'
GROUP BY shop_id
"""
    spu_sql = f"""
SELECT
    shop_id,
    COUNT(DISTINCT spu_id)                          AS active_spus,
    SUM(CASE WHEN is_popular_spu = 1 THEN 1 ELSE 0 END) AS hot_spus
FROM mart_sailor_global.aggr_product_spu_info_d
WHERE region = 'BR'
  AND shop_id = {shop_id}
  AND dt = '{ed}'
GROUP BY shop_id
"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_order = pool.submit(hive_query, order_sql)
        f_spu   = pool.submit(hive_query, spu_sql)
        order_rows = f_order.result()
        spu_rows   = f_spu.result()

    def row_to_dict(rows, keys):
        if not rows:
            return {}
        r = rows[0]
        if isinstance(r, dict):
            return r
        return dict(zip(keys, r))

    order = row_to_dict(order_rows, ["shop_id","shop_name","total_orders","cancel_orders",
                                      "gmv_brl","aov_brl","avg_delivery_min","avg_rating"])
    spu   = row_to_dict(spu_rows,   ["shop_id","active_spus","hot_spus"])

    if not order:
        return {}

    total_orders  = int(order.get("total_orders") or 0)
    cancel_orders = int(order.get("cancel_orders") or 0)
    def safe_float(v):
        if v is None or v == 'NULL' or v == '': return 0.0
        try: return float(v)
        except: return 0.0
    gmv    = safe_float(order.get("gmv_brl"))
    aov    = safe_float(order.get("aov_brl"))
    cancel_rate = (cancel_orders / max(total_orders + cancel_orders, 1)) * 100
    active_spus = int(spu.get("active_spus") or 0)
    hot_spus    = int(spu.get("hot_spus") or 0)
    spu_img_pct = (hot_spus / max(active_spus, 1)) * 100 if active_spus else 0
    avg_rating   = safe_float(order.get("avg_rating"))
    avg_delivery = safe_float(order.get("avg_delivery_min"))

    return {
        "shop_id": shop_id,
        "shop_name": order.get("shop_name") or f"Shop #{shop_id}",
        "start_date": start_date, "end_date": end_date,
        "total_orders": total_orders,
        "gmv_brl": round(gmv, 2),
        "aov_brl": round(aov, 2),
        "cancel_rate": round(cancel_rate, 1),
        "avg_rating": round(avg_rating, 2) if avg_rating else None,
        "avg_delivery_min": round(avg_delivery, 1) if avg_delivery else None,
        "active_spus": active_spus,
        "spu_img_pct": round(spu_img_pct, 1),
        "visit_cvr": 0.0,
        "order_cvr": 0.0,
        "new_user_pct": 0.0,
        "repurchase_rate": 0.0,
    }


# ─── Health Score ─────────────────────────────────────────────────────────────

BENCHMARKS = {
    "aov_brl":        {"good": 35.0,  "weight": 15},
    "cancel_rate":    {"good": 3.0,   "weight": 20, "lower_is_better": True},
    "avg_rating":     {"good": 4.5,   "weight": 25},
    "avg_delivery_min":{"good": 35.0, "weight": 20, "lower_is_better": True},
    "spu_img_pct":    {"good": 80.0,  "weight": 10},
    "active_spus":    {"good": 20.0,  "weight": 10},
}

def calc_health_score(data: dict) -> dict:
    earned = 0; total_weight = 0
    metrics_scored = {}; improvements = []; strengths = []
    for key, cfg in BENCHMARKS.items():
        val = data.get(key)
        if val is None:
            continue
        w = cfg["weight"]
        bm = cfg["good"]
        lower = cfg.get("lower_is_better", False)
        ratio = min(bm / max(val, 0.01), 1.5) if lower else min(val / max(bm, 0.01), 1.5)
        score = min(int(ratio * w), w)
        earned += score; total_weight += w
        pct = (score / w) * 100
        metrics_scored[key] = {"score": score, "max": w, "val": val, "benchmark": bm}
        if pct >= 75:
            strengths.append(key)
        elif pct < 50:
            improvements.append(key)

    health = max(0, min(100, int((earned / max(total_weight, 1)) * 100)))
    if health >= 90:   grade, gc = "Excelente", "#22c55e"
    elif health >= 75: grade, gc = "Bom", "#3b82f6"
    elif health >= 60: grade, gc = "Regular", "#f59e0b"
    else:              grade, gc = "Crítico", "#ef4444"
    return {"score": health, "grade": grade, "grade_color": gc,
            "metrics": metrics_scored, "improvements": improvements, "strengths": strengths}


# ─── HTML Report ──────────────────────────────────────────────────────────────

METRIC_LABELS = {
    "aov_brl":         ("Ticket Médio / AOV"),
    "cancel_rate":     ("Taxa de Cancelamento / Cancel Rate"),
    "avg_rating":      ("Avaliação / Rating"),
    "avg_delivery_min":("Tempo de Entrega / Delivery Time"),
    "spu_img_pct":     ("SPU c/ Imagem / SPU w/Image"),
    "active_spus":     ("Produtos Ativos / Active SKUs"),
}

TIPS = {
    "aov_brl":         "Crie combos e adicione itens premium ao cardápio.",
    "cancel_rate":     "Confirme pedidos rapidamente e mantenha estoque atualizado.",
    "avg_rating":      "Responda avaliações negativas e melhore o serviço.",
    "avg_delivery_min":"Otimize preparo e coordene com entregadores.",
    "spu_img_pct":     "Adicione fotos de qualidade a todos os produtos.",
    "active_spus":     "Diversifique o cardápio com mais opções.",
}

def fmt_val(key, val):
    if val is None: return "--"
    if key == "aov_brl": return f"R${val:,.2f}"
    if key == "cancel_rate": return f"{val:.1f}%"
    if key == "avg_rating": return f"{val:.2f} ★"
    if key == "avg_delivery_min": return f"{val:.0f} min"
    if key == "spu_img_pct": return f"{val:.0f}%"
    if key == "active_spus": return str(int(val))
    return str(val)

def build_html(data: dict, health: dict) -> str:
    now_str = datetime.now(BRT).strftime("%Y-%m-%d %H:%M BRT")
    score = health["score"]
    grade = health["grade"]
    gc = health["grade_color"]

    # Gauge SVG
    pct = score / 100
    angle = pct * 180
    rad = math.radians(180 - angle)
    nx = 80 + 70 * math.cos(rad)
    ny = 85 - 70 * math.sin(rad)
    gauge = f"""<svg width="160" height="100" viewBox="0 0 160 100">
  <path d="M10,85 A70,70 0 0,1 150,85" fill="none" stroke="#1e3050" stroke-width="14" stroke-linecap="round"/>
  <path d="M10,85 A70,70 0 0,1 {nx:.1f},{ny:.1f}" fill="none" stroke="{gc}" stroke-width="14" stroke-linecap="round"/>
  <text x="80" y="80" text-anchor="middle" font-size="30" font-weight="900" fill="{gc}">{score}</text>
  <text x="80" y="96" text-anchor="middle" font-size="11" fill="#94a3b8">{grade}</text>
</svg>"""

    # Metric rows
    rows_html = ""
    for key, cfg in BENCHMARKS.items():
        m = health["metrics"].get(key)
        val_str = fmt_val(key, m["val"] if m else None)
        bm_str  = fmt_val(key, cfg["good"])
        bar_w   = min(int((m["score"]/m["max"])*100), 100) if m else 0
        bar_color = "#22c55e" if bar_w >= 75 else ("#f59e0b" if bar_w >= 50 else "#ef4444")
        rows_html += f"""<tr>
      <td class="mn">{METRIC_LABELS[key]}</td>
      <td class="mv">{val_str}</td>
      <td class="mb">{bm_str}</td>
      <td class="ms"><div class="bw"><div class="bb" style="width:{bar_w}%;background:{bar_color}"></div></div><span style="color:{bar_color};font-size:10px">{bar_w}</span></td>
    </tr>"""

    # Actions
    def action(key, good):
        color = "#22c55e" if good else "#ef4444"
        icon = "✅" if good else "⚠️"
        tip = TIPS.get(key, "")
        label = METRIC_LABELS.get(key, key)
        return f'<div style="border-left:3px solid {color};padding:8px 10px;margin:5px 0;background:{color}18;border-radius:6px"><div style="font-weight:700;color:{color};font-size:11px">{icon} {label}</div><div style="font-size:10px;color:#94a3b8;margin-top:3px">{tip}</div></div>'

    imp_html = "".join(action(k, False) for k in health["improvements"]) or '<div style="font-size:11px;color:#5a7aaa">Nenhum crítico</div>'
    str_html = "".join(action(k, True)  for k in health["strengths"])   or '<div style="font-size:11px;color:#5a7aaa">—</div>'

    days = (datetime.strptime(data["end_date"], "%Y-%m-%d") - datetime.strptime(data["start_date"], "%Y-%m-%d")).days + 1

    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0f1e;color:#e2e8f0;width:420px;padding:16px}}
.card{{background:linear-gradient(135deg,#1a2035,#1e2a45);border:1px solid #2a3a5c;border-radius:14px;padding:14px;margin-bottom:12px}}
.st{{font-size:10px;font-weight:700;color:#7eb3ff;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}}
.ig{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.il .lbl{{font-size:9px;color:#5a7aaa;text-transform:uppercase}}
.il .val{{font-size:13px;font-weight:600;color:#e2e8f0;margin-top:2px}}
.krow{{display:flex;gap:8px;margin-top:10px}}
.kb{{flex:1;background:#0d1a2e;border-radius:8px;padding:8px;text-align:center}}
.kb .kv{{font-size:17px;font-weight:800;color:#7eb3ff}}
.kb .kl{{font-size:9px;color:#5a7aaa;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{color:#5a7aaa;font-size:9px;text-transform:uppercase;padding:4px 6px;text-align:left;border-bottom:1px solid #2a3a5c}}
td{{padding:5px 6px;border-bottom:1px solid #111827;vertical-align:middle}}
.mn{{color:#94a3b8;font-weight:500;font-size:10px}}
.mv{{color:#e2e8f0;font-weight:700}}
.mb{{color:#5a7aaa}}
.ms{{width:80px}}
.bw{{background:#1e3050;border-radius:3px;height:4px;margin-bottom:2px;overflow:hidden}}
.bb{{height:4px;border-radius:3px}}
.footer{{text-align:center;font-size:9px;color:#374151;padding-top:8px}}
</style></head><body>

<div style="text-align:center;margin-bottom:14px">
  <div style="font-size:11px;color:#5a7aaa;margin-bottom:2px">Keeta · Diagnóstico de Loja</div>
  <div style="font-size:20px;font-weight:900;color:#7eb3ff">{data['shop_name']}</div>
  <div style="font-size:11px;color:#5a7aaa;margin-top:2px">ID {data['shop_id']} · {data['start_date']} → {data['end_date']} ({days}d)</div>
</div>

<div class="card">
  <div class="st">Informações da Loja</div>
  <div class="ig">
    <div class="il"><div class="lbl">Shop ID</div><div class="val">{data['shop_id']}</div></div>
    <div class="il"><div class="lbl">Pedidos</div><div class="val">{data['total_orders']}</div></div>
    <div class="il"><div class="lbl">GMV</div><div class="val">R${data['gmv_brl']:,.0f}</div></div>
    <div class="il"><div class="lbl">Ticket Médio</div><div class="val">R${data['aov_brl']:.2f}</div></div>
  </div>
</div>

<div class="card">
  <div class="st">Pontuação de Saúde</div>
  <div style="display:flex;flex-direction:column;align-items:center">{gauge}</div>
  <div class="krow">
    <div class="kb"><div class="kv">{data['cancel_rate']:.1f}%</div><div class="kl">Cancelamento</div></div>
    <div class="kb"><div class="kv">{data['avg_rating'] or '--'}</div><div class="kl">Avaliação</div></div>
    <div class="kb"><div class="kv">{f"{data['avg_delivery_min']:.0f}min" if data['avg_delivery_min'] else '--'}</div><div class="kl">Entrega</div></div>
  </div>
</div>

<div class="card">
  <div class="st">Scorecard de Métricas</div>
  <table>
    <thead><tr><th>Métrica</th><th>Real</th><th>Referência</th><th>Score</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<div class="card">
  <div class="st">Diagnóstico & Plano de Ação</div>
  <div style="margin-bottom:10px">
    <div style="font-size:10px;font-weight:700;color:#ef4444;margin-bottom:5px">⚠️ Áreas para Melhorar</div>
    {imp_html}
  </div>
  <div>
    <div style="font-size:10px;font-weight:700;color:#22c55e;margin-bottom:5px">✅ Pontos Fortes</div>
    {str_html}
  </div>
</div>

<div class="footer">Fonte: Keeta Hive SQL · Gerado em {now_str}</div>
</body></html>"""


# ─── Screenshot via CDP ───────────────────────────────────────────────────────

def screenshot_html(shop_id: int, html: str) -> Path:
    html_path = OUTPUT_DIR / f"shop_report_{shop_id}.html"
    img_path  = OUTPUT_DIR / f"shop_report_{shop_id}.jpg"
    html_path.write_text(html, encoding="utf-8")

    script = f"""
import asyncio, base64, json
from pathlib import Path
import websockets

async def snap():
    import urllib.request
    tabs = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
    # 找或新建 tab
    tab = None
    for t in tabs:
        if t.get('type') == 'page':
            tab = t
            break
    if not tab:
        new = json.loads(urllib.request.urlopen('http://localhost:9222/json/new').read())
        tab = new

    ws_url = tab['webSocketDebuggerUrl']
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        _id = 0
        async def send(method, params=None):
            nonlocal _id
            _id += 1
            await ws.send(json.dumps({{"id":_id,"method":method,"params":params or {{}}}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if msg.get('id') == _id:
                    return msg

        await send('Page.enable')
        file_url = 'file://{html_path}'
        await send('Page.navigate', {{'url': file_url}})
        await asyncio.sleep(2)
        # set viewport
        await send('Emulation.setDeviceMetricsOverride', {{'width':420,'height':900,'deviceScaleFactor':2,'mobile':False}})
        await asyncio.sleep(0.5)
        # get full height
        r = await send('Runtime.evaluate', {{'expression':'document.body.scrollHeight'}})
        height = r.get('result',{{}}).get('result',{{}}).get('value', 900)
        await send('Emulation.setDeviceMetricsOverride', {{'width':420,'height':height,'deviceScaleFactor':2,'mobile':False}})
        await asyncio.sleep(0.3)
        r = await send('Page.captureScreenshot', {{'format':'jpeg','quality':88}})
        data = r['result']['data']
        Path('{img_path}').write_bytes(base64.b64decode(data))
        print('OK', len(base64.b64decode(data)))

asyncio.run(snap())
"""
    r = subprocess.run(["python3", "-c", script], capture_output=True, text=True, timeout=30)
    if img_path.exists() and img_path.stat().st_size > 5000:
        print(f"  ✅ Screenshot: {img_path} ({img_path.stat().st_size//1024}KB)")
        return img_path
    print(f"  ⚠️ CDP screenshot failed: {r.stderr[:300]}", file=sys.stderr)
    # fallback: chromium headless
    r2 = subprocess.run([
        "chromium-browser", "--headless", "--no-sandbox", "--disable-gpu",
        f"--screenshot={img_path}",
        "--window-size=420,900",
        f"file://{html_path}"
    ], capture_output=True, text=True, timeout=30)
    if img_path.exists():
        print(f"  ✅ Chromium screenshot: {img_path} ({img_path.stat().st_size//1024}KB)")
        return img_path
    print(f"  ⚠️ Chromium fallback failed: {r2.stderr[:200]}", file=sys.stderr)
    return None


# ─── Upload ───────────────────────────────────────────────────────────────────

def upload_image(img_path: Path) -> str:
    r = subprocess.run([
        "curl", "-s", "-X", "POST",
        "https://qa.service.test.sankuai.com/api/flowCopilot/oversea/file/uploadImage",
        "-F", f"file=@{img_path}"
    ], capture_output=True, text=True, timeout=30)
    try:
        resp = json.loads(r.stdout)
        url = resp.get("data", {}).get("url") or resp.get("url")
        if url:
            print(f"  📤 Uploaded: {url}")
            return url
    except Exception:
        pass
    print(f"  ⚠️ Upload failed: {r.stdout[:200]}", file=sys.stderr)
    return None


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop_id", type=int, required=True)
    parser.add_argument("--period", choices=["this_month","this_week","last_7","last_30"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--lang", choices=["pt","en","zh"], default="pt")
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()

    start_date, end_date = get_date_range(args.period, args.days, args.start, args.end)

    print(f"\n🏥 Shop Diagnosis | ID:{args.shop_id} | {start_date}→{end_date}")

    data = fetch_shop_data(args.shop_id, start_date, end_date)
    # 如果最近7天无数据，自动回退到有数据的最近时间窗口（最多往前找60天）
    if not data:
        print(f"  ⚠️  No data in [{start_date}→{end_date}], looking for latest available window...")
        latest_sql = f"""SELECT MAX(dt) as latest_dt
FROM mart_sailor_global.app_shop_order_analysis_d
WHERE region = 'BR' AND shop_id = {args.shop_id}"""
        latest_rows = hive_query(latest_sql)
        latest_dt = latest_rows[0][0] if latest_rows and latest_rows[0][0] else None
        if latest_dt:
            import datetime as _dt
            latest = _dt.datetime.strptime(str(latest_dt), "%Y%m%d").date()
            fallback_end = latest.strftime("%Y-%m-%d")
            fallback_start = (latest - _dt.timedelta(days=6)).strftime("%Y-%m-%d")
            print(f"  🔄 Retrying with [{fallback_start}→{fallback_end}]")
            data = fetch_shop_data(args.shop_id, fallback_start, fallback_end)
            if data:
                start_date, end_date = fallback_start, fallback_end
    if not data:
        print(f"❌ Shop {args.shop_id} not found or no data.", file=sys.stderr)
        sys.exit(1)

    json_path = OUTPUT_DIR / f"shop_report_{args.shop_id}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    health = calc_health_score(data)
    print(f"  🏆 Score: {health['score']} ({health['grade']})")

    html = build_html(data, health)
    img_path = screenshot_html(args.shop_id, html)
    if not img_path:
        print("❌ Render failed", file=sys.stderr); sys.exit(3)

    if args.upload:
        url = upload_image(img_path)
        print(json.dumps({"shop_id": args.shop_id, "shop_name": data["shop_name"],
                          "score": health["score"], "grade": health["grade"],
                          "img_url": url, "img_path": str(img_path)}))
    else:
        print(f"\n✅ Done: {img_path}")

if __name__ == "__main__":
    main()
