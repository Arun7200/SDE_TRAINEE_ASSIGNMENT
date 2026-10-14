# How to run

The folder `repo/` is what you push to GitHub. `check_analytics.py` is only for testing, don't push it.

## 1. Start MongoDB (6.0 or newer)

Docker:
    docker run -d --name mongo -p 27017:27017 mongo:7

Or use a free MongoDB Atlas cluster and copy its connection string.

## 2. Install and start the API

    cd repo
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt

    export MONGO_URI="mongodb://localhost:27017"     # Windows: set MONGO_URI=...
    export MONGO_DB="attendance_db"
    uvicorn app.main:app --port 8000

Open http://localhost:8000/docs to try the endpoints. http://localhost:8000/health should say ok.

## 3. Try it quickly

    curl -X POST localhost:8000/employees -H "Content-Type: application/json" \
      -d '{"emp_code":"EMP0001","name":"Asha","email":"asha@x.co","department":"Engineering","joined_on":"2026-01-05"}'

    curl -X POST localhost:8000/attendance/punch-in -H "Content-Type: application/json" \
      -d '{"emp_code":"EMP0001"}'

## 4. Check the analytics (do this before submitting)

This wipes the database it points to, so use a scratch one.

    pip install httpx
    export MONGO_DB="attendance_scratch"
    uvicorn app.main:app --port 8000        # restart the API with this DB
    python ../check_analytics.py            # in a second terminal, same MONGO_DB

It should end with ALL GOOD. If something fails, send me the output.

## 5. Submit

Create a public GitHub repo with the contents of `repo/` (no .env), then send the link before 11:59 PM IST, 15 Oct 2026. Don't post the problem or your solution anywhere else before 15 Nov 2026.
