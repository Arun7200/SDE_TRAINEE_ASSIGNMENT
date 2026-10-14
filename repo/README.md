# Employee Attendance & Analytics API

FastAPI + MongoDB, all code in `app/main.py`.

## Run

```
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

A `.env` file works too. Indexes are created when the app starts. Needs MongoDB 6.0 or newer.

## Notes

- All endpoints in openapi.yaml are done, nothing is missing.
- Analytics are aggregation pipelines. The trend uses `$range` to fill empty days and `$setWindowFields` for the moving average.
- Rounding is half up. Mongo's `$round` rounds half to even, so rates are scaled to whole numbers and rounded with floor(x + 0.5), the rest is rounded in Python.
- `moving_avg_7d` averages the reported (4 decimal) rates.
- The 92 day limit on the trend counts both the first and last day.
- REVIEW.md and DECISIONS.md have the review and the design answers.
