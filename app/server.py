"""Nederlands oefenen — kleine webserver zonder externe afhankelijkheden (alleen Python standaardbibliotheek).

Start:  python server.py         (poort 8001, database in ./data/nederlands.db)
Omgeving: PORT, DATA_DIR, COOKIE_SECURE=1 (alleen als je via https draait)
"""
import csv
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import content as C
from tts import TTS

PORT = int(os.environ.get("PORT", "8001"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "nederlands.db")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
SESSION_DAYS = 30
DAILY_GOAL = 20
XP_CORRECT, XP_ALMOST = 10, 4

os.makedirs(DATA_DIR, exist_ok=True)
SPEECH = TTS(DATA_DIR, "nl-NL", "nl-NL-FennaNeural", C.all_phrases())


# ---------------- database ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student', pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
            created INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0, speed_best INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS progress(
            user_id INTEGER NOT NULL, key TEXT NOT NULL, box INTEGER NOT NULL DEFAULT 0,
            seen INTEGER NOT NULL DEFAULT 0, wrong INTEGER NOT NULL DEFAULT 0, last_ts INTEGER,
            PRIMARY KEY(user_id, key));
        CREATE TABLE IF NOT EXISTS answers(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, ts INTEGER NOT NULL, day TEXT NOT NULL,
            mode TEXT NOT NULL, level INTEGER, item TEXT NOT NULL, verb TEXT, pron TEXT,
            prompt TEXT, given TEXT, expected TEXT, result INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_answers_user ON answers(user_id, id);
        """)


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return h, salt


def today():
    return dt.date.today().isoformat()


# ---------------- in-memory state ----------------
LOCK = threading.Lock()
PENDING = {}                       # qid -> vraag (met antwoord)
RECENT = defaultdict(lambda: deque(maxlen=4))   # user_id -> laatst gevraagde items
FAILS = defaultdict(list)          # ip -> tijdstippen mislukte logins


def purge():
    now = time.time()
    for d, age in ((PENDING, 3600),):
        for k in [k for k, v in d.items() if now - v["t"] > age]:
            d.pop(k, None)


# ---------------- leerlogica ----------------
BOX_WEIGHT = {0: 3, 1: 10, 2: 5, 3: 2, 4: 1, 5: 0.5}


def load_progress(con, uid, mode):
    rows = con.execute("SELECT key, box, seen, wrong FROM progress WHERE user_id=? AND key LIKE ?",
                       (uid, mode + ":%")).fetchall()
    return {r["key"].split(":", 1)[1]: dict(r) for r in rows}


def pick_item(con, uid, mode, pool):
    prog = load_progress(con, uid, mode)
    recent = RECENT[uid]
    cand = [i for i in pool if i not in recent] if len(pool) > 5 else list(pool)
    box1 = [i for i in cand if prog.get(i, {}).get("box") == 1]
    if box1 and random.random() < 0.35:
        choice = random.choice(box1)
    else:
        weights = [BOX_WEIGHT[prog.get(i, {}).get("box", 0)] for i in cand]
        choice = random.choices(cand, weights=weights)[0]
    recent.append(choice)
    return choice


def weak_set(con, uid):
    rows = con.execute("SELECT key FROM progress WHERE user_id=? AND wrong>0 AND box<=2", (uid,)).fetchall()
    return {r["key"].split(":", 1)[1] for r in rows}


def normalize(s):
    s = (s or "").strip().lower().rstrip(".!? ")
    s = re.sub(r"[’‘`´]", "'", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*'\s*", "'", s)
    return s.rstrip(".!? ")


def strip_accents(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def check(given, q):
    if q["kind"] == "build":
        g = re.sub(r"[.,?!]", " ", (given or "").lower())
        g = " ".join(g.split())
        return 2 if g in [a.lower() for a in q["accepted"]] else 0
    g = normalize(given)
    ok = [normalize(a) for a in q["accepted"]]
    if g in ok:
        return 2
    if strip_accents(g) in [strip_accents(a) for a in ok]:
        return 1
    return 0


def streak(con, uid):
    days = {r["day"] for r in con.execute("SELECT DISTINCT day FROM answers WHERE user_id=?", (uid,))}
    d = dt.date.today()
    if d.isoformat() not in days:
        d -= dt.timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n


# ---------------- HTTP ----------------
class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".json": "application/manifest+json", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NederlandsOefenen/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") != "1":
            super().log_message(fmt, *args)

    # ---- helpers ----
    def send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (headers or {}).pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ApiError(413, "Te groot")
        if not n:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "JSON verwacht")
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ApiError(400, "Ongeldige JSON")

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["sid"].value if "sid" in c else None

    def user(self, con, required=True, admin=False):
        tok = self.token()
        u = None
        if tok:
            u = con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                            "WHERE s.token=? AND s.created>?", (tok, time.time() - SESSION_DAYS * 86400)).fetchone()
        if required and not u:
            raise ApiError(401, "Niet ingelogd")
        if admin and u["role"] != "admin":
            raise ApiError(403, "Alleen voor ouders/docenten")
        return u

    def target_user(self, con, me, qs):
        """Welke leerling bekijken we? Leerlingen alleen zichzelf, beheerders iedereen."""
        uid = qs.get("user", [None])[0]
        if uid and me["role"] == "admin":
            u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                raise ApiError(404, "Gebruiker niet gevonden")
            return u
        return me

    def cookie(self, value, max_age):
        flags = f"sid={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        return flags + ("; Secure" if COOKIE_SECURE else "")

    # ---- routing ----
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Niet toegestaan")
                return self.static(path)
            fn = getattr(self, f"api_{method.lower()}_{path[5:].replace('/', '_').replace('.', '_')}", None)
            if not fn:
                raise ApiError(404, "Onbekend")
            con = db()
            try:
                with con:
                    res = fn(con, qs)
            finally:
                con.close()
            if res is not None:
                self.send(200, res)
        except ApiError as e:
            self.send(e.status, {"error": e.msg})
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self.send(500, {"error": "Er ging iets mis op de server"})

    def static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            full = os.path.join(STATIC, "index.html")
        with open(full, "rb") as f:
            data = f.read()
        self.send(200, data, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    # ---------------- account ----------------
    def api_get_state(self, con, qs):
        setup = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        u = None if setup else self.user(con, required=False)
        return {"setup_needed": setup, "tts": SPEECH.engine,
                "user": {"id": u["id"], "name": u["name"], "role": u["role"]} if u else None}

    def api_post_setup(self, con, qs):
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise ApiError(400, "Er is al een beheerder")
        b = self.body()
        return self._create_user(con, b, role="admin", login=True)

    def _create_user(self, con, b, role, login=False):
        username = (b.get("username") or "").strip().lower()
        name = (b.get("name") or "").strip() or username
        pw = b.get("password") or ""
        if not re.fullmatch(r"[a-z0-9._-]{2,30}", username):
            raise ApiError(400, "Gebruikersnaam: 2-30 tekens, alleen letters, cijfers, . _ -")
        if len(pw) < 6:
            raise ApiError(400, "Wachtwoord moet minstens 6 tekens zijn")
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ApiError(400, "Die gebruikersnaam bestaat al")
        h, salt = hash_pw(pw)
        cur = con.execute("INSERT INTO users(username,name,role,pw_hash,salt,created) VALUES(?,?,?,?,?,?)",
                          (username, name[:40], role, h, salt, int(time.time())))
        if login:
            return self._login(con, cur.lastrowid)
        return {"ok": True, "id": cur.lastrowid}

    def _login(self, con, uid):
        tok = secrets.token_urlsafe(32)
        con.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, uid, int(time.time())))
        con.commit()
        self.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie(tok, SESSION_DAYS * 86400)})

    def api_post_login(self, con, qs):
        ip = self.client_address[0]
        now = time.time()
        with LOCK:
            FAILS[ip] = [t for t in FAILS[ip] if now - t < 600]
            if len(FAILS[ip]) >= 10:
                raise ApiError(429, "Te veel pogingen. Probeer het over 10 minuten opnieuw.")
        b = self.body()
        u = con.execute("SELECT * FROM users WHERE username=?", ((b.get("username") or "").strip().lower(),)).fetchone()
        if not u or not secrets.compare_digest(hash_pw(b.get("password") or "", u["salt"])[0], u["pw_hash"]):
            with LOCK:
                FAILS[ip].append(now)
            raise ApiError(401, "Gebruikersnaam of wachtwoord klopt niet")
        return self._login(con, u["id"])

    def api_post_logout(self, con, qs):
        tok = self.token()
        if tok:
            con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        self.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})

    def api_post_password(self, con, qs):
        me = self.user(con)
        b = self.body()
        if hash_pw(b.get("old") or "", me["salt"])[0] != me["pw_hash"]:
            raise ApiError(400, "Huidig wachtwoord klopt niet")
        if len(b.get("new") or "") < 6:
            raise ApiError(400, "Nieuw wachtwoord moet minstens 6 tekens zijn")
        h, salt = hash_pw(b["new"])
        con.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (h, salt, me["id"]))
        return {"ok": True}

    def api_get_tts(self, con, qs):
        self.user(con)
        res = SPEECH.get(qs.get("t", [""])[0])
        if not res:
            raise ApiError(404, "Geen geluid")
        self.send(200, res[0], res[1], {"Cache-Control": "private, max-age=604800"})

    # ---------------- beheer ----------------
    def api_get_users(self, con, qs):
        self.user(con, admin=True)
        rows = con.execute("""
            SELECT u.id, u.username, u.name, u.role, u.xp,
                   (SELECT MAX(ts) FROM answers a WHERE a.user_id=u.id) AS last_ts,
                   (SELECT COUNT(*) FROM answers a WHERE a.user_id=u.id AND a.day=?) AS today,
                   (SELECT COUNT(*) FROM answers a WHERE a.user_id=u.id) AS total
            FROM users u ORDER BY u.role, u.name""", (today(),)).fetchall()
        return [dict(r) for r in rows]

    def api_post_users_create(self, con, qs):
        self.user(con, admin=True)
        b = self.body()
        return self._create_user(con, b, role="admin" if b.get("role") == "admin" else "student")

    def api_post_users_password(self, con, qs):
        self.user(con, admin=True)
        b = self.body()
        if len(b.get("password") or "") < 6:
            raise ApiError(400, "Wachtwoord moet minstens 6 tekens zijn")
        h, salt = hash_pw(b["password"])
        con.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (h, salt, b.get("id")))
        con.execute("DELETE FROM sessions WHERE user_id=?", (b.get("id"),))
        return {"ok": True}

    def api_post_users_delete(self, con, qs):
        me = self.user(con, admin=True)
        uid = self.body().get("id")
        if uid == me["id"]:
            raise ApiError(400, "Je kunt jezelf niet verwijderen")
        for t in ("sessions", "progress", "answers"):
            con.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        return {"ok": True}

    # ---------------- oefenen ----------------
    def api_get_home(self, con, qs):
        me = self.user(con)
        n_today = con.execute("SELECT COUNT(*) FROM answers WHERE user_id=? AND day=?", (me["id"], today())).fetchone()[0]
        return {"name": me["name"], "role": me["role"], "xp": me["xp"], "streak": streak(con, me["id"]),
                "today": n_today, "goal": DAILY_GOAL, "groups": C.GROUPS, "weak_count": len(weak_set(con, me["id"]))}

    def api_post_question(self, con, qs):
        me = self.user(con)
        b = self.body()
        ex, focus = b.get("ex"), b.get("focus") or "alle"
        if ex not in C.EXERCISES:
            raise ApiError(400, "Onbekende oefening")
        pool, note = C.pool(ex, focus, weak_set(con, me["id"]))
        if not pool:
            raise ApiError(400, "Hier zijn (nog) geen oefeningen")
        item = pick_item(con, me["id"], ex, pool)
        q = C.question(ex, item)
        qid = secrets.token_urlsafe(12)
        q.update({"t": time.time(), "uid": me["id"]})
        with LOCK:
            purge()
            PENDING[qid] = q
        return {"qid": qid, "ex": ex, "kind": q["kind"], "prompt": q["prompt"], "options": q.get("options"),
                "opt_style": q.get("opt_style", "text"), "tiles": q.get("tiles"), "note": note}

    def api_post_answer(self, con, qs):
        me = self.user(con)
        b = self.body()
        with LOCK:
            q = PENDING.pop(b.get("qid"), None)
        if not q or q["uid"] != me["id"]:
            raise ApiError(400, "Vraag verlopen, we gaan door met een nieuwe")
        given = (b.get("answer") or "")[:200]
        result = check(given, q)
        uid, key = me["id"], f"{q['ex']}:{q['item']}"
        p = con.execute("SELECT box FROM progress WHERE user_id=? AND key=?", (uid, key)).fetchone()
        box = p["box"] if p else 0
        box = (2 if box == 0 else min(5, box + 1)) if result == 2 else (max(1, box) if result == 1 else 1)
        con.execute("""INSERT INTO progress(user_id,key,box,seen,wrong,last_ts) VALUES(?,?,?,1,?,?)
                       ON CONFLICT(user_id,key) DO UPDATE SET box=excluded.box, seen=seen+1,
                       wrong=wrong+excluded.wrong, last_ts=excluded.last_ts""",
                    (uid, key, box, 1 if result < 2 else 0, int(time.time())))
        grp, cat = C.category(q["item"])
        label = C.item_label(q["item"])[0]
        prompt = f'{C.EXERCISES[q["ex"]]["name"]}: {label}'
        con.execute("INSERT INTO answers(user_id,ts,day,mode,level,item,verb,pron,prompt,given,expected,result) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, int(time.time()), today(), q["ex"], None, q["item"], grp, cat, prompt, given, q["answer"], result))
        xp = {2: XP_CORRECT, 1: XP_ALMOST, 0: 0}[result]
        con.execute("UPDATE users SET xp=xp+? WHERE id=?", (xp, uid))
        n_today = con.execute("SELECT COUNT(*) FROM answers WHERE user_id=? AND day=?", (uid, today())).fetchone()[0]
        return {"result": ["wrong", "almost", "correct"][result], "answer": q["answer"], "accepted": q["accepted"],
                "say": q["say"], "info": q.get("info"), "kind": q["kind"],
                "xp_gained": xp, "xp": me["xp"] + xp, "today": n_today, "goal": DAILY_GOAL}

    # ---------------- scores ----------------
    def api_get_stats(self, con, qs):
        me = self.user(con)
        u = self.target_user(con, me, qs)
        uid = u["id"]
        tot = con.execute("SELECT COUNT(*) n, COALESCE(SUM(result),0) s FROM answers WHERE user_id=?", (uid,)).fetchone()
        start = (dt.date.today() - dt.timedelta(days=13)).isoformat()
        per_day = {r["day"]: r for r in con.execute(
            "SELECT day, COUNT(*) n, SUM(result) s FROM answers WHERE user_id=? AND day>=? GROUP BY day", (uid, start))}
        days = []
        for i in range(13, -1, -1):
            d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
            r = per_day.get(d)
            days.append({"day": d, "n": r["n"] if r else 0, "pct": round(50 * r["s"] / r["n"]) if r else None})
        cats = [{"grp": r["verb"], "cat": r["pron"], "n": r["n"], "pct": round(50 * r["s"] / r["n"])} for r in con.execute(
            "SELECT verb, pron, COUNT(*) n, SUM(result) s FROM answers WHERE user_id=? GROUP BY verb, pron ORDER BY verb, pron", (uid,))]
        weak = []
        for r in con.execute("SELECT key, box, seen, wrong FROM progress WHERE user_id=? AND wrong>0 "
                             "ORDER BY box ASC, wrong DESC LIMIT 20", (uid,)):
            ex, item = r["key"].split(":", 1)
            if ex in C.EXERCISES:
                label, say = C.item_label(item)
                weak.append({"label": label, "say": say, "ex": C.EXERCISES[ex]["name"],
                             "wrong": r["wrong"], "seen": r["seen"], "box": r["box"]})
        mistakes = [dict(r) for r in con.execute(
            "SELECT ts, prompt, given, expected, result FROM answers WHERE user_id=? AND result<2 "
            "ORDER BY id DESC LIMIT 15", (uid,))]
        return {"user": {"id": uid, "name": u["name"]}, "total": tot["n"],
                "pct": round(50 * tot["s"] / tot["n"]) if tot["n"] else None,
                "xp": u["xp"], "streak": streak(con, uid), "days": days, "cats": cats,
                "weak": weak, "mistakes": mistakes}

    def api_get_export_csv(self, con, qs):
        me = self.user(con)
        if qs.get("user", [""])[0] == "all" and me["role"] == "admin":
            where, args, fname = "", (), "alle-leerlingen"
        else:
            u = self.target_user(con, me, qs)
            where, args, fname = "WHERE a.user_id=?", (u["id"],), u["username"]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["naam", "datum", "tijd", "onderdeel", "categorie", "vraag", "antwoord", "juist antwoord", "resultaat"])
        for r in con.execute(f"SELECT a.*, u.name FROM answers a JOIN users u ON u.id=a.user_id {where} ORDER BY a.id", args):
            w.writerow([r["name"], r["day"], time.strftime("%H:%M", time.localtime(r["ts"])),
                        r["verb"], r["pron"],
                        r["prompt"], r["given"], r["expected"], ["fout", "bijna (accent)", "goed"][r["result"]]])
        data = "﻿" + buf.getvalue()   # BOM zodat Excel de accenten goed toont
        self.send(200, data, "text/csv; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="nederlands-{fname}-{today()}.csv"'})


def main():
    init_db()
    SPEECH.prewarm()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Nederlands oefenen draait op http://0.0.0.0:{PORT}  (database: {DB_PATH})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
