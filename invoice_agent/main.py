"""
Evidence-first invoice agent scaffold.
- Public HTTPS deployment: run behind a platform TLS proxy.
- Durable SQLite state and idempotent action ledger.
- A2A JSON-RPC endpoint plus simple /check and /save endpoints.
IMPORTANT: wire the platform-specific package/receipt/payment API into ActionProvider
once the assignment's complete API contract is available. This scaffold never fabricates
payment receipts and does not initiate real payments by default.
"""
from __future__ import annotations
import os, json, sqlite3, hashlib, uuid, datetime
from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "invoice_agent.sqlite3")
APP_NAME = "Evidence-First Invoice Agent"
app = FastAPI(title=APP_NAME, version="1.0.0")

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
          task_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, status TEXT NOT NULL,
          result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS actions (
          idempotency_key TEXT PRIMARY KEY, invoice_key TEXT NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
init_db()

class Package(BaseModel):
    package_id: Optional[str] = None
    customer_account: Optional[str] = None
    invoice: dict[str, Any] = Field(default_factory=dict)
    purchase_order: dict[str, Any] = Field(default_factory=dict)
    goods_received_note: dict[str, Any] = Field(default_factory=dict)
    supplier_email: dict[str, Any] = Field(default_factory=dict)
    credit_notes: list[dict[str, Any]] = Field(default_factory=list)
    approvals: dict[str, Any] = Field(default_factory=dict)
    prior_actions: list[dict[str, Any]] = Field(default_factory=list)
    raw_documents: list[dict[str, Any]] = Field(default_factory=list)

class SaveRequest(BaseModel):
    task_id: str
    approved_action: Optional[str] = None
    approval_reference: Optional[str] = None

class ActionProvider:
    """Replace with the assignment's actual action/receipt API adapter."""
    def execute(self, action: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        # Fail closed: never pretend that an external action happened.
        raise RuntimeError("No action provider configured; refusing to fabricate a receipt.")

provider = ActionProvider()

def money(v):
    try: return round(float(v), 2)
    except (TypeError, ValueError): return None

def first(d, *keys):
    for k in keys:
        if k in d and d[k] is not None: return d[k]
    return None

def invoice_identity(p: Package) -> str:
    inv=p.invoice
    supplier=str(first(inv,"supplier_name","supplier","vendor_name") or first(p.purchase_order,"supplier_name","supplier") or "unknown").strip().lower()
    number=str(first(inv,"invoice_number","number","invoice_id") or p.package_id or "unknown").strip().lower()
    return f"{supplier}|{number}"

def evidence(doc_name, field, value, detail=""):
    return {"document":doc_name,"field":field,"value":value,"detail":detail}

def analyze(p: Package) -> dict[str, Any]:
    """Deterministic reconciliation. Unknown fields cause review, not guessed payment."""
    inv, po, grn = p.invoice, p.purchase_order, p.goods_received_note
    evidence_list=[]; issues=[]
    inv_no=first(inv,"invoice_number","number","invoice_id")
    supplier=first(inv,"supplier_name","supplier","vendor_name")
    po_supplier=first(po,"supplier_name","supplier","vendor_name")
    po_no=first(inv,"purchase_order_number","po_number") or first(po,"po_number","purchase_order_number","number")
    grn_po=first(grn,"po_number","purchase_order_number")
    if inv_no: evidence_list.append(evidence("invoice","invoice_number",inv_no))
    if supplier: evidence_list.append(evidence("invoice","supplier_name",supplier))
    if po_no: evidence_list.append(evidence("purchase_order","po_number",po_no))
    if supplier and po_supplier and str(supplier).strip().casefold()!=str(po_supplier).strip().casefold():
        issues.append("Supplier on invoice does not match purchase order.")
    if po_no and grn_po and str(po_no).strip().casefold()!=str(grn_po).strip().casefold():
        issues.append("Goods received note references a different purchase order.")
    qty=money(first(grn,"quantity_received","received_quantity","quantity","cartons"))
    rate=money(first(po,"unit_price","price_per_unit","rate","price_per_carton"))
    gst_pct=money(first(po,"gst_percent","gst_rate_percent","tax_percent"))
    if qty is None: issues.append("Received quantity is missing.")
    if rate is None: issues.append("Purchase-order unit price is missing.")
    if gst_pct is None: issues.append("Purchase-order GST percentage is missing.")
    base=round(qty*rate,2) if qty is not None and rate is not None else None
    gst=round(base*gst_pct/100,2) if base is not None and gst_pct is not None else None
    gross=round(base+gst,2) if base is not None and gst is not None else None
    if qty is not None: evidence_list.append(evidence("goods_received_note","quantity_received",qty))
    if rate is not None: evidence_list.append(evidence("purchase_order","unit_price",rate))
    if gst_pct is not None: evidence_list.append(evidence("purchase_order","gst_percent",gst_pct))
    invoice_total=money(first(inv,"total","total_amount","amount_due","grand_total"))
    if invoice_total is not None:
        evidence_list.append(evidence("invoice","total_amount",invoice_total))
        if gross is not None and abs(invoice_total-gross)>0.02:
            issues.append(f"Invoice total {invoice_total:.2f} differs from PO-priced received goods plus GST ({gross:.2f}), before valid credits.")
    valid_credits=[]; credit_total=0.0
    for cn in p.credit_notes:
        status=str(first(cn,"status","state") or "").strip().casefold()
        amount=money(first(cn,"amount","total","credit_amount"))
        cn_no=first(cn,"credit_note_number","number","id")
        # Only explicitly valid/issued/active credit notes count. Cancelled/replaced are excluded.
        if status in {"cancelled","canceled","void","rejected","invalid"}:
            evidence_list.append(evidence("credit_note","status",status,f"{cn_no or 'unnumbered'} excluded"))
            continue
        if status in {"valid","issued","active","approved"} and amount is not None:
            valid_credits.append({"number":cn_no,"amount":amount,"status":status})
            credit_total+=amount
            evidence_list.append(evidence("credit_note","amount",amount,f"{cn_no or 'unnumbered'} status={status}"))
        elif amount is not None:
            issues.append(f"Credit note {cn_no or '(unnumbered)'} has no explicit valid status; amount not deducted.")
    payable=round(gross-credit_total,2) if gross is not None else None
    if payable is not None and payable < 0:
        issues.append("Valid credit notes exceed the calculated payable amount.")
    prior_ids={str(first(x,"invoice_number","invoice_id","number") or "").strip().casefold() for x in p.prior_actions}
    duplicate=bool(inv_no and str(inv_no).strip().casefold() in prior_ids)
    if duplicate: issues.append("A prior action references this same invoice number; do not pay again.")
    # Payment approval must be explicit, not inferred from the documents.
    approval=bool(first(p.approvals,"payment_approved","approved") is True)
    if not approval: issues.append("No explicit payment approval is present.")
    action="review"
    if duplicate: action="duplicate_no_payment"
    elif issues: action="review"
    elif approval and payable is not None: action="pay"
    return {
      "invoice_key":invoice_identity(p),"invoice_number":inv_no,"supplier":supplier,
      "calculation":{"received_quantity":qty,"po_unit_price":rate,"subtotal":base,
        "gst_percent":gst_pct,"gst_amount":gst,"gross_before_credits":gross,
        "valid_credit_notes":valid_credits,"credit_total":round(credit_total,2),
        "payable":payable},
      "recommended_action":action,"issues":issues,"evidence":evidence_list,
      "explanation":"Payable is calculated from PO unit price × received quantity, plus PO GST, less only explicitly valid credit notes. Any missing/mismatched evidence or missing approval blocks payment.",
      "receipt":None
    }

def log_event(task_id, typ, payload):
    with db() as c:
        c.execute("INSERT INTO events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                  (task_id,typ,json.dumps(payload,default=str),now()))

@app.get("/health")
def health(): return {"ok":True,"name":APP_NAME}

@app.get("/.well-known/agent-card.json")
def agent_card():
    return {"name":APP_NAME,"description":"Reconciles invoice, PO, goods received note, supplier email and credit notes. Produces evidence-cited decisions, enforces explicit approval and avoids repeating recorded actions.","url":os.getenv("PUBLIC_BASE_URL","").rstrip("/")+"/a2a",
      "version":"1.0.0","capabilities":{"streaming":False,"pushNotifications":False},"defaultInputModes":["text","application/json"],"defaultOutputModes":["text","application/json"],"skills":[{"id":"invoice-reconciliation","name":"Invoice reconciliation","description":"Reconcile invoice packages and return recommended action with evidence."}]}

@app.post("/check")
def check(p: Package, x_request_id: Optional[str]=Header(default=None)):
    payload=p.model_dump()
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
    task_id=x_request_id or str(uuid.uuid4())
    with db() as c:
        row=c.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if row and row["input_hash"]!=digest:
            raise HTTPException(409,"task_id already exists with different input")
        if row and row["result_json"]:
            return json.loads(row["result_json"])
        c.execute("INSERT OR IGNORE INTO tasks(task_id,input_hash,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                  (task_id,digest,"checked",now(),now()))
    result=analyze(p); result["task_id"]=task_id
    with db() as c:
        c.execute("UPDATE tasks SET status='checked',result_json=?,updated_at=? WHERE task_id=?",
                  (json.dumps(result),now(),task_id))
    log_event(task_id,"check",result)
    return result

@app.post("/save")
def save(req: SaveRequest):
    with db() as c:
        row=c.execute("SELECT * FROM tasks WHERE task_id=?",(req.task_id,)).fetchone()
        if not row or not row["result_json"]:
            raise HTTPException(404,"Check this task before Save")
        result=json.loads(row["result_json"])
        if result.get("saved") is True:
            return result
        if req.approved_action not in {"pay","request_credit_note","hold","review","duplicate_no_payment"}:
            raise HTTPException(400,"approved_action must be an allowed action")
        recommended=result.get("recommended_action")
        if req.approved_action=="pay":
            if recommended!="pay" or not req.approval_reference:
                raise HTTPException(403,"Payment requires a pay recommendation and explicit approval_reference")
            key=hashlib.sha256((result["invoice_key"]+"|pay").encode()).hexdigest()
            existing=c.execute("SELECT * FROM actions WHERE idempotency_key=?",(key,)).fetchone()
            if existing:
                if existing["status"]=="completed" and existing["receipt_json"]:
                    result["receipt"]=json.loads(existing["receipt_json"])
                else:
                    raise HTTPException(409,"Payment action is pending/uncertain; reconcile provider status before retrying")
            else:
                c.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?)",
                  (key,result["invoice_key"],"pay","pending",json.dumps({"amount":result["calculation"]["payable"],"approval_reference":req.approval_reference}),None,now(),now()))
                # Fail closed until real assignment action adapter is wired.
                raise HTTPException(503,"No real payment/receipt provider configured. Action remains pending; do not retry until reconciled.")
        result["saved"]=True
        result["approved_action"]=req.approved_action
        result["approval_reference"]=req.approval_reference
        with db() as c:
            c.execute("UPDATE tasks SET status='saved',result_json=?,updated_at=? WHERE task_id=?",
                      (json.dumps(result),now(),req.task_id))
        log_event(req.task_id,"save",result)
        return result

@app.post("/a2a")
def a2a(body: dict[str, Any]):
    """Minimal JSON-RPC A2A compatibility endpoint for message/send."""
    method=body.get("method")
    rid=body.get("id")
    params=body.get("params") or {}
    if method in {"message/send","message/stream"}:
        msg=params.get("message") or {}
        parts=msg.get("parts") or []
        text_in=" ".join(str(part.get("text","")) for part in parts if isinstance(part,dict))
        # Protocol-safe response asks for structured package JSON; never invent document facts.
        answer=("Please submit the invoice package as JSON to POST /check, with invoice, purchase_order, "
                "goods_received_note, supplier_email, credit_notes, approvals, and prior_actions. "
                "The agent will return a recommended action and document-field evidence citations. "
                "No payment is executed through this free-text endpoint.")
        return {"jsonrpc":"2.0","id":rid,"result":{"kind":"message","messageId":str(uuid.uuid4()),
          "role":"agent","parts":[{"kind":"text","text":answer+" Received message: "+text_in[:500]}],
          "taskId":params.get("taskId") or str(uuid.uuid4()),"status":{"state":"completed"}}}
    if method=="tasks/get":
        return {"jsonrpc":"2.0","id":rid,"error":{"code":-32602,"message":"Use POST /check with the task_id to retrieve task state."}}
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"Method not found"}}

@app.get("/")
def root():
    return {"name":APP_NAME,"health":"/health","agent_card":"/.well-known/agent-card.json",
            "check":"/check","save":"/save","a2a":"/a2a"}
