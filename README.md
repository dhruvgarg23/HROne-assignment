# Attendance API — submission

FastAPI + PyMongo, all code in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db
# local mongo: docker run -p 27017:27017 mongo:7
python sample_seed.py
uvicorn app.main:app --port 8000
```

Health: `GET /health` (200 only when Mongo pings, else 503). Indexes are created at startup, idempotently. Real env vars win over `.env`. Nothing else to configure.

Notes: single-file app; analytics are Mongo aggregations (`$rank` for leaderboard ties, `$densify` + `$setWindowFields` for trend); times are epoch-ms truncated to seconds, dates IST with overnight handling. See `REVIEW.md` / `DECISIONS.md`.
