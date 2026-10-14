# LotoWriter

Generates **EHS-003a Machine-Specific Energy Control Procedures** (DOCX + PDF) from structured procedure data and photos. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full plan.

**Status: Phase 4 (review workflow) — ready for the pilot.** Browser app for phone, tablet and PC: sign-in, assets, isolation points with photos and markup, procedure details, completeness check, Copy from Similar Machine, offline editing, the paper-review/approval workflow, revisions, scans, periodic reviews and a dashboard. To install on the Linux PC, see [deploy/INSTALL.md](deploy/INSTALL.md).

> Generated documents are **drafts for review**. Anything not marked `"status": "active"` prints with a *DRAFT – NOT FOR USE* watermark. The completeness warnings are a drafting aid, not a safety check; trained personnel must review every procedure.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
# .venv/bin/python -m pip install -r requirements.txt       # Linux
```

PDF output needs **LibreOffice** (`soffice`) on the Linux server. On a Windows dev PC, Microsoft Word is used instead if LibreOffice isn't installed.

## Run the app

```bash
.venv/Scripts/python -m lotowriter serve --port 8765        # http://localhost:8765
```

- First sign-in: user **admin**, password **Password1**. You must set a new password before anything else. **Do this immediately after installing**, before announcing the address.
- Data (database, photos, generated documents) is stored in `./data`, or wherever `LOTOWRITER_DATA` points (e.g. `/var/lib/lotowriter`).
- Lost the only admin password? On the server: `python -m lotowriter reset-admin` (resets to Password1, must change at sign-in).
- Sign-in cookies are marked *Secure* (HTTPS only; browsers also accept them on `localhost`) unless `LOTOWRITER_SECURE_COOKIE=0`.
- **Production at Villa Rica: closed private Wi-Fi, plain HTTP on port 80, no certificates** (the service file sets `LOTOWRITER_SECURE_COOKIE=0`). See [deploy/INSTALL.md](deploy/INSTALL.md). HTTPS remains an option in the guide's appendix.

### Review & approval workflow

```
Draft ─► Ready for review ─► Printed for review ─► Approved ─► Active ─► Superseded
  ▲                                │
  └──── Returned with markups ◄────┘
```

| Step | Who / where | What the app does |
|---|---|---|
| Mark ready for review | Review & print tab | Blocked until the completeness check passes |
| Print for review | Review & print | Prints the review copy (DRAFT watermark) and **locks** the procedure while reviewers have the paper |
| Returned with markups | Review & print | Records what must change; the procedure becomes editable again |
| Record approval | Review & print | Reviewer name(s) and date; the app records the paper approval — it doesn't approve anything itself. Upload a scan of the signed copy |
| Activate | Review & print | Sets the effective date, prints the **final copy without watermark**, and marks the previous Active revision **Superseded** |
| Start a new revision | Active procedure | Creates V01, V02… as a draft copy with a required reason; the Active one stays in use until the new one is activated |
| Record periodic review | Active procedure | Annual review with no changes: moves the next due date one year on (the printed document isn't re-issued) |
| Flag for revision | Active procedure | Machine changed; shows on the dashboard until a new revision is activated |

- **History tab:** all revisions, periodic reviews and an activity log for the procedure.
- **Dashboard:** reviews overdue / due within 60 days, flagged procedures, work in progress by stage, copies whose original was revised, machines without a procedure, and drafts checked out to devices.
- **Scans:** marked-up and signed paper copies (PDF or photo) are kept with each revision.

### Working offline

- A procedure that is **checked out** to a device works without a connection: points, photos, markup and details are saved on the device and upload automatically when the server can be reached. The indicator at the top shows *All saved*, *Offline · N waiting*, *Uploading…* or *problems*; tap it for details.
- **Phones and tablets check out a draft automatically** when it's opened for editing. On a PC, use **Make available offline**.
- While checked out, other devices see who has it and can't edit it. The same person can **take it over** on another device, and an Admin can **release** anyone's. Changes that hadn't uploaded from the old device are then refused and listed on that device.
- **Done editing – release** (or **Mark ready for review**) uploads and frees the procedure.
- Checking, printing, copying and editing machine details need a connection.
- On plain HTTP (the closed-Wi-Fi setup), keep the page open while out of range: edits still save on the device and upload later, but browsers only let the app *reopen* without a connection on HTTPS.

### Photo markup

**✎ Mark up** on a photo adds arrows, circles, boxes and text labels (red, yellow, white, black). The markup is stored separately and drawn onto the printed copy; the original photo is never changed. When a photo is retaken, the markup of another photo of the same point can be copied onto it.

### Accounts

| Access | Can do |
|---|---|
| Admin | Everything, plus users, sites and departments |
| Full access | Create, edit, copy and print procedures |
| Read-only | View and print the **Active** revision only (drafts, history, scans and draft photos are hidden) |

## Generate a procedure from JSON (no app)

```bash
.venv/Scripts/python tools/make_sample_photos.py
.venv/Scripts/python -m lotowriter generate samples/sample_procedure.json --out out
```

Output: `out/02-PROD-10045-V00.docx` and `.pdf`. The sample uses fictional data and covers all 8 legend energy types.

## Files

| Path | Purpose |
|---|---|
| `EHS-003a LOTO Machine-Specific Energy.docx` | EHS's original form (source) |
| `tools/build_template.py` | Turns the original form into the tagged template |
| `templates/EHS-003a_v1.docx` | Tagged template used by the generator (can be edited in Word) |
| `config/energy_types.json` | Energy Source Legend: names, fill, pattern, text color, units |
| `lotowriter/generate.py` | Builds the document: fields, numbering, review-date default, photos, shading, watermark |
| `lotowriter/photos.py` | Photo rotation fix, metadata/GPS removal, print sizing |
| `lotowriter/pdf.py` | DOCX → PDF (LibreOffice, or Word on Windows) |
| `lotowriter/db.py` | SQLite schema, upgrades, starter data (site 02, PROD/WH/FAC, admin) |
| `lotowriter/auth.py` | Passwords (scrypt), sessions, lockout after 10 failed sign-ins |
| `lotowriter/service.py` | Business rules: numbering, one procedure per asset, edits, photos, completeness, copy, offline sync |
| `lotowriter/workflow.py` | Review/approval steps, revisions, supersede, scans, periodic reviews, dashboard |
| `lotowriter/web/app.py` | HTTP API |
| `lotowriter/web/static/` | Browser app (plain JavaScript, no build step) |
| `lotowriter/web/static/data.js` | Offline data layer: device copies, upload queue, sync |
| `lotowriter/web/static/annotate.js` | Photo markup editor and overlay |
| `lotowriter/web/static/sw.js` | Service worker: app opens offline, photos cached |
| `deploy/` | Linux install guide, systemd service, Caddy (HTTPS), nightly backup |
| `samples/sample_procedure.json` | Example input showing every field |
| `tests/` | `python -m pytest` |

## When EHS revises the form

1. Save the new form next to the old one.
2. `python tools/build_template.py "<new form>.docx" templates/EHS-003a_v2.docx`
   The script stops with an error if a label it relies on has moved or been renamed.
3. Open the new template in Word, check the tags, run the tests, and generate the sample.

## Changes from the original form layout

- **Description columns rebalanced:** labels narrower, value boxes wider (the original value boxes were ~0.9" wide). The table width and the numbered rows below are unchanged. Use `--keep-original-widths` to turn this off.
- **Isolation table** split into its own table so its header row repeats on every page and no point row is split across pages.
- **Footer** gains a line with the procedure number and status. Effective date, review date and owner come from each procedure.
- **Control information** prints in 11 pt Calibri with bold labels, instead of the 14 pt bold serif text used in the form's example, so more points fit per page.
