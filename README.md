# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of employee punch-in, punch-out, attendance regularization, audit history, monthly analytics, department trends, late ranking and query-plan inspection.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` and set variables with `$env:MONGO_URI` and `$env:MONGO_DB`.

The app creates its indexes automatically at startup. `GET /health` reports readiness only after MongoDB responds. Interactive API documentation is available at `/docs`.

All required endpoints and documents are implemented.
