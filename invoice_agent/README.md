# Evidence-First Invoice Agent

## What is included
- FastAPI HTTP service with HTTPS-ready hosting via Render/Railway/Fly/etc.
- A2A Agent Card at `/.well-known/agent-card.json` and a minimal JSON-RPC endpoint at `/a2a`.
- `/check` endpoint to reconcile structured invoice packages and cite document fields.
- `/save` endpoint with persistent SQLite task/action records and idempotency key.
- Fail-closed payment behavior: no payment is simulated and no fake receipt is produced.

## Important assignment integration
The prompt supplied so far ends at “A2A interface base URL” and does not include the evaluator's exact endpoint contract or action/receipt API. This implementation is a safe deployable foundation, not yet guaranteed compatible with that evaluator. Before using a scarce Check/Save attempt, wire the exact contract and the real action provider. Do not test against the grader until that is done.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open `http://127.0.0.1:8000/docs`.

## Check package
```bash
curl -X POST http://127.0.0.1:8000/check \\
  -H 'Content-Type: application/json' \\
  -H 'X-Request-ID: task-001' \\
  -d '{
    "package_id":"pkg-001",
    "invoice":{"invoice_number":"INV-001","supplier_name":"Example Supplier","total_amount":508462},
    "purchase_order":{"po_number":"PO-001","supplier_name":"Example Supplier","unit_price":2780,"gst_percent":18},
    "goods_received_note":{"po_number":"PO-001","quantity_received":155},
    "supplier_email":{"text":"Please pay the invoice"},
    "credit_notes":[{"credit_note_number":"CN-OLD","amount":1000,"status":"cancelled"}],
    "approvals":{"payment_approved":false},
    "prior_actions":[]
  }'
```

## Safety and state
- A repeated `X-Request-ID` with identical package input returns the stored result.
- Reusing a task ID with different input returns HTTP 409.
- Payments require a `pay` recommendation plus an approval reference.
- An action key is unique per invoice identity and action, so different invoices from the same supplier are not treated as duplicates.
- The provider adapter intentionally refuses to run until connected to the real API. A pending action must be reconciled with the provider before any retry.
- For production/multi-instance use, use PostgreSQL or a persistent mounted disk; ephemeral filesystems lose SQLite state on redeploy.
