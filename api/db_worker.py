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

def process_request(row):
    row_id = row["id"]
    shop_id = row["shop_id"]
    log(f"Processing shop_id={shop_id} (id={row_id})")

    # mark as processing
    set_status(row_id, "processing")

    img_path = OUTPUT_DIR / f"shop_report_{shop_id}.jpg"
    json_path = OUTPUT_DIR / f"shop_report_{shop_id}.json"

    # run gen_shop_report.py
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--shop_id", str(shop_id), "--days", "7", "--lang", "zh"],
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
