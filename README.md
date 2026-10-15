# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+, tested design target 7.0). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or a .env file; real env vars win
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup. `/health` returns 503 until MongoDB answers a ping.


## Interpretations worth knowing
- `moving_avg_7d` averages the *reported* (4-decimal, rounded) `attendance_rate` values of the window.
- The trend range limit counts days inclusively: `from`..`to` may span at most 92 calendar days.
- In `PATCH`, an omitted field is unchanged, but an explicit `null` for `status`/`punch_in`/`punch_out` is a 422, because the contract types them as non-nullable. `punch_in` is checked against the record's date (R1) only when it is supplied.
- Half-up rounding is done with `Decimal` in Python and with `Decimal128` inside pipelines. MongoDB's `$round` is half-to-even, so it is not used for reported numbers.
- `page_size > 100` or `page < 1` is rejected with 422, not clamped.
- Concurrent `PATCH`es use optimistic locking (history length plus current punch/status). The loser gets 409 and no history entry is lost.
