# Evidence-based Invoice Agent

## Public A2A URL

```text
https://empathic-cake-juror.ngrok-free.dev/a2a
```

Agent card:

```text
https://empathic-cake-juror.ngrok-free.dev/.well-known/agent-card.json
```

The public URL is a temporary HTTPS tunnel to the running service.

## A2A request

Send JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "tasks/send",
  "params": {
    "task_id": "invoice-task-1",
    "action": "assess",
    "package": {
      "invoice": {
        "invoice_id": "INV-100",
        "vendor": "Acme Ltd",
        "total": 1080
      },
      "purchase_order": {
        "po_id": "PO-100",
        "unit_price": 100,
        "quantity": 10,
        "gst_rate": 0.08
      },
      "goods_received": {"quantity": 10},
      "credit_notes": []
    }
  }
}
```

Supported methods are `tasks/send`, `message/send`, and `invoice/process`.
`action` may be `assess`, `review`, `dry_run`, `approve`, or `pay`.

Payment requires both:

```json
"action": "pay",
"approval": {"approved": true}
```

## Decision rules

- Expected payment is received quantity multiplied by purchase-order unit price,
  plus the purchase-order GST rate, minus valid credit notes.
- Invoice identity and supplier are required.
- No payment is allowed without received goods and purchase-order pricing.
- A mismatch returns `request_correction`.
- Amounts above 10,000 return `needs_approval` and require explicit approval.
- Every result includes source, field, and value evidence citations.
- Each package is interpreted by the local Ollama `deepseek-r1:1.5b` model.
  The response includes `ai_document_interpretation` with extracted facts,
  missing documents, and suspicious signals. The model is advisory only:
  deterministic reconciliation and approval gates decide whether payment is
  allowed.
- A payment receipt is persisted in SQLite.
- Retries return the original receipt as `already_paid`; they do not create a
  second payment.
- Invalid A2A parameters return a JSON-RPC `-32602` error instead of HTTP 500.

## Local files

- `invoice_agent.py`: FastAPI service and A2A protocol
- `invoice_agent.sqlite3`: persistent task, audit, and payment state
- `Dockerfile`: container deployment
- `requirements.txt`: runtime dependencies
