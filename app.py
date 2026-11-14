"""Omvana booth bookings for HOLOLIFE (Amsterdam, Sun Nov 15 2026).

Two armchairs, 30-minute slots across the day. People book a seat on trust (no payment), or join the waitlist when
it's full. The booth team sees every booking at /team?key=<TEAM_KEY>, marks arrivals and no-shows, and moves
waitlisted people into freed seats. Asked for by Vishen, Sat Nov 14 2026.
"""
import os
import re
from datetime import datetime, timedelta

import psycopg
from flask import Flask, abort, redirect, render_template, request, url_for

app = Flask(__name__)

DAY = os.environ.get("BOOTH_DAY", "Sun Nov 15")
START = os.environ.get("BOOTH_START", "10:00")       # first slot, Amsterdam time
HOURS = float(os.environ.get("BOOTH_HOURS", "9"))
SEATS = int(os.environ.get("BOOTH_SEATS", "2"))
TEAM_KEY = os.environ.get("TEAM_KEY", "")
SESSIONS = {
    # Three upgrades (Vishen, Sat Nov 14): confidence and resilience from Paul McKenna, connection from the 6 Phase.
    "confidence": {"track": "Confidence upgrade", "name": "Hypnotic Trance: Instant Confidence", "who": "with Paul McKenna", "mins": 27, "img": "cover-confidence.jpg"},
    "resilience": {"track": "Resilience upgrade", "name": "The Havening Technique", "who": "with Paul McKenna", "mins": 5, "img": "cover-havening.jpg"},
    "6phase": {"track": "Connection upgrade", "name": "The 6 Phase Meditation", "who": "with Vishen Lakhiani", "mins": 19, "img": "cover-6-phase.jpg"},
}
APP_STORE = "https://apps.apple.com/app/id6827573005"


def slots():
    t = datetime.strptime(START, "%H:%M")
    return [(t + timedelta(minutes=30 * i)).strftime("%H:%M") for i in range(int(HOURS * 2))]


def db():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def setup():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY, slot TEXT NOT NULL, seat INT NOT NULL, first_name TEXT NOT NULL, email TEXT NOT NULL,
            session TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked', created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (slot, seat))""")
        c.execute("""CREATE TABLE IF NOT EXISTS waitlist (
            id SERIAL PRIMARY KEY, first_name TEXT NOT NULL, email TEXT NOT NULL, session TEXT,
            status TEXT NOT NULL DEFAULT 'waiting', created_at TIMESTAMPTZ DEFAULT now())""")


def taken():
    with db() as c:
        rows = c.execute("SELECT slot, seat, first_name FROM bookings WHERE status <> 'cancelled'").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def clean(s, n=60):
    return re.sub(r"\s+", " ", (s or "").strip())[:n]


@app.get("/")
def home():
    t = taken()
    grid = [{"slot": s, "seats": [t.get((s, k)) for k in range(1, SEATS + 1)]} for s in slots()]
    free = sum(1 for g in grid for x in g["seats"] if x is None)
    return render_template("index.html", grid=grid, free=free, total=len(grid) * SEATS, day=DAY, sessions=SESSIONS,
                           app_store=APP_STORE, done=request.args.get("done"), slot=request.args.get("slot"),
                           err=request.args.get("err"))


@app.post("/book")
def book():
    name, email, slot, sess = clean(request.form.get("first_name"), 40), clean(request.form.get("email"), 120), request.form.get("slot"), request.form.get("session")
    if not name or "@" not in email or slot not in slots() or sess not in SESSIONS:
        return redirect(url_for("home", err="Please add your first name, email, a time and a meditation.") + "#book")
    t = taken()
    seat = next((k for k in range(1, SEATS + 1) if (slot, k) not in t), None)
    if seat is None:
        return redirect(url_for("home", err=f"Someone just took {slot}. Pick another time or join the waitlist.") + "#book")
    try:
        with db() as c:
            c.execute("INSERT INTO bookings (slot, seat, first_name, email, session) VALUES (%s,%s,%s,%s,%s)",
                      (slot, seat, name, email.lower(), sess))
    except psycopg.errors.UniqueViolation:
        return redirect(url_for("home", err=f"Someone just took {slot}. Pick another time.") + "#book")
    return redirect(url_for("home", done="booked", slot=slot) + "#top")


@app.post("/waitlist")
def waitlist():
    name, email, sess = clean(request.form.get("first_name"), 40), clean(request.form.get("email"), 120), request.form.get("session")
    if not name or "@" not in email:
        return redirect(url_for("home", err="Please add your first name and email to join the waitlist.") + "#waitlist")
    with db() as c:
        c.execute("INSERT INTO waitlist (first_name, email, session) VALUES (%s,%s,%s)", (name, email.lower(), sess if sess in SESSIONS else None))
    return redirect(url_for("home", done="waitlist") + "#top")


def team_only():
    if not TEAM_KEY or request.args.get("key") != TEAM_KEY:
        abort(404)


@app.get("/team")
def team():
    team_only()
    with db() as c:
        b = c.execute("SELECT id, slot, seat, first_name, email, session, status FROM bookings ORDER BY slot, seat").fetchall()
        w = c.execute("SELECT id, first_name, email, session, status, created_at FROM waitlist ORDER BY created_at").fetchall()
    return render_template("team.html", bookings=b, waitlist=w, key=TEAM_KEY, sessions=SESSIONS, day=DAY,
                           free=[(s, k) for s in slots() for k in range(1, SEATS + 1) if (s, k) not in taken()])


@app.post("/team/status")
def team_status():
    team_only()
    bid, st = request.form.get("id"), request.form.get("status")
    if st in ("booked", "arrived", "no-show", "cancelled"):
        with db() as c:
            c.execute("UPDATE bookings SET status=%s WHERE id=%s", (st, bid))
    return redirect(url_for("team", key=TEAM_KEY))


@app.post("/team/promote")
def team_promote():
    """Give a waitlisted person a free seat (e.g. one freed by a no-show)."""
    team_only()
    wid, slot = request.form.get("id"), request.form.get("slot")
    with db() as c:
        w = c.execute("SELECT first_name, email, session FROM waitlist WHERE id=%s AND status='waiting'", (wid,)).fetchone()
        if w and slot in slots():
            c.execute("DELETE FROM bookings WHERE slot=%s AND status IN ('no-show','cancelled')", (slot,))
            used = {r[0] for r in c.execute("SELECT seat FROM bookings WHERE slot=%s", (slot,)).fetchall()}
            seat = next((k for k in range(1, SEATS + 1) if k not in used), None)
            if seat:
                c.execute("INSERT INTO bookings (slot, seat, first_name, email, session) VALUES (%s,%s,%s,%s,%s)",
                          (slot, seat, w[0], w[1], w[2] or "6phase"))
                c.execute("UPDATE waitlist SET status='seated' WHERE id=%s", (wid,))
    return redirect(url_for("team", key=TEAM_KEY))


@app.get("/healthz")
def healthz():
    return "ok"


setup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
