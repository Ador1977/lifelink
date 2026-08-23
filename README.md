# LifeLink — AI-Powered Blood Donation Platform with Urgency Detection

A full working web application built from your CSE299 project proposal
(*AI-Powered Blood Donation Platform with Urgency Detection and Donor
Health Screening*, North South University).

To make it something **anyone can run on any PC in a few minutes**, it uses:

| Proposal said | This build uses | Why |
|---|---|---|
| Django | **Flask** | Much simpler to set up, same Python-based logic |
| MySQL | **SQLite** (built into Python) | No database server to install — the whole database is one file |
| Scikit-learn / Pandas | **Scikit-learn** | Kept exactly as proposed |

Everything else (registration/login, blood requests, AI urgency detection,
AI donor health screening, blood-group matching, admin verification) is
implemented and working.

---

## ✅ What's included

- **User registration & login** for Donors, Patients, and Admins
- **Blood request creation** with hospital, blood group, units, and timeframe
- **AI Urgency Detection** — a trained scikit-learn model scores every
  request as `Critical / High / Medium / Low` in real time and the
  triage board auto-sorts by urgency
- **AI Donor Health Screening** — a trained scikit-learn model checks
  hemoglobin, blood pressure, weight, pulse, and age against safe
  donation thresholds and explains *why* someone is or isn't eligible
- **Blood group compatibility matching** — donors automatically see
  requests that match their blood type
- **AI Donor Recommendation System** — a trained model ranks the best
  nearby donors for every request, blending blood compatibility,
  proximity, reliability, and eligibility, weighted by request urgency
  (see "How the AI parts work" below)
- **Blood Donation History & Automatic Eligibility Tracking** — every
  donation is stored permanently; a donor's eligibility is always
  calculated automatically from their history (configurable waiting
  period: 90 days male / 120 days female), and compatible-but-in-waiting
  donors stay visible under "Currently Not Eligible" with a short reason
- **Blood Group Card Verification (AI)** — a donor uploads a photo of
  their blood-group / donor card and the local vision model
  (Ollama + Qwen2.5-VL) reads the group. Only the 8 real blood groups are
  ever stored; uncertain, conflicting, or blurry reads are never guessed
  — they are flagged "Verification Required". A verified group is never
  overwritten automatically, the original image is kept for later review
  (owner/admin only), and flagged/unverified donors are excluded from
  AI matching until confirmed (Admin → Review Blood Groups)
- **Bulk Donor Import (Admin)** — an admin pastes a donor roster (the
  kind of plain-text / OCR list a blood-donation group shares on
  Facebook) or a CSV/TSV table into **Admin Panel → Import Donors**. The
  parser is OCR-tolerant (`0+` → `O+`, phone numbers like `01710-027589`,
  page markers like `25/25`, merged rows, footers) and shows an editable
  preview before anything is written to the database. Only rows an admin
  checks and fixes are imported; duplicates (same phone/email) are
  skipped, imported donors get a synthetic
  `donor.<phone>@import.lifelink` email, default password `Donor@1234`,
  and a verified blood group that immediately qualifies them for
  AI donor matching
- **Profile location with auto-detected coordinates** — on their Profile a
  user can either press **"Use My Location"** (the browser asks for GPS
  permission) or **search/select** their area from the built-in Bangladesh
  location list. Either way the matching **latitude/longitude are detected
  and stored automatically** (validated server-side) — no manual entry.
  If GPS permission is denied, the manual search/select still works. The
  saved location and its coordinates are displayed on the profile, and
  changing the location always updates the coordinates. These stored
  coordinates are exactly what the AI recommendation system uses to rank
  **closer, eligible donors higher** for every request
- **Admin panel** — verify/fulfill/cancel requests, manage donor
  availability, view platform-wide stats, adjust the donation waiting
  period, browse the donation ledger and each donor's history, review
  flagged blood-group cards, and bulk-import donor rosters
- **Notification-style dashboard** — each user sees relevant matching
  requests the moment they log in

The AI models train themselves automatically the first time you start
the app (takes about 1–2 seconds) using realistic synthetic
medical-triage data — no external dataset download needed.

---

## 📁 Files you received

```
blood_donation_platform/
├── app.py                  ← main Flask application (routes, database models)
├── ai_engine.py            ← the three AI models (urgency detection, health screening, donor recommendation)
├── recommendation_engine.py ← rule-based filter + ranking logic for donor recommendations
├── eligibility_engine.py   ← rule-based donation-history eligibility (waiting period, auto status)
├── blood_group_engine.py   ← rule-based blood-group validation / VLM reply parsing / never-guess decision logic
├── donor_import_engine.py  ← OCR-tolerant parser + validator for the admin bulk donor-import feature
├── location_engine.py      ← offline geocoding (GPS reverse-geocode, search/select, coordinate validation)
├── requirements.txt        ← list of Python packages needed
├── README.md               ← this file
├── test_recommendation_engine.py ← unit tests for the recommendation pipeline
├── test_eligibility_engine.py    ← unit tests for the eligibility engine
├── test_blood_group_engine.py    ← unit tests for blood-group extraction logic
├── test_donor_import_engine.py   ← unit tests for the bulk donor-import parser
├── test_location_engine.py       ← unit tests for the profile location/geocoding helpers
├── static/
│   ├── css/style.css       ← all styling
│   └── js/script.js        ← small UI behaviors
├── uploads/blood_groups/   ← stored blood-group card images (served to owner/admin only)
└── templates/              ← all the HTML pages
    ├── base.html
    ├── index.html
    ├── login.html
    ├── register.html
    ├── dashboard.html
    ├── create_request.html
    ├── requests_list.html
    ├── health_screening.html
    ├── recommendations.html ← full AI donor recommendation page per request (incl. "Currently Not Eligible")
    ├── donor_search.html    ← AI-ranked donor search
    ├── profile.html         ← donor profile incl. blood-group card upload & verification
    ├── admin_panel.html     ← admin dashboard (incl. donation settings)
    ├── admin_donations.html ← donation-history ledger for all donors
    ├── admin_donor_donations.html ← one donor's history + record-a-donation form
    └── admin_blood_groups.html ← blood-group card review queue
    └── admin_import_donors.html ← admin bulk donor-import page (paste → preview → import)
```

The `instance/` folder (containing the SQLite database file) is created
automatically the first time you run the app — you don't need to create
it yourself.

---

## 🚀 How to run it — step by step

### Step 1: Install Python
You need **Python 3.9 or newer**. Most Windows/Mac/Linux computers
already have it. Check by opening a terminal (Command Prompt / PowerShell
on Windows, Terminal on Mac/Linux) and typing:

```
python --version
```

If that doesn't work, try:

```
python3 --version
```

If neither works, download and install Python from **https://www.python.org/downloads/**
(on Windows, tick "Add Python to PATH" during install).

### Step 2: Open a terminal in the project folder
- **Windows**: open the `blood_donation_platform` folder in File Explorer,
  click the address bar, type `cmd`, and press Enter.
- **Mac**: right-click the `blood_donation_platform` folder → "New Terminal
  at Folder" (or open Terminal and `cd` into the folder).
- **Linux**: open a terminal and `cd` into the folder.

### Step 3: (Recommended) Create a virtual environment
This keeps the project's packages separate from the rest of your system.

```
python -m venv venv
```

Activate it:
- **Windows**: `venv\Scripts\activate`
- **Mac/Linux**: `source venv/bin/activate`

You'll know it worked because you'll see `(venv)` at the start of your
terminal line. (You can skip this step and just do Step 4 directly if
you prefer — it will still work.)

### Step 4: Install the required packages
```
pip install -r requirements.txt
```
This installs Flask, the database toolkit, login handling, and
scikit-learn. It only needs to be done once.

### Step 5: Run the application
```
python app.py
```

You should see something like:
```
============================================================
 AI-Powered Blood Donation Platform
 Starting server at: http://127.0.0.1:5000
 Demo logins:
   Admin   -> admin@bloodplatform.com   / admin123
   Donor   -> donor@bloodplatform.com   / donor123
   Patient -> patient@bloodplatform.com / patient123
============================================================
```

### Step 6: Open it in your browser
Go to: **http://127.0.0.1:5000**

That's it — the platform is running locally on that computer.

### Step 7: Stop the server
Go back to the terminal window and press `Ctrl + C`.

---

## 🔑 Demo accounts (already created for you)

| Role | Email | Password |
|---|---|---|
| Admin | admin@bloodplatform.com | admin123 |
| Donor | donor@bloodplatform.com | donor123 |
| Donor (recently donated — shows "Currently Not Eligible") | donor7@bloodplatform.com | donor123 |
| Patient | patient@bloodplatform.com | patient123 |

You can also click "Sign Up" to create new donor/patient accounts.

---

## 🧠 How the AI parts work (for your report/presentation)

**Urgency Detection** (`ai_engine.py` → `UrgencyDetector`)
- Input features: units of blood needed, patient condition severity (1–5),
  hours until needed, and age-related risk factor
- A `RandomForestClassifier` (scikit-learn) is trained on 1,200 synthetic
  but medically-reasonable triage cases and predicts one of
  `Critical / High / Medium / Low`, along with a confidence percentage
- Every new blood request runs through this model instantly and the
  triage board (`/requests`) automatically sorts by urgency level

**Donor Health Screening** (`ai_engine.py` → `HealthScreener`)
- Input features: hemoglobin, systolic BP, diastolic BP, weight, pulse,
  age — the values typically found in a basic blood/health report
- A `RandomForestClassifier` is trained on 1,500 synthetic health
  records labeled against standard safe-donation thresholds
  (e.g. hemoglobin ≥ 12.5 g/dL, age 18–65, weight ≥ 50 kg)
- The screening page explains exactly *which* parameter(s) caused a
  "not eligible" result, so it's transparent, not a black box

Both models retrain fresh every time the app starts (takes under 2
seconds), so there's no large model file to carry around — anyone can
clone or copy this project and it "just works."

**Donor Recommendation System** (`ai_engine.py` → `DonorRecommender` +
`recommendation_engine.py`)
- For every blood request, the platform finds donors with a *compatible*
  blood group, then ranks them with a three-layer pipeline:
  1. **Hard filters (rules)** — blood compatibility, donor availability,
     health-screening pass, and age 18–65. Donors who fail these are
     shown in the "excluded" list with the reason.
  2. **Donation-history eligibility (rules)** — the current waiting
     period (90 days male / 120 days female, configurable) is checked
     against the donor's recorded history. Donors still in the waiting
     period are **not excluded and not ranked as primary** — they appear
     in a separate "Currently Not Eligible" panel with their last/next
     donation dates and a short reason.
  3. **ML score + urgency blend** — a `GradientBoostingRegressor`
     (scikit-learn) turns normalized features (compatibility,
     reliability, distance, eligibility, donation experience,
     availability, recency, contact preference) into a 0–100 score,
     blended with distance using an urgency boost so Critical/High
     requests favor the nearest donor while Medium/Low requests favor
     the most reliable one; an exact blood-group match gets a small bonus
- The recommendation page (`/request/<id>/recommendations`) shows the
  ranked eligible donors with match-score bars, distance, donation
  history, next eligible date, and preferred contact method, plus the
  "Currently Not Eligible" panel and a panel explaining *why* excluded
  donors were filtered out
- Donors can set a preferred contact method and coordinates on their
  profile; every fulfilled request records a permanent donation that
  increases a donor's reliability score, donation count, and bag total

**Blood Group Card Verification** (`blood_group_engine.py` + the VLM in `app.py`)
- The donor uploads a photo of their blood-group / donor identity card;
  a local vision-language model (Ollama + Qwen2.5-VL) returns a strict
  JSON summary of the group, its confidence, and the card's name/ID
- The deterministic engine in `blood_group_engine.py` then enforces the
  safety rules: only the 8 real groups are stored, a single confident
  reading (≥ 60%) is accepted, and no-group / conflicting / low-confidence
  results are flagged "Verification Required" — the app never guesses
- Verified groups feed the recommendation system; unverified or flagged
  groups are hard-filtered out of matching (same layer as the
  availability/screening rules) until the donor or an admin confirms them

> **ML vs rules, clearly separated for your report:** the trained
> `GradientBoostingRegressor` produces the core match score, while
> eligibility, cooldown, compatibility, distance (Haversine), and the
> urgency boost are deterministic rules in `recommendation_engine.py`
> and `eligibility_engine.py`.

## 🩸 Donation History & Automatic Eligibility Tracking

- **Every donation is a permanent record** (`Donation` rows are never
  overwritten). Records are created when a request is marked Fulfilled,
  when a donor updates their last donation date on their profile, or
  manually by an admin.
- **Eligibility is always automatic** — it is computed from the donation
  history and the current date every time it is shown (profile,
  recommendations, search, admin pages, and the JSON APIs). There is no
  manual "mark eligible" toggle for the waiting period.
- **Configurable waiting period** — defaults to 90 days (male) / 120
  days (female) in `eligibility_engine.py`. An admin can change it in
  **Admin Panel → Donation Eligibility Settings**; the values are stored
  in the `AppSetting` table and every donor's eligibility recalculates
  immediately from the new guideline.
- **Status reasons are short and clear**, e.g.
  `Can donate again after 13 October 2026.` (plus
  `Only 5 days remaining before eligibility.` when the date is close).
- **Donor profile** shows a summary card (total donations, total bags,
  last donation, next eligible date, current status) plus the full
  donation history table.
- **Admin tools** — the donation ledger (`/admin/donations`), per-donor
  history pages with a record-a-donation form, and delete buttons that
  automatically recalculate eligibility.
- **JSON APIs** — `/api/donor/<id>/eligibility` and
  `/api/donor/<id>/donations` (owner or admin only).
- On startup, existing donors' old `donation_count`/`last_donation_date`
  values are backfilled into real history rows once, and a demo donor
  (`donor7@bloodplatform.com`) with a recent donation is seeded so the
  "Currently Not Eligible" flow can be seen immediately.

---

## 🩸 Blood Group Card Verification (AI)

A donor can upload a photo of their blood-group / donor identity card
from their **Profile → Blood Group Verification** panel. The local
vision-language model (Ollama + Qwen2.5-VL, same as the lab-report
uploads) reads the card and the app stores the result safely:

- **Only the 8 real groups are ever stored** (`A+/A-/B+/B-/AB+/AB-/O+/O-`,
  enforced by `blood_group_engine.py`). Anything else is rejected.
- **The app never guesses.** No group found, more than one conflicting
  group on the card, or a low-confidence read → the record is flagged
  **"Verification Required"** with a short reason and the donor is asked
  to confirm manually.
- **A verified group is never overwritten automatically.** If the card
  shows a different group than the donor's verified one, the account is
  flagged with "Confirm before updating" instead of silently changing it.
- **Cards are tied to the right account.** If the name printed on the
  card doesn't match the account name, the upload is flagged for manual
  review rather than being associated with the wrong user.
- **The original image is kept** under `uploads/blood_groups/` (never in
  the public `static/` folder) and served only to the owner or an admin
  via a protected route.
- **Only verified groups reach the recommendation system.** Donors whose
  group is still unverified or flagged are excluded from donor search
  and AI matching (with the reason "Blood group not verified yet" /
  "Blood group flagged for verification") until an admin or the donor
  confirms it.
- **Admin review queue** — **Admin Panel → Review Blood Groups** shows
  every flagged/unverified donor plus every card submission with a
  thumbnail of the card, the detected group, confidence, and card
  details, with **Confirm** (accept / override) and **Reject** (clear the
  flag, keep the current group) actions.

---

## 📍 Profile Location (auto-detected coordinates)

The profile's **Location** field now detects and stores the matching
**latitude/longitude automatically** — users never type coordinates:

- **"Use My Location"** — the browser's `navigator.geolocation` API asks
  for permission and the fix is sent to `POST /api/profile/location`,
  which reverse-geocodes it to the nearest known area name (e.g. a fix in
  central Dhaka is labelled "Dhaka"). If permission is denied, a friendly
  message explains that manual search/select still works.
- **Search/select** — typing in the field queries
  `GET /api/locations/search` (a debounced autocomplete over the built-in
  Bangladesh location table); picking a place fills in its coordinates
  instantly. Works fully offline, no API key.
- **Validation** — coordinates are strictly checked on the server
  (latitude −90…90, longitude −180…180) before they are saved, both via
  the API and on the normal "Save Changes" form.
- **Always in sync** — if the user changes their location text without
  picking a suggestion, the server resolves coordinates from the text on
  save, so latitude/longitude can never go stale.
- The saved location (name + coordinates) is displayed clearly on the
  profile.

**Why it matters for matching:** the recommendation engine already
computes a Haversine distance from the **patient/requester's stored
coordinates** to **each donor's stored coordinates** and blends proximity
into the rank (heavier for Critical/High requests), so a closer eligible
donor like "Donor A — 1.8 km" outranks "Donor B — 5.4 km" when the
urgency favors proximity. The profile location feature is what fills
those coordinates with real values instead of guesses.

---

## 🗺️ Google Maps

The platform can use the Google Maps API for three things. Every feature
**also works without a key** (it falls back to the built-in Bangladesh
location table and keyless map embeds), so the app runs offline first and
upgrades automatically once you add a key.

| Feature | Without a key | With `GOOGLE_MAPS_API_KEY` |
|---|---|---|
| **Hospital geocoding** | Hospital coordinates resolved from the offline area table on startup | Google Geocoding API resolves real hospital coordinates (lazily, cached) |
| **Request detail map** (`/request/<id>`) | Keyless embedded Google Map centered on the hospital + Get Directions / Open in Google Maps buttons | Maps Embed API *place* mode with a labeled map |
| **Donor search map** (`/donor-search`) | Static keyless map centered on your area | Interactive **Maps JavaScript API** map with a marker (and match score) for every matching donor |

### How to enable it

1. Get an API key from the [Google Cloud Console](https://console.cloud.google.com/)
   (enable the **Geocoding API**, **Maps Embed API**, and **Maps JavaScript API**;
   restrict the key by HTTP referrer to your server's host for safety).
2. Put it in `.env`:

   ```
   GOOGLE_MAPS_API_KEY=your_key_here
   ```

3. Restart the app. Hospital pages now show real Google Maps embeds and the
   donor-search page shows an interactive marker map.

The Google Geocoding API calls are cached in memory (`maps_engine.py`), so
repeated page loads don't re-bill. All helper code lives in `maps_engine.py`
and is unit-tested (`test_maps_engine.py`).

---

## 🧪 Running the tests

The recommendation engine, the eligibility engine, the blood-group
extraction logic, the bulk donor-import parser, and the profile-location
geocoding helpers each have a unit-test suite (129 tests in total)
covering all eight blood-group compatibility rules, distance
calculation, the configurable waiting period, automatic eligibility
transitions at the date boundary, the "currently not eligible" bucket,
urgency ordering, ML score bounds, blood-group validation /
normalization / conflict detection, VLM reply parsing, the never-guess
decision logic, unverified-donor exclusion, the import parser (phone
cleanup, OCR group normalization, noise/page-marker skipping, free-form
+ CSV parsing, error reporting, synthetic emails), and the location
engine (coordinate validation, GPS reverse-geocoding, search/select
autocomplete):

```
python -m unittest discover -s . -p "test_*.py" -v
```

---

## 📌 Possible next steps if you want to extend it further

- Replace synthetic training data with a real anonymized dataset if
  your instructor wants stronger evidence of model validity
- Add SMS/email notifications for donors near a critical request
  (e.g. via Twilio or an email API)
- Deploy it online (Render, Railway, PythonAnywhere) so it's accessible
  from any device, not just the local computer

---

## ⚠️ Note for your report/presentation

This is a fully functional prototype demonstrating every feature from
your proposal (user registration, blood request creation and tracking,
blood group matching, AI urgency detection, AI donor health screening,
AI blood-group card verification, notifications via matching dashboard,
and admin verification). The
underlying tech stack was adapted from Django/MySQL to Flask/SQLite
specifically so it can be handed to your instructor or teammates and
run on any laptop in under 5 minutes with no server setup — you're
welcome to mention this adaptation in your report if asked why the
implementation differs slightly from the originally proposed stack.
