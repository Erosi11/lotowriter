# LotoWriter — Project Plan

Digitizing creation of machine-specific Lockout/Tagout procedures on form **EHS-003a — Machine-Specific Energy Control Procedure**.

> **Scope boundary (important):** LotoWriter is a *drafting and documentation* tool. It does not approve procedures, and nothing it produces is valid for use until trained personnel have reviewed the printed draft and the approval has been recorded. The design below enforces that boundary in the software (status tracking, DRAFT watermarking, revision control) rather than relying on people remembering it.

---

## 1. Goals and non-goals

**Goals**
- Capture machine info, isolation points, energy types/magnitudes and photos at the machine, on a phone/tablet or PC.
- Work on the plant floor with poor or no Wi-Fi.
- Generate an EHS-003a document that matches the existing Word template exactly (layout, header/footer, legend colors).
- Keep a revision history per machine so procedures can be found, re-printed, revised and periodically re-inspected.

**Non-goals (v1)**
- Electronic approval/e-signature (review stays paper-based, per current process).
- Replacing the CMMS/asset system — LotoWriter may *import* asset data but is not the system of record for assets.
- Live lock tracking (who has a lock on what right now).

---

## 2. What the EHS-003a template actually contains

Analysis of `EHS-003a LOTO Machine-Specific Energy.docx` (Letter, portrait, 2 tables, 3 embedded example images):

| Template section | Data needed | Notes |
|---|---|---|
| Header | Form ID, title | Static |
| **Description** | Facility/Site, Department, Machine/Equipment, Tasks Covered, Energy Types & Magnitudes Involved, # of Locks Required | Machine/Equipment = **Asset # + Machine Type** (e.g. "10045 – Die Cutter"). OEM and Model are stored in the app but **not printed** |
| Notify Affected Employees | List of roles/positions | Pick-list + free text |
| Shutdown Equipment | Reference to shutdown procedure | Free text / document reference |
| Isolate All Energy Sources | Ordered table: **Photo · Energy Type & Magnitude (color-coded) · Energy Control Information** | Order = shutdown/isolation sequence (template states this explicitly) |
| Energy Control Information | Isolation device location & description, de-energization method, lock info, stored energy release/restraint, zero-energy verification | Should be structured fields, rendered as text |
| Apply Lockout/Tagout Devices | Device types needed; group lockout considerations | Pick-list (ball valve cover, plug cover, breaker lock, gate valve cover, hasp…) + free text |
| Release or Restrain Stored Energy / Verify Isolation | Free text | Can be auto-summarized from point data |
| Release from Lockout | Restart steps | Ordered list |
| Footer | Page X of Y, Document Effective Date, Review Date, Document Owner | **Entered by the user per procedure** (effective date, review date [defaults to effective + 1 yr, editable], document owner); page numbering automatic |

**Energy Source Legend (cell shading in the template):**

| Energy type | Fill | Pattern | Text color | Also covers |
|---|---|---|---|---|
| Electrical | Red `#FF0000` | solid | black | Stored electrical (capacitors, VFD DC bus, batteries/UPS) |
| Chemical | Yellow `#FFFF00` | solid | black | |
| Mechanical | Plum `#993366` | solid | black | Gravity/suspended loads, springs, rotating inertia |
| Pneumatic | Green `#008000` | solid | black | |
| Hydraulic | Blue `#0000FF` | solid | white | |
| Steam/Hot Water | Blue `#0000FF` | thin diagonal stripe, red `#FF0000` | white | |
| Start/Stop Switch | Grey `#C0C0C0` | diagonal crosshatch, white `#FFFFFF` | black | |
| Thermal | Orange `#FF6600` | solid | black | |

The legend is fixed at these **8 types**. Vacuum and gas aren't included. Each type is stored as a configuration entry (fill, pattern, pattern color, text color, units, and the conditions it covers, listed so authors can pick the right category) and is written to Word as the same `w:shd` value/color/fill the template uses, so generated cells match the legend exactly.

---

## 3. Recommended architecture

```
 ┌──────────────────────────────┐        ┌──────────────────────────────────┐
 │  Progressive Web App (PWA)   │  sync  │  API server                      │
 │  phone / tablet / PC browser │◄──────►│  - REST API + auth               │
 │  - offline drafts (IndexedDB)│        │  - database (SQLite)             │
 │  - camera capture + markup   │        │  - photo storage (local disk)    │
 │  - image compression         │        │                                  │
 └──────────────────────────────┘        │  - document generator:           │
                                         │    DOCX template fill → PDF      │
                                         │  Linux PC on the plant network   │
                                         └──────────────────────────────────┘
```

**Why a PWA:** one codebase covers PC and mobile, installs to the home screen, has camera access, and works offline via a service worker. No app-store deployment or MDM packaging needed. A native app is only worth it if you later need features like barcode scanning in rugged-device SDKs or hazardous-area-certified devices with locked-down browsers.

**Suggested stack** (adjust to what your IT group supports):

| Layer | Choice | Reason |
|---|---|---|
| Front end | React or Vue + Vite, PWA plugin, IndexedDB (Dexie) | Offline-first drafts |
| Back end | Python FastAPI | Best library for filling Word templates is Python |
| DB | SQLite (single file on the Linux PC); move to PostgreSQL only if usage outgrows it | No separate DB server to maintain |
| Doc generation | **docxtpl** (Jinja tags inside the real EHS-003a .docx) | EHS can keep editing the template in Word |
| PDF | LibreOffice headless (`soffice --convert-to pdf`) | Faithful DOCX→PDF on a server |
| Auth | Local user accounts in the app (decided; see §5.7): one built-in `admin`, plus **Full access** and **Read-only** accounts | Ties revisions to real people without depending on IT systems |
| Hosting | **Linux PC on the plant network** (decided) | Data stays on site |
| Photos | **Local disk on the Linux PC** (decided), e.g. `/var/lib/lotowriter/photos/` | See §3.1 |

**Template strategy (key design choice):** Instead of re-creating the form in code, copy `EHS-003a…docx` into a `templates/` folder and insert placeholder tags (`{{ machine.department }}`, a `{%tr for point in points %}` row loop, `{{ point.photo }}` image tags, cell shading driven by energy type). EHS keeps ownership of the look of the form, and a new template revision is just a new uploaded file.

### 3.1 Self-hosting on the Linux PC

- **Services:** run the app with systemd (or Docker Compose) behind **nginx** or **Caddy** as a reverse proxy. Install LibreOffice headless and the fonts used by the template (Calibri/Arial, or metric-compatible substitutes like Carlito/Liberation) so PDFs match Word's layout.
- **HTTPS is required, not optional.** Browsers only allow offline mode (service workers) and dependable camera access on HTTPS pages. Plain `http://192.168.x.x` will break offline drafting. Options, best first:
  1. A certificate from company IT's internal certificate authority for a DNS name such as `lotowriter.plant.local`.
  2. Caddy's built-in internal CA, with its root certificate installed on every phone, tablet and PC that uses the app.
  3. A public domain and Let's Encrypt using a DNS challenge. This works even when the PC isn't reachable from the internet.
- **Fixed address:** give the PC a DHCP reservation or static IP and a DNS name. If the address changes, installed copies of the app lose track of the server.
- **Storage layout:** `/var/lib/lotowriter/{db,photos/originals,photos/display,generated,scans,templates}`. Files are named by GUID, the folders are writable only by the service account, and the web server never serves them directly; they go through the authenticated API.
- **Backups (critical, because all data lives on one PC):** nightly backup of the SQLite database (using SQLite's `.backup` command, not a plain file copy of a live database) plus the photo, scan and template folders, to a **different machine or network share**. Keep 30 days, use `restic` or `rsync` with snapshots, and test a restore before the pilot. Recovery target: no more than one day of lost work.
- **Disk:** about 0.5–1 MB of display photos per point plus originals of about 3–5 MB, so roughly 50–100 MB per procedure. 500 procedures fit in about 50 GB; start with a 250 GB+ SSD and add a disk-space alert at 80%.
- **Power and uptime:** a UPS for the PC, the app set to start automatically on boot, and a simple health-check page. Because drafts are saved on the devices, phones and tablets keep working if the server is down; they sync when it's back.
- **Security:** firewall allowing only 443 (and SSH from admin machines), automatic OS security updates, and no exposure to the internet.
- **Access when off the plant network:** none planned; devices must be on plant Wi-Fi or VPN to sync.

---

## 4. Data model (v1)

```
Site            (id, name)
Department      (id, site_id, name)
Asset           (id, site_id, department_id, asset_number, oem*, model*, serial*,
                 machine_type, description, location_text, status[active/retired])
                 -- * stored for search/reference only; not printed on EHS-003a
Procedure       (id, asset_id UNIQUE /* 1 procedure per asset */, procedure_number /* SITE-DEPT-ASSET, locked */,
                 copied_from_revision_id (nullable), current_revision_id)
Revision        (id, procedure_id, rev_no, status, created_by, created_at,
                 tasks_covered, affected_roles[], shutdown_reference,
                 device_types[], group_lockout_notes, restart_steps[],
                 locks_required (calculated + override), change_reason,
                 approved_by_name, approved_date,
                 effective_date, review_date, document_owner,   -- user-entered, printed in footer
                 -- review_date defaults to effective_date + 1 year; user-editable
                 template_version)
IsolationPoint  (id, revision_id, sequence, label (e.g. "E-1"), copy_status [n/a | needs_check | confirmed | replaced],
                 energy_type, magnitude_value, magnitude_unit, magnitude_text,
                 device_type, device_location, device_description,
                 deenergize_method, lock_info, locks_at_point,
                 stored_energy_method, verification_method,
                 shared_point_ref (nullable), notes)
Photo           (id, isolation_point_id | revision_id, original_file, display_file, copied_from_photo_id,
                 annotations_json, taken_at, taken_by, width, height, caption)
EnergyType      (code, name, fill_color, shd_pattern, pattern_color, text_color,
                 default_units[], includes_text, active)   -- config, 8 fixed types
User            (id, username, display_name, role [admin | full | read_only], password_hash,
                 must_change_password, active, last_login)
AuditLog        (who, when, entity, action, before/after)
```

### 4.1 Procedure numbering — `SITE-DEPT-ASSET-REV`

| Part | Rule | Example |
|---|---|---|
| SITE | `02` = Villa Rica (seeded at install; Admin can add more sites, 2–6 uppercase letters/digits). Stored as text so the leading zero is never dropped | `02` |
| DEPT | `PROD` = Production, `WH` = Warehouse, `FAC` = Facility (seeded at install; Admin can add more) | `PROD` |
| ASSET | Asset # as entered; any `-` replaced with `_` so the number parses cleanly (recommended) | `10045` |
| REV | `V` + 2 digits, `V00` = first revision (decided) | `V02` |

Full example: **`02-PROD-10045-V02`**

- The number is generated by the app, not typed in, and is unique across the database.
- **SITE-DEPT-ASSET is locked when the first revision is created.** If the asset later moves to another department, the procedure keeps its number (search still finds it by its current department). Otherwise every printed copy and reference would point to a number that no longer exists. The alternative, re-numbering on the next revision, is available if EHS prefers.
- **One procedure per asset** (decided), so SITE-DEPT-ASSET identifies the procedure on its own and no suffix is needed. The database enforces this: creating a second procedure for an asset is blocked, and the app opens the existing one instead.
- V99 is the maximum; if it is ever reached, the field widens to 3 digits.
- The number prints in the header area of the form and in the generated filenames, e.g. `02-PROD-10045-V02.pdf`.
- REV only goes up when a revision is **approved** or a new revision is started from an Active one. Drafts returned with markups keep the same REV; the draft's status and print date tell the printouts apart.

**Revision status workflow**

```
Draft ──► Ready for Review ──► Printed for Review ──► Approved ──► Active ──► Superseded
  ▲                │                    │
  └──── Returned with markups ◄─────────┘
```

- Everything before **Active** prints with a diagonal **"DRAFT – NOT FOR USE"** watermark and the revision/status in the footer.
- **Approved** requires recording reviewer name(s), date, and (recommended) uploading a scan of the signed paper copy.
- Activating a revision automatically supersedes the previous one; superseded revisions stay read-only and retrievable.

---

## 5. Core features — data-entry interface

### 5.1 Machine / asset information
- Search existing assets by Asset #, OEM, model, department; or create new.
- Optional: CSV/Excel import from the CMMS asset list so most fields are pre-filled.
- Optional: scan the asset tag barcode/QR with the device camera to jump to the asset.
- Fields: Asset #, OEM, Model, Serial, Equipment Type, Department, Site, Location, Tasks Covered.
- Duplicate check on Asset # within a site.

### 5.2 Isolation point wizard (the heart of the app)
One screen per point, designed for one-handed use, gloves, and bright/dim lighting:
1. **Energy type** — big color-coded buttons that match the legend.
2. **Magnitude** — numeric value + unit chosen from the energy type's unit list (Electrical: VAC/VDC/phase; Pneumatic/Hydraulic: PSI/bar; Thermal/Steam: °F/°C; etc.), with a free-text fallback.
3. **Isolating device** — type (disconnect, breaker, valve, plug, …) + location + description.
4. **Photo(s)** — capture, then annotate (arrow/circle/label on the actual device).
5. **Control steps** — guided fields: de-energize method → lock info → stored-energy release → zero-energy verification. Each has suggested phrasing templates per energy type that the author edits (never auto-filled silently).
6. **Locks at this point.**

Point list screen: drag-to-reorder (sequence = isolation order), duplicate a point, mark a point as "shared with another asset".

### 5.3 Procedure-level sections
- Affected roles (multi-select from site list + free text).
- Shutdown reference, device types needed, group lockout notes, restart steps (ordered list).
- **# of Locks Required**: auto-sum of points, editable with a required reason if overridden.
- **Energy Types & Magnitudes Involved**: auto-generated summary from points (e.g., "Electrical 480 VAC 3Ø; Pneumatic 95 PSI").

### 5.4 Completeness check before "Ready for Review"
Blocking checks: at least one isolation point; every point has energy type, magnitude (or explicit "unknown – verify"), device location, verification method, and a photo (or a recorded reason for no photo); at least one Electrical or Start/Stop entry, or an explicit acknowledgment that none applies.
Warnings (non-blocking): only one energy type listed; no stored-energy step on a Hydraulic/Pneumatic point; very short text fields.

The check is a completeness aid, not a technical validation — the printout still goes through human review.

### 5.5 Search, reports, admin
- Find procedures by asset, department, status, due-for-review date.
- Dashboard: assets without a procedure, drafts pending review, procedures due for periodic review.
- Admin: sites, departments, roles list, energy types/colors/units, device types, template upload/versioning, user accounts (see §5.7).

### 5.6 Copy from Similar Machine

For runs of similar machines, the author builds one procedure carefully, then copies it to each similar machine and only changes what's different.

**Flow**
1. Open a source procedure (any status; usually an Active one) and choose **Copy to similar machine**.
2. Pick one or more target assets, or create them right there (Asset #, Machine Type, Department). Assets that already have a procedure are not offered as targets.
3. Choose what to copy, all checked by default: description fields (tasks, affected roles, shutdown reference, device types, group lockout notes, restart steps), isolation points with their control text, and photos.
4. The app creates one new **V00 Draft** per target, numbered from the target asset (e.g. `02-PROD-10046-V00`). Footer dates and owner are pre-filled from the source; the Review Date default is recalculated.
5. **Asset # replacement:** every place the source Asset # appears in copied text is replaced with the target's, and each replacement is highlighted so the author can see it. Other numbers that often differ between machines (breaker/circuit numbers, panel IDs, valve tags) are **highlighted for checking but not changed**.
6. **Update checklist at the new machine.** Each copied point shows as "Copied – needs check". At each point the author either:
   - **Retakes the photo** (camera opens straight to that point; the annotations from the copied photo can be re-applied and adjusted), or
   - **Confirms the copied photo is accurate for this machine** (deliberate tap on each point, recorded with user and time).

   The author also confirms or edits the magnitude, device location and lock info. The point list shows progress, e.g. "4 of 7 points checked".
7. **Ready for Review is blocked** until every copied point is checked. This is the main safety control on copying: nothing reaches review without someone confirming it at the actual machine.

**Behind the scenes**
- Copied photos point to the same stored file until retaken, so copying 10 procedures doesn't create 10 copies of every photo.
- The procedure records which revision it was copied from. This appears in its history and in the "copies of this procedure" list on the source; it is not printed on the form.
- Works offline: the source procedure and its photos are downloaded to the device first, so the author can copy and check at machines without Wi-Fi.
- Copying requires a Full access or Admin account.

### 5.7 User accounts

| Role | Can do |
|---|---|
| **Admin** (built-in `admin` account) | Everything Full access can do, plus manage user accounts, sites/departments, energy-type settings and form templates |
| **Full access** | Create, edit, copy, print, record approvals, revise procedures |
| **Read-only** | Search, view and print Active procedures only (drafts are hidden so they can't be mistaken for approved procedures) |

- First install creates `admin` with password **`Password1`**, flagged **must change on first login**. The app won't do anything else until the password is changed.
- **Do the first login right after installation**, before announcing the server's address. Until then, anyone on the plant network who knows the default could take over the admin account.
- Admin adds accounts with a temporary password; each new user must also change it on first login. Admin can reset passwords and deactivate accounts (never delete them, so the history still shows who did what).
- Password rules: at least 8 characters, not `Password1`, stored as salted hashes (argon2/bcrypt). Lock out for 15 minutes after 10 failed attempts.
- Keep at least one active Admin at all times; the last Admin can't be deactivated or downgraded.
- **Offline use:** a device keeps the user signed in for a set time (e.g. 14 days) so drafting works without Wi-Fi. Deactivating an account takes effect the next time that device syncs.
- Recovery if the only Admin password is lost: a command-line reset run on the Linux PC (`lotowriter reset-admin`), documented in the admin guide.

---

## 6. Photo handling

| Concern | Approach |
|---|---|
| Capture | `<input type="file" accept="image/*" capture="environment">` for broad support; optional in-app camera view with flash/torch toggle |
| Formats | Accept JPEG, PNG, HEIC (iPhone). Convert HEIC → JPEG server-side (pillow-heif) |
| Orientation | Apply EXIF rotation, then strip it, so photos never print sideways |
| Size | Keep the **original** for the record; create a **display** copy ~1600 px long edge, JPEG q≈80 (≈200–400 KB) client-side before upload to save mobile data |
| Offline | Store in IndexedDB with the draft; upload queue with retry; show per-photo sync status |
| Privacy | Strip GPS/location EXIF; keep capture timestamp and device/user in the DB. Warn the author to avoid people's faces |
| Annotation | Arrows/circles/text stored as vector JSON over the original (non-destructive), rendered into the print image at generation time |
| Print fit | Crop/fit to the template's photo cell aspect ratio; let the author choose crop. Allow 1–2 photos per point (e.g., wide "where is it" + close-up "what is it") |
| Quality | Warn on very dark or blurry images (simple brightness/Laplacian variance check) |
| Storage | Local disk on the Linux PC (`/var/lib/lotowriter/photos/`). Files named by GUID, referenced from DB, never by user-supplied filename; served only through the authenticated API. Included in nightly backup (§3.1) |

---

## 7. Form generation

1. Load the EHS-003a template version recorded on the revision (so old revisions re-print identically).
2. Fill header/description fields. Machine/Equipment = `{asset_number} – {machine_type}`. OEM/Model are not printed.
3. Loop isolation points into table rows in sequence order:
   - Photo cell: annotated display image sized to the cell width.
   - Energy cell: "Pneumatic – 95 PSI", cell shading from the EnergyType config.
   - Control info cell: structured fields rendered as short labeled lines or bullets.
4. Mark rows "can't split across pages" and repeat the table header row on each page.
5. Footer: user-entered Document Effective Date, Review Date and Document Owner; plus revision number, status, page X of Y; watermark if not Active.
6. Output **DOCX** (editable fallback) and **PDF** (what gets printed). Store the generated PDF with the revision so "what was printed" is preserved exactly.
7. Optional: QR code on the form linking back to the procedure record (helps reviewers and later revisions).

Testing: golden-file tests comparing generated PDFs (rendered to images) against approved samples for 1-point, 5-point and 25-point procedures.

---

## 8. Edge cases to plan for

### Machine & procedure data
- **Long machine-type names**: Machine/Equipment is a single line. Limit its length or let it wrap without breaking the layout.
- **Duplicate or changed Asset #s** (re-tagging, CMMS migration, asset moved between departments/sites). Keep an internal ID; Asset # is an editable attribute with history.
- **Similar machines**: handled by Copy from Similar Machine (§5.6). Watch for:
  - **Look-alike but different.** Same model, but a different breaker number, panel, air drop or voltage (older vs. newer builds, retrofits). This is why every copied point must be checked at the new machine.
  - **Mirror-image machines** (left-hand vs. right-hand): photos and "left/right" wording are wrong if kept.
  - **The source procedure changes later.** Copies don't update automatically. When a procedure that others were copied from gets a new revision, list its copies so the user can decide whether they need the same change.
  - **A copied photo kept by mistake.** The author must confirm each photo point by point; there is no "accept all" button.
  - **A shared isolation point** (one disconnect feeding two machines): copying is correct here, and the point should be marked shared on both.
- **Lines or cells covered by one procedure**: not supported. One procedure per asset; a line is handled as separate asset procedures, or as one asset number for the whole line if that's how the CMMS records it.
- **Second procedure for the same asset** (e.g., a different task): not allowed. Cover additional tasks under "Tasks Covered" in the asset's one procedure.
- **Shared isolation points** — one disconnect or air header feeding several machines. Isolating it affects other equipment; record the shared relationship and list impacted assets.
- **Machine modified/retrofitted** — the existing procedure becomes stale. Provide a "flag for revision" action and require a change reason on every new revision.
- **Retired/relocated equipment** — retire the procedure, don't delete it.

### Energy & isolation points
- **Energy that doesn't obviously fit a category**: gravity, springs and inertia go under Mechanical; capacitors, VFD DC bus and batteries/UPS go under Electrical. Show these "also covers" notes on the energy-type buttons, and require the description to name the specific hazard (e.g., "Mechanical – Gravity: raised platen, 2,000 lb"). Vacuum and gas are not types. If a machine has them, the author records them under the closest category with a clear description, and reviewers decide on paper.
- **Multiple energy types at one point** (e.g., a valve isolating hot water under pressure; electrical panel with a stored-energy capacitor). Allow multiple energy entries per point or split into separate points.
- **Hydraulic vs. Steam/Hot Water, and Start/Stop**: these are told apart by pattern (red stripe, white crosshatch), not just fill color. The generator must write the pattern into the cell shading, not only the fill, and the energy type is always printed as text too.
- **Black-and-white printing**: Hydraulic, Steam and Electrical all become similar dark greys, so the energy-type text in the cell is mandatory. Recommend printing in color for review.
- **Unknown magnitude** — allow "Unknown – verify with maintenance" with a review-blocking flag, rather than forcing a guess.
- **Units & symbols** — 480 VAC 3Ø, 24 VDC, °F, ±; make sure fonts and generation handle Unicode.
- **Sequence matters** — reordering must be explicit and visible; the printed order must equal the stored order.
- **Stored/residual energy** steps (bleed, block, discharge, wait time) separate from isolation.
- **Points that can't be locked directly** (no lockable device; tagout-only). Capture as tagout with the additional-protection note.
- **Verification steps** (try-start, meter test, gauge to zero) — every point needs one; a "Start/Stop Switch" try-step often appears at the end.
- **Lock count** — group lockout, lockboxes, and points needing multiple locks make a simple sum wrong; allow override with reason.

### Photos
- **Camera-prohibited areas** (customer confidentiality, clean rooms) or hazardous (classified) locations where phones aren't allowed — allow "photo to be added later" or upload from an approved camera on a PC.
- **Unsafe to photograph** (inside an energized panel). Photo should show the *isolating device*, taken from a safe position; include a reminder on the capture screen.
- **Poor lighting, glare, lens grime, oily fingers** — quality warning + retake.
- **Device in the photo doesn't match the label** — annotation helps; reviewers check this on paper.
- **Very large or many photos** — compression and per-revision limits so the PDF stays printable (target < 10 MB).

### Field conditions & devices
- **No Wi-Fi in the plant / dead zones** — full offline drafting; never lose data on refresh or app close; clear "unsynced" indicator.
- **Two people editing the same draft** — lock a draft to one author (check-out), or detect conflicts on sync and require a manual merge.
- **Lost/broken device with unsynced data** — encourage sync at shift end; show how long since last sync.
- **Gloves, noise, small screens** — large touch targets, minimal typing, pick-lists, voice-to-text works via the phone keyboard.
- **Shared kiosk PCs** — short session timeouts and visible "logged in as".
- **Language**: English only for now (D4). UI text is kept in one strings file so other languages can be added later without rework.

### Review, revision & compliance
- **Paper markups** — reviewers will write corrections on the printout. Workflow: "Returned with markups" status, upload a scan of the marked-up copy, author edits, new draft prints. Keep the scan with the revision.
- **Someone uses a draft on the floor** — the DRAFT watermark and status in the footer prevent mistaking an unapproved printout for an active procedure.
- **Old printouts still posted** after a new revision is approved — print revision number and effective date prominently; optional QR to check current status.
- **Periodic inspection** — OSHA 29 CFR 1910.147(c)(6) requires periodic (at least annual) inspection of energy control procedures. Track next-review dates and surface overdue items. The Review Date defaults to Effective Date + 1 year and drives the review-due dashboard. If the user changes the Effective Date and hasn't edited the Review Date, the Review Date moves with it; once they've edited it by hand, it stays as entered. Warn if the Review Date is more than 1 year out or earlier than the Effective Date.
- **Template changes** — when EHS revises EHS-003a, existing procedures keep their template version until revised; new drafts use the new one.
- **Record retention & audit trail** — who entered what and when; never hard-delete approved revisions.
- **Liability boundary** — the app should present its suggested text as suggestions only; no "auto-generated procedure" language anywhere.

---

## 9. Phased delivery plan

| Phase | Duration (1–2 devs) | Deliverables |
|---|---|---|
| **0. Discovery** | 1–2 wks | Set up the Linux PC (HTTPS cert, hostname, backups); collect 3–5 real completed EHS-003a examples; walk the floor with authors |
| **1. Template + generator spike** ✅ *built, awaiting EHS print review* | 1–2 wks | Tagged EHS-003a template; script that turns a JSON sample into DOCX/PDF with photos, colors, multi-page tables. Validate printout with EHS **before** building UI |
| **2. MVP (online)** ✅ *built* | 4–5 wks | Asset entry, isolation point wizard, photo upload/compress, procedure sections, completeness check, DRAFT generation, user accounts (admin / full / read-only, forced password change), **Copy from Similar Machine** |
| **3. Offline & mobile hardening** ✅ *built; HTTPS/LibreOffice to verify on the Linux PC* | 2–3 wks | Service worker, IndexedDB drafts, upload queue, HEIC/rotation handling, photo annotation, gloves-friendly UI pass |
| **4. Workflow & revisions** ✅ *built* | 2 wks | Status workflow, approval recording + scan upload, revision history, supersede, review-due dashboard, audit log |
| **5. Pilot** | 3–4 wks | One department, ~10–20 machines; compare effort and quality vs. paper/Word process; fix issues |
| **6. Rollout** | ongoing | Remaining departments/sites, CMMS import, training material, backup/restore drill |

Rough MVP-to-pilot total: **~3–4 months** part-time for one developer, faster with two.

---

## 10. Decisions needed before building

| # | Decision | Options |
|---|---|---|
| ~~D1~~ | **Decided:** Machine/Equipment = Asset # + Machine Type; OEM/Model not printed | |
| ~~D2~~ | **Decided:** Footer Effective Date, Review Date and Document Owner are entered by the user per procedure. Review Date defaults to Effective Date + 1 year (matches the annual inspection); the user can edit it | |
| ~~D3~~ | **Decided:** Keep 8 legend types. Gravity → Mechanical, stored electrical → Electrical, no vacuum/gas | |
| ~~D4~~ | **Decided:** English only (UI and printed form) for now | Keep UI text in a strings file so translation can be added later |
| ~~D5~~ | **Decided:** Self-hosted on a Linux PC; photos stored on its local disk | See §3.1 |
| ~~D6~~ | **Decided:** `SITE-DEPT-ASSET-REV`, revision format `V00`, one procedure per asset | See §4.1 |
| ~~D7~~ | **Decided:** Local accounts: built-in `admin` (initial password `Password1`, must change on first login); admin adds Full access or Read-only accounts | See §5.7 |
| ~~D8~~ | **Decided:** Copy a procedure from a similar machine, then update photos and asset number | See §5.6 |

Department codes (decided): `PROD` Production, `WH` Warehouse, `FAC` Facility.

Site code (decided): `02` = Villa Rica.

All planning decisions are now made.

---

## 11. Success criteria for the pilot
- Time to create a procedure (walk-down to printed draft) reduced vs. current process.
- ≥ 95% of printed drafts need no layout corrections (only content corrections from review).
- Zero lost drafts due to connectivity.
- Reviewers rate the printout as at least as easy to review as the current Word-based form.
