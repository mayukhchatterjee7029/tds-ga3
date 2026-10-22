import os, json, time, sqlite3, uuid, hashlib, datetime, threading
from typing import Optional
import httpx
from openai import OpenAI
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL = os.getenv("LLM_MODEL", "google/gemma-4-31b-it:free")
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY", "unset"), timeout=30, max_retries=1)
MAX_STEPS = 6
LOCK = threading.Lock()
DB = os.getenv("DB_PATH", "incidents.db")
try: os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True); open(DB, "a").close()
except OSError: DB = "/tmp/incidents.db"
C = sqlite3.connect(DB, check_same_thread=False); C.row_factory = sqlite3.Row
C.executescript("""
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, created TEXT, request TEXT, status TEXT, cause TEXT, action TEXT, action_result TEXT);
CREATE TABLE IF NOT EXISTS events(incident_id TEXT, seq INT, ts TEXT, kind TEXT, name TEXT, ok INT, duration_ms REAL, detail TEXT, prev TEXT, hash TEXT);
""")
app = FastAPI(title="Incident Agent")
now = lambda: datetime.datetime.utcnow().isoformat() + "Z"

class Diag(BaseModel): name: str; description: str = ""; url: str; method: str = "GET"
class Act(BaseModel): name: str; description: str = ""; url: str; method: str = "POST"
class Svc(BaseModel): name: str; replicas: Optional[int] = None; max_replicas: Optional[int] = None
class Incident(BaseModel):
    id: Optional[str] = None; title: str; description: str = ""; service: Svc
    diagnostics: list[Diag] = []; actions: list[Act] = []

def digest(prev, seq, ts, kind, name, ok, detail):
    return hashlib.sha256(json.dumps([prev, seq, ts, kind, name, ok, detail], sort_keys=True).encode()).hexdigest()

def emit(iid, kind, name, ok, dur, detail):
    with LOCK:
        last = C.execute("SELECT seq,hash FROM events WHERE incident_id=? ORDER BY seq DESC LIMIT 1", (iid,)).fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, "")
        ts, d = now(), json.dumps(detail, sort_keys=True)
        C.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (iid, seq, ts, kind, name, int(ok), round(dur, 2), d, prev, digest(prev, seq, ts, kind, name, int(ok), d)))
        C.commit()

def set_(iid, **kw):
    with LOCK:
        for k, v in kw.items(): C.execute(f"UPDATE incidents SET {k}=? WHERE id=?", (v if isinstance(v, str) or v is None else json.dumps(v), iid))
        C.commit()

SYSTEM = """You are an incident-response agent. Reply with ONE JSON object, no prose.
Find the cause using as FEW diagnostics as needed, then choose ONE permitted action.
To run a diagnostic: {"diagnostic": "<name from available_diagnostics>", "reason": "..."}
To act: {"action": "<name from permitted_actions>", "cause": "<root cause>", "reason": "..."}
Rule: if the service is already at its max_replicas, do NOT scale; page the owner instead.
Never invent evidence. Use only the names provided."""

def ask(iid, req, results):
    ran = {r["name"] for r in results}
    user = {"title": req["title"], "description": req["description"], "service": req["service"],
            "available_diagnostics": [{"name": d["name"], "description": d["description"]} for d in req["diagnostics"] if d["name"] not in ran],
            "diagnostic_results": results,
            "permitted_actions": [{"name": a["name"], "description": a["description"]} for a in req["actions"]]}
    t = time.time(); out, usage, err = None, {}, None
    try:
        r = client.chat.completions.create(model=MODEL, temperature=0, extra_body={"reasoning": {"enabled": True}},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(user)}])
        usage = r.usage.model_dump() if r.usage else {}
        txt = r.choices[0].message.content or ""; out = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    emit(iid, "llm", MODEL, out is not None, (time.time() - t) * 1000, {"output": out, "usage": usage, "error": err})
    return out

def call(item, iid, extra):
    t = time.time()
    try:
        r = httpx.request(item["method"], item["url"], json={"incident_id": iid, **extra}, timeout=10)
        return {"executed": r.is_success, "http_status": r.status_code, "response": r.text[:500]}, (time.time() - t) * 1000
    except Exception as e:
        return {"executed": False, "error": f"{type(e).__name__}: {e}"[:200]}, (time.time() - t) * 1000

def run(iid, req):
    results = []
    diags = {d["name"]: d for d in req["diagnostics"]}; acts = {a["name"]: a for a in req["actions"]}
    for _ in range(MAX_STEPS):
        out = ask(iid, req, results)
        if out is None: set_(iid, status="model_error"); return
        if "diagnostic" in out:
            n = out["diagnostic"]
            if n not in diags or any(r["name"] == n for r in results):
                emit(iid, "rejected", str(n), False, 0, {"reason": "unknown or already run"}); continue
            res, dur = call(diags[n], iid, {"diagnostic": n})
            emit(iid, "diagnostic", n, res["executed"], dur, res); results.append({"name": n, **res})
        elif "action" in out:
            n, cause = out["action"], str(out.get("cause", ""))[:300]
            if n not in acts:
                emit(iid, "rejected", str(n), False, 0, {"reason": "not a permitted action"}); continue
            s = req["service"]
            if n.startswith("scale") and s["replicas"] is not None and s["max_replicas"] is not None and s["replicas"] >= s["max_replicas"]:
                page = next((a for a in acts if a.startswith("page")), None)
                emit(iid, "override", n, True, 0, {"from": n, "to": page, "reason": "service at scaling limit"})
                if not page: set_(iid, status="no_action", cause=cause); return
                n = page
            res, dur = call(acts[n], iid, {"action": n, "cause": cause, "reason": out.get("reason", ""), "service": s["name"]})
            emit(iid, "action", n, res["executed"], dur, res)
            set_(iid, cause=cause, action=n, action_result=res, status="resolved" if res["executed"] else "action_failed"); return
    set_(iid, status="no_action")

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/incidents")
def create(body: Incident):
    req = body.model_dump(); iid = req["id"] or "inc_" + uuid.uuid4().hex[:10]; req["id"] = iid
    with LOCK:
        if C.execute("SELECT 1 FROM incidents WHERE id=?", (iid,)).fetchone(): return get(iid)
        C.execute("INSERT INTO incidents(id,created,request,status) VALUES(?,?,?,?)", (iid, now(), json.dumps(req), "investigating")); C.commit()
    emit(iid, "incident", "opened", True, 0, {"title": req["title"]})
    run(iid, req)
    return get(iid)

@app.get("/incidents/{iid}")
def get(iid: str):
    r = C.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    if not r: raise HTTPException(404, "not found")
    j = lambda s: json.loads(s) if s else None
    return {"id": iid, "created": r["created"], "status": r["status"], "cause": r["cause"], "action": r["action"], "action_result": j(r["action_result"]), "request": j(r["request"])}

@app.get("/incidents/{iid}/telemetry")
def telemetry(iid: str):
    rows = C.execute("SELECT * FROM events WHERE incident_id=? ORDER BY seq", (iid,)).fetchall()
    if not rows: raise HTTPException(404, "not found")
    ev, prev, ok_chain = [], "", True
    for r in rows:
        ok_chain &= r["prev"] == prev and r["hash"] == digest(prev, r["seq"], r["ts"], r["kind"], r["name"], r["ok"], r["detail"]); prev = r["hash"]
        ev.append({"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "name": r["name"], "ok": bool(r["ok"]), "duration_ms": r["duration_ms"], "detail": json.loads(r["detail"]), "hash": r["hash"]})
    n = lambda k: sum(e["kind"] == k for e in ev)
    u = [e["detail"].get("usage") or {} for e in ev if e["kind"] == "llm"]
    tok = lambda k: sum(int(x.get(k) or 0) for x in u)
    return {"incident_id": iid, "chain_ok": ok_chain, "events": ev,
            "counts": {"diagnostics": n("diagnostic"), "actions": n("action"), "overrides": n("override"), "rejected": n("rejected"), "llm_calls": n("llm")},
            "diagnostics_run": [e["name"] for e in ev if e["kind"] == "diagnostic"], "actions_executed": [e["name"] for e in ev if e["kind"] == "action"],
            "tokens": {"prompt": tok("prompt_tokens"), "completion": tok("completion_tokens"), "total": tok("total_tokens")}}