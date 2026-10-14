# Employee Attendance & Analytics API

Small HR attendance backend. FastAPI on top of MongoDB.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer

Atlas free tier works, and so does a local `mongo:7` Docker container.

## Running it

Set up a virtualenv and install:

    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Copy `.env.example` to `.env` and fill in your connection:

    MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
    MONGO_DB=attendance_db

Real environment variables override `.env`. If you run this behind a scheduler or CI, leave `.env` out and set the two variables directly.

Optional: seed the sample documents:

    python sample_seed.py

Start the server:

    uvicorn app.main:app --port 8000

Check that it's up:

    curl http://127.0.0.1:8000/health
    # {"status":"ok"}

Interactive API docs: http://127.0.0.1:8000/docs

## Endpoints

`openapi.yaml` is the contract; it defines status codes, params, and the response shapes. The interesting ones:

- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Indexes

All created at startup, `create_index` is idempotent, no manual step. Startup is fast; the index builds on an empty collection are instant and on an existing one they're no-ops.

## Concurrency

Punch-in relies on the unique index on `(emp_code, date)` and returns 409 on duplicate. Punch-out uses `find_one_and_update` filtered on `punch_out: null`, so exactly one of two simultaneous punch-outs wins and the other gets 409. PATCH uses the current `history` array as an optimistic-lock filter; if another writer changed the record first, the update matches nothing and the request returns 409 so the client can retry with the new state.

## Notes

- Timestamps in requests and responses are epoch milliseconds. In MongoDB they are BSON datetimes.
- Dates and shift times are strings (`YYYY-MM-DD`, `HH:MM`) in IST. That's on purpose; see `DATA_MODEL.md`.
- Analytics go through MongoDB aggregation pipelines. There is no Python loop over records.

## What's missing

Nothing from the contract. All endpoints in `openapi.yaml` are implemented.