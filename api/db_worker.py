"""
DB Worker - polls diagnosis_requests table, runs gen_shop_report.py, writes results back
Runs every 30s via cron or as a daemon
"""
import json, os, subprocess, sys, time, requests
from datetime import datetime, timedelta
from pathlib import Path

SUPABASE_URL = "https://dbcli-7zafhdhzxmshpg1s.database.sankuai.com"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlIiwiaWF0IjoxNzQ2OTc5MjAwLCJleHAiOjE5MDQ3NDU2MDB9.xT9igTCfzpypACeEKlIgzp5AHNlSmPRa1fJBDeZPing"
HEADERS = {
    "apikey": ANON_KEY,
    "Authorization": f"Bearer {ANON_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

SCRIPT = Path("/root/.openclaw/workspace/shop-diagnosis/gen_shop_report.py")
OUTPUT_DIR = Path("/tmp/files")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_URL = "https://qa.service.test.sankuai.com/api/flowCopilot/oversea/file/uploadImage"

LOG = Path("/tmp/db_worker.log")

def log(msg):
    ts = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S BRT")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    # keep log under 300 lines
    lines = LOG.read_text().splitlines()
    if len(lines) > 300:
        LOG.write_text("\n".join(lines[-200:]) + "\n")

def rest(method, path, **kwargs):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    r = requests.request(method, url, headers=HEADERS, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json() if r.text else {}

def fetch_pending():
    rows = rest("GET", "diagnosis_requests",
                params={"status": "eq.pending", "order": "created_at.asc", "limit": "3"})
    return rows if isinstance(rows, list) else []

def set_status(row_id, status, extra=None):
    data = {"status": status, "updated_at": "now()"}
    if extra:
        data.update(extra)
    rest("PATCH", "diagnosis_requests",
         params={"id": f"eq.{row_id}"},
         json=data)

def upload_image(img_path):
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", UPLOAD_URL, "-F", f"file=@{img_path}"],
        capture_output=True, text=True, timeout=30
    )
    try:
        return json.loads(r.stdout).get("data", {}).get("url")
    except Exception:
        return None

def build_diagnosis(metrics: dict) -> dict:
    """Build health score + diagnosis rows from raw metrics dict."""
    BENCHMARKS = {
        "aov_brl":          {"label": "Ticket Medio",         "good": 35.0,  "weight": 15, "unit": "R$",  "lower": False, "category": "Financeiro"},
        "cancel_rate":      {"label": "Taxa de Cancelamento", "good": 3.0,   "weight": 20, "unit": "%",   "lower": True,  "category": "Operacoes"},
        "avg_rating":       {"label": "Avaliacao",            "good": 4.5,   "weight": 25, "unit": "star","lower": False, "category": "Qualidade"},
        "avg_delivery_min": {"label": "Tempo de Entrega",     "good": 35.0,  "weight": 20, "unit": "min", "lower": True,  "category": "Entrega"},
        "spu_img_pct":      {"label": "SPU c/ Imagem",        "good": 80.0,  "weight": 10, "unit": "%",   "lower": False, "category": "Cardapio"},
        "active_spus":      {"label": "Produtos Ativos",      "good": 20.0,  "weight": 10, "unit": "num", "lower": False, "category": "Cardapio"},
    }
    TIPS = {
        "aov_brl":          "Crie combos e adicione itens premium ao cardapio.",
        "cancel_rate":      "Confirme pedidos rapidamente e mantenha estoque atualizado.",
        "avg_rating":       "Responda avaliacoes negativas e melhore o servico.",
        "avg_delivery_min": "Otimize preparo e coordene com entregadores.",
        "spu_img_pct":      "Adicione fotos de qualidade a todos os produtos.",
        "active_spus":      "Diversifique o cardapio com mais opcoes.",
    }
    def fmt(key, val):
        if val is None: return "--"
        n = float(val)
        u = BENCHMARKS[key]["unit"]
        if u == "R$":   return f"R${n:,.2f}"
        if u == "%":    return f"{n:.1f}%"
        if u == "star": return f"{n:.2f} *"
        if u == "min":  return f"{round(n)} min"
        return str(int(n))

    earned = 0; total_w = 0
    diagnosis = []; deductions = []
    for key, cfg in BENCHMARKS.items():
        val = metrics.get(key)
        if val is None: continue
        n = float(val); bm = cfg["good"]; w = cfg["weight"]; lower = cfg["lower"]
        ratio = min(bm / max(n, 0.01), 1.5) if lower else min(n / max(bm, 0.01), 1.5)
        score = min(int(ratio * w), w)
        earned += score; total_w += w
        pct = (score / w) * 100
        status = "ok" if pct >= 75 else ("warn" if pct >= 50 else "bad")
        pts_lost = w - score
        if pts_lost > 0:
            deductions.append({"pts": pts_lost, "label": cfg["label"]})
        diagnosis.append({
            "metric": cfg["label"], "value": fmt(key, val),
            "benchmark": fmt(key, bm), "status": status,
            "category": cfg["category"],
            "suggestion": TIPS[key] if status != "ok" else "",
        })
    health = max(0, min(100, int((earned / max(total_w, 1)) * 100)))
    if health >= 90:   level, color, emoji = "Excelente", "#22c55e", "trophy"
    elif health >= 75: level, color, emoji = "Bom",       "#3b82f6", "+1"
    elif health >= 60: level, color, emoji = "Regular",   "#f59e0b", "chart"
    else:              level, color, emoji = "Critico",   "#ef4444", "alert"
    deductions.sort(key=lambda x: -x["pts"])
    return {
        "health_score": health, "health_level": level,
        "health_color": color, "health_emoji": emoji,
        "deductions": deductions[:4], "diagnosis": diagnosis,
    }


def process_request(row):
    row_id = row["id"]
    shop_id = row["shop_id"]
    # 兼容两种方式：DB列（旧）或 metrics JSON里的 req_start/req_end（新）
    start_date = row.get("start_date")
    end_date = row.get("end_date")
    if not start_date or not end_date:
        try:
            pre_metrics = json.loads(row.get("metrics") or "{}")
            start_date = start_date or pre_metrics.get("req_start")
            end_date   = end_date   or pre_metrics.get("req_end")
        except Exception:
            pass
    log(f"Processing shop_id={shop_id} (id={row_id}) [{start_date} -> {end_date}]")

    # mark as processing
    set_status(row_id, "processing")

    img_path = OUTPUT_DIR / f"shop_report_{shop_id}.jpg"
    json_path = OUTPUT_DIR / f"shop_report_{shop_id}.json"

    # build command
    cmd = [sys.executable, str(SCRIPT), "--shop_id", str(shop_id), "--lang", "pt"]
    if start_date and end_date:
        cmd += ["--start", str(start_date), "--end", str(end_date)]
    else:
        cmd += ["--days", "7"]

    # run gen_shop_report.py
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=120,
        cwd=str(SCRIPT.parent)
    )

    if r.returncode != 0 or not img_path.exists():
        err = (r.stderr or r.stdout or "unknown error")[:300]
        log(f"  ERROR: {err}")
        set_status(row_id, "error", {"error_msg": err})
        return

    # upload image
    img_url = upload_image(img_path)
    if not img_url:
        log("  WARNING: upload failed, img_url is None")

    # read metrics from json
    metrics = {}
    if json_path.exists():
        try:
            metrics = json.loads(json_path.read_text())
        except Exception:
            pass

    shop_name = metrics.pop("shop_name", f"Shop #{shop_id}")

    # Build health score + diagnosis and merge into metrics
    health_info = build_diagnosis(metrics)
    metrics.update(health_info)
    # preserve date range info
    if start_date: metrics["start_date"] = start_date
    if end_date:   metrics["end_date"] = end_date

    set_status(row_id, "done", {
        "img_url": img_url,
        "shop_name": shop_name,
        "metrics": json.dumps(metrics),
    })
    log(f"  Done: {shop_name} -> {img_url}")

def main():
    log("DB Worker started")
    pending = fetch_pending()
    if not pending:
        log("No pending requests")
        return
    for row in pending:
        try:
            process_request(row)
        except Exception as e:
            log(f"  EXCEPTION for id={row.get('id')}: {e}")
            try:
                set_status(row["id"], "error", {"error_msg": str(e)[:300]})
            except Exception:
                pass

if __name__ == "__main__":
    main()
