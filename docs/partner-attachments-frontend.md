# Partner Attachments — Frontend Integration

## What changed

A new endpoint lets users attach files (PDF, DOCX, XLSX, CSV, TXT, MD, images) to a partner discovery conversation that has already been opened. The server uploads the file to GCS, runs **one cheap LLM pass** to produce a summary, and **auto-fills partner-discovery fields** the file confidently mentions (`partner_industry`, `partner_pains`, `objectives_*`, etc.).

Attachments are gated behind the conversation having been opened first. The required order is:

1. `partner/chat/dispatch` with `message: null` to open the conversation and get the first-pass reply.
2. Then (and only then) the user can upload one or more files.
3. The user's next message via `partner/chat/dispatch` triggers the assistant's response, which now sees the attachment-derived data on the conversation row.

The dispatch contract did NOT change — no new required params on the FE side. Uploading a file does NOT trigger a chat turn; it only loads context.

Lower-confidence findings are NOT auto-applied. They land in `field_candidates` on the conversation row, ready for the FE to surface as "we found this in your file — want to use it?" prompts.

---

## Endpoint

```
POST /api/v0/partner/conversations/attachments/upload
Content-Type: multipart/form-data
Authorization: Bearer <user_token>
```

### Request — multipart form fields

| field              | type   | required | notes |
|--------------------|--------|----------|-------|
| `attachment`       | file   | yes      | The file to upload. Max 50 MB. |
| `conversation_key` | string | yes      | Same key used in `partner/chat/dispatch`. If the conversation doesn't exist yet, the server creates it. |
| `metadata`         | string | no       | Optional JSON object as a string. Stored on the attachment row. |

### Supported file types

`pdf`, `docx`, `xlsx`, `xlsm`, `csv`, `txt`, `md`, `markdown`, `jpg/jpeg`, `png`, `webp`, `gif`, `bmp`. Other types are accepted but only get a metadata-only summary (no extracted fields).

### Example — fetch

```ts
const form = new FormData();
form.append("attachment", file);
form.append("conversation_key", conversationKey);

const res = await fetch("/api/v0/partner/conversations/attachments/upload", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
  body: form,
});
const json = await res.json();
```

The endpoint is **synchronous** with a 30-second hard timeout. Show a "reading your file…" spinner.

---

## Response

### 200 OK — happy path (`status === "ok"`, attachment status `"ready"`)

```json
{
  "status": "ok",
  "data": {
    "attachment": {
      "id": "8e3c…",
      "conversation_id": "f2a0…",
      "conversation_key": "abc123",
      "filename": "customer_survey_q3.pdf",
      "gcs_key": "uploads/partner/<conversation_id>/files/customer_survey_q3.<uuid>.pdf",
      "signed_url": "https://storage.googleapis.com/…?X-Goog-Signature=…",
      "content_type": "application/pdf",
      "size_bytes": 482912,
      "status": "ready",
      "classification": "pdf_report",
      "extraction_error": null,
      "summary_text": "100–300 word prose summary written by the LLM …",
      "summary": {
        "purpose": "Q3 customer churn analysis",
        "classification": "pdf_report",
        "key_topics": ["churn drivers", "pricing"],
        "key_insights": ["NPS dropped 11 points QoQ", "…"],
        "structures": ["10 sections", "3 tables"],
        "entities": {
          "companies": ["Acme Co"],
          "people":    [],
          "products":  ["Product X v2"],
          "metrics":   ["NPS", "MRR"]
        }
      },
      "model_provider": "openai",
      "model_name": "gpt-4o-mini",
      "prompt_version": "v1",
      "created_at": "2026-04-29T12:34:56.000Z",
      "updated_at": "2026-04-29T12:35:04.000Z"
    },
    "applied_fields": {
      "partner_industry": {
        "value": "B2B SaaS",
        "confidence": 0.93,
        "evidence": "page 1 header",
        "attachment_id": "8e3c…",
        "filename": "customer_survey_q3.pdf",
        "captured_at": "2026-04-29T12:35:03.000Z"
      },
      "partner_pains": [
        {
          "value": "manual onboarding takes 2 weeks",
          "confidence": 0.91,
          "evidence": "section 3, paragraph 2",
          "attachment_id": "8e3c…",
          "filename": "customer_survey_q3.pdf",
          "captured_at": "2026-04-29T12:35:03.000Z"
        }
      ]
    },
    "candidate_fields": {
      "partner_name": {
        "value": "Acme Corp",
        "confidence": 0.74,
        "evidence": "footer mention",
        "attachment_id": "8e3c…",
        "filename": "customer_survey_q3.pdf",
        "captured_at": "2026-04-29T12:35:03.000Z",
        "reason": "below_auto_apply_threshold"
      }
    }
  },
  "perf_ms": 7842.31
}
```

**`applied_fields`** — what was auto-upserted into the conversation row (confidence ≥ 0.9; for scalars, only when the target column was empty).

**`candidate_fields`** — what was *not* applied. Two reasons appear in `reason`:
- `"below_auto_apply_threshold"` — confidence in [0.5, 0.9)
- `"scalar_already_set"` — confidence ≥ 0.9 but the conversation already had a user-set value (we never overwrite)

Scalar entries are objects; list entries are arrays of objects.

### 200 OK — failed extraction (file is in GCS, summary failed)

The endpoint still returns 200; the failure is signaled inside `data.attachment.status`:

```json
{
  "status": "ok",
  "data": {
    "attachment": {
      "status": "failed",
      "extraction_error": "llm_call_failed: …",
      "summary_text": null,
      "summary": null,
      ...
    },
    "applied_fields": {},
    "candidate_fields": {}
  }
}
```

The user can retry. The file row still exists with the original `gcs_key` so a backend job could re-summarize without re-uploading.

### Error responses

| status | meaning |
|--------|---------|
| 400    | Missing/empty file, missing `conversation_key`, or invalid metadata JSON |
| 401    | No / invalid auth token |
| 413    | File exceeds 50 MB |
| 500    | GCS upload failed or unhandled server error |

---

## Required FE flow

The conversation MUST be opened by a dispatch call before attachments are allowed. Attachments are a "context-loading" action — they do NOT trigger a chat turn on their own. The user's eventual message is what fires dispatch.

```
1. Conversation start
   POST /api/v0/partner/chat/dispatch
        { conversation_key, message: null }
   -> conversation row is created, assistant returns its first-pass reply

2. Attachments allowed (any time after step 1, zero or more)
   POST /api/v0/partner/conversations/attachments/upload
        multipart: attachment, conversation_key
   -> each upload writes to GCS, summarizes, and pre-fills conversation fields
   -> NO dispatch turn is triggered; the assistant doesn't reply yet

3. User sends a message (with or without prior attachments)
   POST /api/v0/partner/chat/dispatch
        { conversation_key, message: "<user text>" }
   -> assistant sees ALL attachment-derived data already on the conversation row
```

### Per-upload UX

- Show a "reading your file…" spinner during the upload call (typical 3–10s, hard timeout 30s).
- On `data.attachment.status === "ready"`:
  - Show the prose summary (`data.attachment.summary_text`) so the user knows what the LLM saw.
  - Optionally render `applied_fields` as inline "we already filled this in" chips, and `candidate_fields` as confirmable suggestions.
- On `"failed"`: show `extraction_error` and offer a retry button. The file row is still in GCS, so a retry can be a simple re-summarize call later (admin-only for now).
- Critical: do NOT auto-send a chat turn after upload. The user is still composing. The next dispatch call only fires when they hit "send."

### Optional later: confirming candidates

`candidate_fields` are suggestions, not applied data. Two ways to handle them on the FE:
- Pass them through to the chat as user-confirmable chips ("Use *Acme Corp* as partner name?"). When the user confirms, send a normal chat message like *"Yes, partner name is Acme Corp"* and dispatch will pick it up via the existing extraction logic.
- (Future) A dedicated "confirm candidate" endpoint can be added if we want one-click apply without a chat round-trip.

The same data is also accumulated on the conversation row at `field_candidates` (a JSONB object keyed by stage column), so the FE can fetch the conversation later and re-render outstanding suggestions even across reloads.

---

## Pending — not yet wired

- `partner/chat/dispatch` does not yet accept `attachment_ids`. For now, the FE doesn't need to send any. Once we wire it, FE may optionally send `attachment_ids: [uuid, …]` to scope which attachments are "active" for that turn (e.g. when a user removes a file from the composer mid-conversation).
- Google Sheets (`application/vnd.google-apps.spreadsheet`) is not supported — needs Drive OAuth, planned for a later milestone.
- Scanned/image-only PDFs are not OCR'd in v1 — they return a low-content summary with a note.
