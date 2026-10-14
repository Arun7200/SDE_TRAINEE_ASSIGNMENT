"""
Optional self-check (not part of the submission). Needs a real MongoDB 6+ and the API running.

  export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_scratch
  uvicorn app.main:app --port 8000        # in one terminal
  python check_analytics.py               # in another (pip install httpx)

WARNING: it wipes employees and attendance_logs in MONGO_DB. Use a scratch database.

It seeds awkward data straight into MongoDB, works out the expected answers with plain Python loops
(written independently of app/main.py) and compares them with what the API returns.
"""
import os, random, sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import httpx
from pymongo import MongoClient

BASE = os.environ.get("API", "http://localhost:8000")
db = MongoClient(os.environ.get("MONGO_URI", "mongodb+srv://karun3456000_db_user:S6WbINtSaZh2ONOU@cluster0.qqeqraj.mongodb.net/?appName=Cluster0"), tz_aware=True)[os.environ.get("MONGO_DB", "attendance_scratch")]
rnd = random.Random(7)
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
MONTH, FIRST, LAST = "2026-07", date(2026, 7, 1), date(2026, 7, 31)


def hu(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- seed ----
emps = []
def add(code, dept, joined="2026-01-05"):
    emps.append({"emp_code": code, "name": "Name " + code, "email": code + "@x.co", "department": dept,
                 "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                 "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)})
for i in range(1, 7): add(f"EMP{i:04d}", "Engineering")
add("EMP0007", "Engineering", "2026-07-15")            # mid-month joiner
for i in range(10, 14): add(f"EMP{i:04d}", "Sales")
add("EMP0020", "Operations")                             # never has a log
add("EMP0021", "Operations", "2026-09-01")               # joins after the month
add("EMP0030", "Support", "2026-07-20")
for i in (40, 41, 42): add(f"EMP{i:04d}", "Sales")      # three-way tie at the very top of the leaderboard                  # Support exists but nobody has joined before the 20th

logs = []
days = [FIRST + timedelta(days=i) for i in range((LAST - FIRST).days + 1)]
for e in emps:
    if e["emp_code"] in ("EMP0020", "EMP0021", "EMP0040", "EMP0041", "EMP0042"): continue
    joined = date.fromisoformat(e["joined_on"])
    for d in days:
        if d < joined or rnd.random() < 0.25: continue          # missing days = no document at all
        if d.weekday() >= 5 and rnd.random() < 0.8: continue    # a few weekend records survive
        status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [10, 3, 2, 1, 1])[0]
        doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None, "punch_out": None,
               "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        if status in PRESENCE:
            late = rnd.choice([0, 0, 0, 10, 15, 20, 30, 45])   # multiples of 5 => plenty of ties
            wh = rnd.choice([3.5, 4.49, 4.5, 8.0, 9.12, None])
            pin = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc) + timedelta(minutes=late)
            doc.update(punch_in=pin, late_minutes=late, overtime_minutes=rnd.choice([0, 0, 35, 70]))
            if wh is not None:
                doc.update(work_hours=wh, punch_out=pin + timedelta(hours=wh), half_day=wh < 4.5)
        logs.append(doc)
for code in ("EMP0040", "EMP0041", "EMP0042"):
    for d in ("2026-07-07", "2026-07-08"):
        logs.append({"emp_code": code, "date": d, "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc), "punch_out": None,
                     "work_hours": None, "late_minutes": 500, "overtime_minutes": 0, "half_day": False, "history": []})
legacy = dict(logs[0]); legacy.pop("half_day"); legacy.pop("history"); logs[0] = legacy   # legacy shape
logs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 500, "punch_in": None})  # orphan

db.employees.delete_many({}); db.attendance_logs.delete_many({})
db.employees.insert_many(emps); db.attendance_logs.insert_many(logs)
print(f"seeded {len(emps)} employees, {len(logs)} logs")

# ----------------------------------------------------------- reference ----
by_emp = {}
for l in logs: by_emp.setdefault(l["emp_code"], []).append(l)
E = {e["emp_code"]: e for e in emps}
def in_month(l): return MONTH == l["date"][:7]
def wkday(l): return date.fromisoformat(l["date"]).weekday() < 5
def weight(l): return (0.5 if l.get("half_day") else 1) if l["status"] in PRESENCE else 0
def present_days(ls): return sum(weight(l) for l in ls if wkday(l))
def wdays(a, b): return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5) if b >= a else 0

def ref_monthly(code):
    ls = [l for l in by_emp.get(code, []) if in_month(l)]
    wd = wdays(max(FIRST, date.fromisoformat(E[code]["joined_on"])), LAST)
    pd = present_days(ls)
    return {"emp_code": code, "month": MONTH, "working_days": wd, "present_days": hu(pd, 2),
            "leave_days": sum(l["status"] == "LEAVE" for l in ls), "late_count": sum(l["late_minutes"] > 0 for l in ls),
            "total_late_minutes": sum(l["late_minutes"] for l in ls), "total_overtime_minutes": sum(l["overtime_minutes"] for l in ls),
            "attendance_pct": None if wd == 0 else hu(Decimal(str(pd)) / wd * 100, 2)}

def ref_summary(dept=None):
    out = {}
    for e in emps:
        if e["joined_on"] > LAST.isoformat() or (dept and e["department"] != dept): continue
        o = out.setdefault(e["department"], dict(department=e["department"], headcount=0, present_days=0.0, late_count=0,
                           total_late_minutes=0, leave_count=0, on_duty_count=0, wh=[]))
        o["headcount"] += 1
        for l in (x for x in by_emp.get(e["emp_code"], []) if in_month(x)):
            o["present_days"] += weight(l) if wkday(l) else 0
            o["late_count"] += l["late_minutes"] > 0; o["total_late_minutes"] += l["late_minutes"]
            o["leave_count"] += l["status"] == "LEAVE"; o["on_duty_count"] += l["status"] == "ON_DUTY"
            if l["status"] in PRESENCE and l.get("work_hours") is not None: o["wh"].append(l["work_hours"])
    res = []
    for k in sorted(out):
        o = out[k]; wh = o.pop("wh")
        o["present_days"] = hu(o["present_days"], 2)
        o["avg_work_hours"] = hu(Decimal(str(round(sum(wh), 8))) / len(wh), 2) if wh else None
        res.append({f: o[f] for f in ["department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"]})
    return {"month": MONTH, "items": res}

def ref_leaderboard(limit, dept=None):
    rows = []
    for code, ls in by_emp.items():
        if code not in E or (dept and E[code]["department"] != dept): continue
        ls = [l for l in ls if in_month(l)]; t = sum(l["late_minutes"] for l in ls)
        if t > 0: rows.append((code, t, sum(l["late_minutes"] > 0 for l in ls)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    out = []
    for i, r in enumerate(rows):
        rank = 1 + sum(1 for x in rows if x[1] > r[1])
        if rank <= limit: out.append({"rank": rank, "emp_code": r[0], "name": E[r[0]]["name"], "department": E[r[0]]["department"], "total_late_minutes": r[1], "late_count": r[2]})
    return {"month": MONTH, "items": out}

def ref_trend(dept, a, b):
    members = [e for e in emps if e["department"] == dept]
    rows, rates = [], []
    d = a
    while d <= b:
        ds = d.isoformat(); wd = d.weekday() < 5
        hc = sum(e["joined_on"] <= ds for e in members)
        recs = [l for e in members for l in by_emp.get(e["emp_code"], []) if l["date"] == ds]
        pc = sum(weight(l) for l in recs)
        rate = hu(Decimal(str(pc)) / hc, 4) if wd and hc > 0 else None
        rates.append(rate)
        win = [r for r in rates[-7:] if r is not None]
        rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": pc,
                     "late_count": sum(l["late_minutes"] > 0 for l in recs), "attendance_rate": rate,
                     "moving_avg_7d": hu(sum(Decimal(str(r)) for r in win) / len(win), 4) if win else None})
        d += timedelta(days=1)
    return {"department": dept, "items": rows}

# --------------------------------------------------------------- compare ----
c = httpx.Client(base_url=BASE, timeout=60)
bad = 0
def check(name, got, want):
    global bad
    if got != want:
        bad += 1; print("FAIL", name)
        if isinstance(want, dict) and "items" in want:
            for i, (g, w) in enumerate(zip(got.get("items", []), want["items"])):
                if g != w: print("  first diff at row", i, "\n   got ", g, "\n   want", w); break
            else: print("  row counts differ:", len(got.get("items", [])), "vs", len(want["items"]))
        else: print("  got ", got, "\n  want", want)
    else: print("ok  ", name)

for code in E: check(f"monthly {code}", c.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH}).json(), ref_monthly(code))
check("summary", c.get("/analytics/departments/summary", params={"month": MONTH}).json(), ref_summary())
check("summary Engineering", c.get("/analytics/departments/summary", params={"month": MONTH, "department": "Engineering"}).json(), ref_summary("Engineering"))
for lim in (1, 3, 5, 10):
    check(f"leaderboard limit={lim}", c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": lim}).json(), ref_leaderboard(lim))
check("leaderboard Sales", c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 3, "department": "Sales"}).json(), ref_leaderboard(3, "Sales"))
for dept in ("Engineering", "Sales", "Operations", "Support"):
    for a, b in (("2026-07-01", "2026-07-31"), ("2026-06-25", "2026-07-12")):
        check(f"trend {dept} {a}..{b}", c.get(f"/analytics/departments/{dept}/trend", params={"from": a, "to": b}).json(), ref_trend(dept, date.fromisoformat(a), date.fromisoformat(b)))

# plans: every explain must show an index scan and no collection scan
cases = [("attendance_list", {}), ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01"}), ("attendance_list", {"status": "WFH"}),
         ("employee_monthly", {"emp_code": "EMP0001", "month": MONTH}), ("department_summary", {"month": MONTH}),
         ("department_summary", {"month": MONTH, "department": "Sales"}), ("late_leaderboard", {"month": MONTH}),
         ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"})]
for ep, params in cases:
    r = c.get(f"/admin/explain/{ep}", params=params); text = r.text
    good = r.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text
    bad += not good; print("ok  " if good else "FAIL", "explain", ep, params, r.status_code)
print("\nALL GOOD" if not bad else f"\n{bad} problem(s)")
sys.exit(1 if bad else 0)