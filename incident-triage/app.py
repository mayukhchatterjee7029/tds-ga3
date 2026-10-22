import os, json, time, socket, ssl, sqlite3, uuid, datetime, threading
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional

DB = os.getenv("DB_PATH", "incidents.db")
API_KEY = os.getenv("API_KEY", "")                 # optional bearer token
ACTION_WEBHOOK = os.getenv("ACTION_WEBHOOK_URL", "")  # real executor for restart/rollback/scale
NOTIFY_WEBHOOK = os.getenv("NOTIFY_WEBHOOK_URL", "")  # Slack/Discord/etc.
LOCK = threading.Lock()
app = FastAPI(title="Incident Agent")

def db():
    c = sqlite3.connect(DB, check_same_thread=False); c.row_factory = sqlite3.Row; return c
C = db()
C.executescript("""
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, created TEXT, request TEXT, status TEXT,
  cause TEXT, decision TEXT, approval TEXT, action_result TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, seq INT,
  ts TEXT, kind TEXT, name TEXT, ok INT, duration_ms REAL, detail TEXT);
""")
now = lambda: datetime.datetime.utcnow().isoformat() + "Z"

def auth(authorization: Optional[str] = Header(None)):
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "unauthorized")

class IncidentIn(BaseModel):
    id: Optional[str] = None
    title: str
    service: str = ""
    target_url: Optional[str] = None       # what to probe
    environment: str = "prod"
    severity: str = "high"
    signals: dict = {}                      # e.g. {"recent_deploy": true, "hint": "tls"}
    allowed_actions: list[str] = ["notify_oncall", "restart_service", "rollback_deploy", "scale_up"]

class ApprovalIn(BaseModel):
    decision: str            # approve | reject
    approver: str

# ---------- events / telemetry ----------
def emit(iid, kind, name, ok, dur, detail):
    with LOCK:
        seq = C.execute("SELECT COUNT(*) FROM events WHERE incident_id=?", (iid,)).fetchone()[0] + 1
        C.execute("INSERT INTO events(incident_id,seq,ts,kind,name,ok,duration_ms,detail) VALUES(?,?,?,?,?,?,?,?)",
                  (iid, seq, now(), kind, name, int(ok), round(dur, 2), json.dumps(detail))); C.commit()

# ---------- real diagnostics ----------
def d_dns(u):
    return {"addrs": sorted({a[4][0] for a in socket.getaddrinfo(u.hostname, None)})}
def d_tcp(u):
    p = u.port or (443 if u.scheme == "https" else 80)
    socket.create_connection((u.hostname, p), timeout=5).close(); return {"port": p}
def d_tls(u):
    if u.scheme != "https": return {"skipped": True, "days_left": 999}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((u.hostname, u.port or 443), timeout=5) as s, ctx.wrap_socket(s, server_hostname=u.hostname) as t:
            exp = ssl.cert_time_to_seconds(t.getpeercert()["notAfter"])
            return {"days_left": round((exp - time.time()) / 86400, 1)}
    except ssl.SSLCertVerificationError as e:
        return {"days_left": -1, "error": str(e)[:120]}
def d_http(u):
    r = httpx.get(u.geturl(), timeout=8, follow_redirects=True)
    return {"status": r.status_code, "latency_ms": round(r.elapsed.total_seconds() * 1000)}
DIAG = {"dns_lookup": d_dns, "tcp_connect": d_tcp, "tls_check": d_tls, "http_probe": d_http}
HINT_ORDER = {"dns": "dns_lookup", "tcp": "tcp_connect", "tls": "tls_check", "http": "http_probe"}

def run_diag(iid, name, u):
    t = time.time()
    try: res = DIAG[name](u); ok = True
    except Exception as e: res = {"error": f"{type(e).__name__}: {e}"[:200]}; ok = False
    emit(iid, "diagnostic", name, ok, (time.time() - t) * 1000, res)
    return {"name": name, "ok": ok, "result": res}

# ---------- pure decision logic (replayable) ----------
def assess(name, d):
    r = d["result"]
    if name == "dns_lookup" and not d["ok"]: return "dns_failure"
    if name == "tcp_connect" and not d["ok"]: return "service_unreachable"
    if name == "tls_check" and (r.get("days_left", 999) <= 0): return "tls_expired"
    if name == "http_probe":
        if not d["ok"]: return "service_unreachable"
        if r["status"] >= 500: return "http_5xx"
        if r["latency_ms"] > 3000: return "high_latency"
    return None

def plan_order(sig):
    base = ["dns_lookup", "tcp_connect", "tls_check", "http_probe"]
    h = HINT_ORDER.get(str(sig.get("hint", "")))
    return ([h] + [b for b in base if b != h]) if h else base

def decide(cause, req):
    allowed, sig = req["allowed_actions"], req["signals"]
    prod = req["environment"] == "prod"
    pref = {"dns_failure": ["notify_oncall"], "tls_expired": ["notify_oncall"],
            "service_unreachable": ["restart_service", "notify_oncall"],
            "http_5xx": ["rollback_deploy"] if sig.get("recent_deploy") else ["restart_service"],
            "high_latency": ["scale_up", "notify_oncall"], None: []}[cause]
    if cause == "http_5xx": pref += ["notify_oncall"]
    action = next((a for a in pref if a in allowed), None)
    if action is None: return {"action": "no_action", "needs_approval": False, "reason": "no permitted action for cause"}
    needs = action != "notify_oncall" and (prod or req["severity"] in ("high", "critical"))
    return {"action": action, "needs_approval": needs, "reason": f"{cause} -> {action}"}

# ---------- executors (real HTTP calls; honest when unconfigured) ----------
def execute(iid, inc, action):
    t = time.time(); body = {"incident_id": iid, "action": action, "service": inc["service"], "title": inc["title"]}
    url = NOTIFY_WEBHOOK if action == "notify_oncall" else ACTION_WEBHOOK
    if not url:
        res = {"executed": False, "reason": "executor not configured"}
    else:
        try:
            r = httpx.post(url, json=body if action != "notify_oncall" else {"text": f"[{inc['severity']}] {inc['title']} ({inc['service']}): {action}", **body}, timeout=10)
            res = {"executed": r.is_success, "http_status": r.status_code, "response": r.text[:300]}
        except Exception as e:
            res = {"executed": False, "error": str(e)[:200]}
    emit(iid, "action", action, res.get("executed", False), (time.time() - t) * 1000, res)
    return res

def investigate(iid, req):
    cause, diags = None, []
    if not req.get("target_url"):
        emit(iid, "note", "no_target_url", False, 0, {})
    else:
        u = urlparse(req["target_url"])
        for name in plan_order(req["signals"]):
            d = run_diag(iid, name, u); diags.append(d)
            cause = assess(name, d)
            if cause: break            # stop at first confirmed cause: no unnecessary diagnostics
    dec = decide(cause, req)
    emit(iid, "decision", dec["action"], True, 0, {"cause": cause, **dec})
    return cause, dec, diags

def save(iid, **kw):
    with LOCK:
        for k, v in kw.items():
            C.execute(f"UPDATE incidents SET {k}=? WHERE id=?", (json.dumps(v) if not isinstance(v, str) else v, iid))
        C.commit()

# ---------- API ----------
@app.get("/health")
def health(): return {"status": "ok", "time": now()}

@app.post("/incidents", dependencies=[Depends(auth)])
def create(body: IncidentIn):
    req = body.model_dump(); iid = req["id"] or "inc_" + uuid.uuid4().hex[:10]; req["id"] = iid
    if C.execute("SELECT 1 FROM incidents WHERE id=?", (iid,)).fetchone():
        return get(iid)                                   # idempotent
    with LOCK:
        C.execute("INSERT INTO incidents(id,created,request,status) VALUES(?,?,?,?)", (iid, now(), json.dumps(req), "investigating")); C.commit()
    emit(iid, "incident", "opened", True, 0, {"title": req["title"]})
    cause, dec, _ = investigate(iid, req)
    save(iid, cause=cause or "unknown", decision=dec)
    if dec["action"] == "no_action":
        save(iid, status="closed_no_action")
    elif dec["needs_approval"]:
        save(iid, status="awaiting_approval"); emit(iid, "approval", "requested", True, 0, {"action": dec["action"]})
    else:
        res = execute(iid, req, dec["action"]); save(iid, action_result=res, status="resolved_action_taken" if res.get("executed") else "action_failed")
    return get(iid)

@app.post("/incidents/{iid}/approval", dependencies=[Depends(auth)])
def approve(iid: str, body: ApprovalIn):
    row = C.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    if not row: raise HTTPException(404, "not found")
    if row["status"] != "awaiting_approval": raise HTTPException(409, f"status is {row['status']}, not awaiting_approval")
    if body.decision not in ("approve", "reject"): raise HTTPException(422, "decision must be approve|reject")
    save(iid, approval={"decision": body.decision, "approver": body.approver, "at": now()})
    emit(iid, "approval", body.decision, body.decision == "approve", 0, {"approver": body.approver})
    if body.decision == "reject": save(iid, status="rejected")
    else:
        req = json.loads(row["request"]); res = execute(iid, req, json.loads(row["decision"])["action"])
        save(iid, action_result=res, status="resolved_action_taken" if res.get("executed") else "action_failed")
    return get(iid)

@app.get("/incidents/{iid}")
def get(iid: str):
    r = C.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    if not r: raise HTTPException(404, "not found")
    j = lambda s: json.loads(s) if s else None
    return {"id": r["id"], "created": r["created"], "status": r["status"], "cause": r["cause"], "request": j(r["request"]),
            "decision": j(r["decision"]), "approval": j(r["approval"]), "action_result": j(r["action_result"])}

@app.get("/incidents")
def lst(): return [dict(id=r["id"], status=r["status"], cause=r["cause"]) for r in C.execute("SELECT * FROM incidents ORDER BY created DESC")]

@app.get("/incidents/{iid}/telemetry")
def telemetry(iid: str):
    ev = [dict(r) for r in C.execute("SELECT seq,ts,kind,name,ok,duration_ms,detail FROM events WHERE incident_id=? ORDER BY seq", (iid,))]
    if not ev: raise HTTPException(404, "not found")
    for e in ev: e["detail"] = json.loads(e["detail"]); e["ok"] = bool(e["ok"])
    cnt = lambda k: sum(1 for e in ev if e["kind"] == k)
    return {"incident_id": iid, "events": ev, "counts": {"diagnostics": cnt("diagnostic"), "actions": cnt("action"), "approvals": cnt("approval")},
            "diagnostics_run": [e["name"] for e in ev if e["kind"] == "diagnostic"],
            "actions_executed": [e["name"] for e in ev if e["kind"] == "action"],
            "total_duration_ms": round(sum(e["duration_ms"] for e in ev), 2)}

@app.post("/incidents/{iid}/replay", dependencies=[Depends(auth)])
def replay(iid: str):
    """Re-derive cause+decision from STORED diagnostic results only. No probes, no actions."""
    r = C.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    if not r: raise HTTPException(404, "not found")
    req = json.loads(r["request"]); cause = None
    for e in C.execute("SELECT name,ok,detail FROM events WHERE incident_id=? AND kind='diagnostic' ORDER BY seq", (iid,)):
        cause = assess(e["name"], {"ok": bool(e["ok"]), "result": json.loads(e["detail"])})
        if cause: break
    dec = decide(cause, req); stored = json.loads(r["decision"]) if r["decision"] else None
    return {"incident_id": iid, "replayed_cause": cause or "unknown", "replayed_decision": dec,
            "stored_cause": r["cause"], "stored_decision": stored,
            "match": (cause or "unknown") == r["cause"] and dec == stored}
