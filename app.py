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


SENDGRID_KEY = os.environ.get("SENDGRID_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "")              # a sender verified in that SendGrid account
WHERE = "the Omvana booth (Mindvalley, upstairs) at HOLOLIFE Amsterdam"


def send_email(to: str, subject: str, text: str) -> bool:
    """Booking emails through SendGrid. Off (returns False, sends nothing) until SENDGRID_API_KEY and MAIL_FROM are set."""
    if not (SENDGRID_KEY and MAIL_FROM and to):
        return False
    import json as _j, urllib.request as _u
    body = {"personalizations": [{"to": [{"email": to}]}], "from": {"email": MAIL_FROM, "name": "Omvana"},
            "subject": subject, "content": [{"type": "text/plain", "value": text}]}
    req = _u.Request("https://api.sendgrid.com/v3/mail/send", data=_j.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {SENDGRID_KEY}", "Content-Type": "application/json"})
    try:
        _u.urlopen(req, timeout=10)
        return True
    except Exception as e:
        app.logger.warning("email to %s failed: %s", to, e)
        return False


def booked_email(name, slot, sess):
    s = SESSIONS.get(sess, {})
    return (f"Hi {name},\n\nYou're booked for {slot} on {DAY}: {s.get('track', '')}, {s.get('name', '')} {s.get('who', '')}.\n\n"
            f"Come to {WHERE} 5 minutes early. Your seat is held on trust: if you can't make it, reply to this email so "
            f"someone on the waitlist gets it.\n\nTake it home too: Omvana has 1,000+ meditations curated by Vishen.\n{APP_STORE}\n\nOmvana")


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
            # A cancelled booking still holds its (slot, seat) row; clear it so the freed seat can really be booked.
            c.execute("DELETE FROM bookings WHERE slot=%s AND seat=%s AND status='cancelled'", (slot, seat))
            c.execute("INSERT INTO bookings (slot, seat, first_name, email, session) VALUES (%s,%s,%s,%s,%s)",
                      (slot, seat, name, email.lower(), sess))
    except psycopg.errors.UniqueViolation:
        return redirect(url_for("home", err=f"Someone just took {slot}. Pick another time.") + "#book")
    send_email(email, f"You're booked: {slot} at the Omvana booth", booked_email(name, slot, sess))
    return redirect(url_for("home", done="booked", slot=slot) + "#top")


@app.post("/waitlist")
def waitlist():
    name, email, sess = clean(request.form.get("first_name"), 40), clean(request.form.get("email"), 120), request.form.get("session")
    if not name or "@" not in email:
        return redirect(url_for("home", err="Please add your first name and email to join the waitlist.") + "#waitlist")
    with db() as c:
        c.execute("INSERT INTO waitlist (first_name, email, session) VALUES (%s,%s,%s)", (name, email.lower(), sess if sess in SESSIONS else None))
    send_email(email, "You're on the Omvana booth waitlist",
               f"Hi {name},\n\nYou're on the waitlist for {DAY}. When a seat frees up, we'll email you and keep it for you at {WHERE}.\n\n"
               f"Don't want to wait? Omvana has 1,000+ meditations curated by Vishen, and you can start tonight.\n{APP_STORE}\n\nOmvana")
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
    return render_template("team.html", bookings=b, waitlist=w, key=TEAM_KEY, sessions=SESSIONS, day=DAY, emails=bool(SENDGRID_KEY and MAIL_FROM),
                           free=[(s, k) for s in slots() for k in range(1, SEATS + 1) if (s, k) not in taken()])


@app.get("/team.csv")
def team_csv():
    """Every booking and waitlist entry as a spreadsheet (opens in Excel, Numbers or Google Sheets)."""
    team_only()
    import csv, io
    with db() as c:
        b = c.execute("SELECT slot, seat, first_name, email, session, status, created_at FROM bookings ORDER BY slot, seat").fetchall()
        w = c.execute("SELECT first_name, email, session, status, created_at FROM waitlist ORDER BY created_at").fetchall()
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["list", "time", "seat", "first name", "email", "meditation", "status", "booked at"])
    for r in b:
        wr.writerow(["booking", r[0], r[1], r[2], r[3], SESSIONS.get(r[4], {}).get("name", r[4]), r[5], r[6]])
    for r in w:
        wr.writerow(["waitlist", "", "", r[0], r[1], SESSIONS.get(r[2], {}).get("name", r[2] or "either"), r[3], r[4]])
    return app.response_class(out.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": "attachment; filename=omvana-booth-bookings.csv"})


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
                send_email(w[1], f"A seat opened for you: {slot} at the Omvana booth", booked_email(w[0], slot, w[2] or "6phase"))
    return redirect(url_for("team", key=TEAM_KEY))


@app.get("/healthz")
def healthz():
    return "ok"


setup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
