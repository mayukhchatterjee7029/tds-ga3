from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "invoice_agent.sqlite3"
MAX_AUTO_APPROVAL = 10_000.0
AI_MODEL = "deepseek-r1:1.5b"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
app = FastAPI(title="Evidence-based Invoice Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
db_lock = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                payment_key TEXT PRIMARY KEY,
                invoice_id TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                receipt_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


init_db()


class InvoicePackage(BaseModel):
    invoice: dict[str, Any] = Field(default_factory=dict)
    purchase_order: dict[str, Any] = Field(default_factory=dict)
    goods_received: dict[str, Any] = Field(default_factory=dict)
    credit_notes: list[dict[str, Any]] = Field(default_factory=list)
    approvals: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    task_id: str | None = None
    action: str = "assess"
    package: InvoicePackage
    approval: dict[str, Any] = Field(default_factory=dict)


def first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else default


def text_value(data: dict[str, Any], *keys: str) -> str:
    value = first(data, *keys)
    return str(value).strip() if value is not None else ""


def citation(source: str, field: str, value: Any) -> dict[str, Any]:
    return {"source": source, "field": field, "value": value}


def interpret_with_ai(package: InvoicePackage) -> dict[str, Any]:
    prompt = (
        "You are an invoice-document understanding assistant. Read this JSON "
        "invoice package and return JSON with only these keys: "
        "summary (string), extracted_facts (array of strings), "
        "missing_documents (array of strings), suspicious_signals (array of strings). "
        "Do not approve, reject, or pay. Do not invent facts. "
        f"PACKAGE:\n{package.model_dump_json()}"
    )
    payload = json.dumps(
        {
            "model": AI_MODEL,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0},
        }
    ).encode()
    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = json.loads(response.read())
        content = response_body["message"]["content"]
        interpretation = json.loads(content)
        if not isinstance(interpretation, dict):
            raise ValueError("AI returned a non-object interpretation")
        return {
            "provider": "ollama",
            "model": AI_MODEL,
            "interpretation": interpretation,
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "provider": "ollama",
            "model": AI_MODEL,
            "status": "unavailable",
            "error": str(exc),
        }


def assess(request: AgentRequest) -> dict[str, Any]:
    p = request.package
    inv, po, grn = p.invoice, p.purchase_order, p.goods_received
    invoice_id = text_value(inv, "invoice_id", "id", "number", "invoice_number")
    vendor = text_value(inv, "vendor", "supplier", "supplier_name")
    po_id = text_value(po, "po_id", "id", "number", "purchase_order")
    invoice_total = number(first(inv, "total", "amount", "gross_total"))
    po_unit_price = number(first(po, "unit_price", "price"))
    po_quantity = number(first(po, "quantity", "qty"))
    received_quantity = number(first(grn, "quantity", "qty", "received"))
    gst_rate = number(first(po, "gst_rate", "tax_rate", "gst"))
    if gst_rate > 1:
        gst_rate /= 100
    credit_total = 0.0
    valid_credits: list[dict[str, Any]] = []
    for note in p.credit_notes:
        if str(first(note, "status", "state")).lower() in {"cancelled", "void", "invalid"}:
            continue
        amount = number(first(note, "amount", "value", "total"))
        if amount > 0:
            credit_total += amount
            valid_credits.append(note)
    expected_subtotal = received_quantity * po_unit_price
    expected_total = round(expected_subtotal * (1 + gst_rate) - credit_total, 2)
    evidence = [
        citation("invoice", "invoice_id", invoice_id),
        citation("invoice", "total", invoice_total),
        citation("purchase_order", "po_id", po_id),
        citation("purchase_order", "unit_price", po_unit_price),
        citation("purchase_order", "quantity", po_quantity),
        citation("goods_received", "quantity", received_quantity),
        citation("purchase_order", "gst_rate", gst_rate),
        citation("credit_notes", "valid_total", credit_total),
    ]
    reasons: list[str] = []
    action = "hold"
    if not invoice_id or not vendor:
        reasons.append("missing invoice identity or supplier")
    elif received_quantity <= 0:
        reasons.append("no received quantity supports payment")
    elif po_unit_price <= 0 or po_quantity <= 0:
        reasons.append("purchase order pricing or quantity is missing")
    elif abs(invoice_total - expected_total) > 0.01:
        reasons.append("invoice total does not reconcile to received goods and PO terms")
        action = "request_correction"
    elif expected_total > MAX_AUTO_APPROVAL:
        reasons.append("amount exceeds the automatic approval limit")
        action = "needs_approval"
    else:
        action = "approve"
        reasons.append("invoice reconciles to received goods, PO price, GST, and valid credits")
    result = {
        "invoice_id": invoice_id,
        "vendor": vendor,
        "action": action,
        "amount": expected_total,
        "invoice_total": invoice_total,
        "reasons": reasons,
        "evidence": evidence,
        "valid_credit_notes": valid_credits,
    }
    result["ai_document_interpretation"] = interpret_with_ai(p)
    return result


def record_audit(task_id: str, event: str, detail: dict[str, Any]) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO audit(task_id,event,detail_json,created_at) VALUES(?,?,?,?)",
            (task_id, event, json.dumps(detail), utc_now()),
        )


def execute(task_id: str, request: AgentRequest, assessment: dict[str, Any]) -> dict[str, Any]:
    requested = request.action.lower()
    if requested in {"assess", "review", "dry_run"}:
        return assessment
    if requested not in {"approve", "pay"}:
        raise HTTPException(400, "unsupported action")
    if assessment["action"] not in {"approve", "needs_approval"}:
        raise HTTPException(409, "payment is not approved by reconciliation")
    if not request.approval.get("approved"):
        raise HTTPException(403, "explicit approval is required")
    invoice_id = assessment["invoice_id"]
    payment_key = hashlib.sha256(
        f"{invoice_id}|{assessment['amount']}".encode()
    ).hexdigest()
    with db_lock, connection() as db:
        existing = db.execute(
            "SELECT * FROM payments WHERE payment_key=?", (payment_key,)
        ).fetchone()
        if existing:
            return {
                **assessment,
                "action": "already_paid",
                "payment": json.loads(existing["receipt_json"] or "{}"),
            }
        receipt = {
            "receipt_id": f"rcpt_{uuid.uuid4().hex}",
            "payment_key": payment_key,
            "invoice_id": invoice_id,
            "amount": assessment["amount"],
            "status": "paid",
            "created_at": utc_now(),
        }
        db.execute(
            "INSERT INTO payments VALUES(?,?,?,?,?,?)",
            (payment_key, invoice_id, assessment["amount"], "paid", json.dumps(receipt), utc_now()),
        )
    record_audit(task_id, "payment_committed", receipt)
    return {**assessment, "action": "paid", "payment": receipt}


def handle(request: AgentRequest) -> dict[str, Any]:
    task_id = request.task_id or f"task_{uuid.uuid4().hex}"
    payload_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with db_lock, connection() as db:
        old = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if old and old["request_hash"] == payload_hash and old["result_json"]:
            return json.loads(old["result_json"])
        if old and old["status"] == "committed":
            raise HTTPException(409, "task was already committed with different input")
        now = utc_now()
        db.execute(
            "INSERT OR REPLACE INTO tasks(task_id,request_hash,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (task_id, payload_hash, "processing", now, now),
        )
    assessment = assess(request)
    result = execute(task_id, request, assessment)
    with db_lock, connection() as db:
        db.execute(
            "UPDATE tasks SET status=?, result_json=?, updated_at=? WHERE task_id=?",
            ("committed" if result["action"] == "paid" else "complete", json.dumps(result), utc_now(), task_id),
        )
    record_audit(task_id, "task_complete", result)
    return {"task_id": task_id, **result}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": "Evidence-based Invoice Agent",
        "description": "Reconciles invoices, cites evidence, enforces approval limits, and prevents duplicate payments.",
        "url": "/a2a",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "skills": [{"id": "invoice-reconciliation", "name": "Invoice reconciliation and safe payment"}],
    }


def process_a2a(body: dict[str, Any]) -> dict[str, Any]:
    method = body.get("method", "tasks/send")
    params = body.get("params", body.get("input", {}))
    if isinstance(params, dict) and "message" in params and "package" not in params:
        message = params["message"]
        for part in message.get("parts", []):
            data = part.get("data")
            if isinstance(data, dict):
                params = data
                break
            text = part.get("text")
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    params = decoded
                    break
    if method not in {"tasks/send", "message/send", "invoice/process"}:
        return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "method not found"}}
    try:
        request = AgentRequest.model_validate(params)
        result = handle(request)
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": {"status": "completed", "task": result}}
    except ValidationError as exc:
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32602, "message": "invalid params", "data": exc.errors()},
        }
    except HTTPException as exc:
        return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": exc.status_code, "message": str(exc.detail)}}


def a2a_response(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=process_a2a(body),
        media_type="application/a2a+json",
    )


def a2a_discovery() -> dict[str, Any]:
    return {
        "name": "Evidence-based Invoice Agent",
        "protocol": "JSON-RPC 2.0",
        "methods": ["tasks/send", "message/send", "invoice/process"],
        "endpoint": "/a2a",
        "message_endpoint": "/a2a/message:send",
    }


@app.get("/a2a")
def a2a_get() -> dict[str, Any]:
    return a2a_discovery()


@app.post("/a2a")
def a2a(body: dict[str, Any]) -> JSONResponse:
    return a2a_response(body)


@app.post("/a2a/message:send")
def a2a_message_send(body: dict[str, Any]) -> JSONResponse:
    """Compatibility route used by A2A batteries for the message/send RPC."""
    return a2a_response({**body, "method": body.get("method", "message/send")})


@app.post("/a2a/message%3Asend")
def a2a_message_send_encoded(body: dict[str, Any]) -> JSONResponse:
    """Compatibility route for proxies that preserve the encoded colon."""
    return a2a_response({**body, "method": body.get("method", "message/send")})


@app.get("/a2a/message:send")
@app.get("/a2a/message%3Asend")
def a2a_message_discovery() -> dict[str, Any]:
    return a2a_discovery()


@app.get("/a2a/tasks")
def a2a_tasks() -> dict[str, Any]:
    return {"tasks": []}


@app.post("/tasks")
def tasks(request: AgentRequest) -> dict[str, Any]:
    return handle(request)


@app.get("/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    return {
        "task_id": task_id,
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }
