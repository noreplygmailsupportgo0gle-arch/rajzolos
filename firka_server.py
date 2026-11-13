#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIRKA - skribbl.io-szerű rajzolós szójáték, egyetlen fájlban (Python stdlib).
Futtatás:  python firka_server.py [port]      (alap port: 8080)
"""
import base64, hashlib, json, os, random, struct, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8080))
DRAW_TIME = 80      # másodperc egy körben
CHOOSE_TIME = 15    # szóválasztási idő
BETWEEN_TIME = 5    # körök közti szünet
MAX_PLAYERS = 12
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_WORDS = """defibrillátor
naso tubus
oro tubus
pelo
intubálás
"""

def load_words():
    p = os.path.join(BASE, "words.txt")
    if os.path.exists(p):
        try:
            ws = [l.strip() for l in open(p, encoding="utf-8")
                  if l.strip() and not l.strip().startswith("#")]
            if len(ws) >= 3:
                return ws
        except Exception:
            pass
    return [w.strip() for w in DEFAULT_WORDS.splitlines() if w.strip()]

WORDS = load_words()

def norm(s):
    return "".join(ch for ch in s.lower().strip() if ch.isalnum() or ch.isspace())

def lev(a, b):
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

# ---------- WebSocket alapok ----------

def send_frame(wfile, op, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    hdr = bytearray([0x80 | op])
    n = len(payload)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(127)
        hdr += struct.pack(">Q", n)
    wfile.write(bytes(hdr) + payload)
    wfile.flush()

def read_frame(rfile):
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None
    fin = bool(hdr[0] & 0x80)
    op = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    ln = hdr[1] & 0x7F
    if ln == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        ln = struct.unpack(">H", ext)[0]
    elif ln == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        ln = struct.unpack(">Q", ext)[0]
    mask = rfile.read(4) if masked else None
    data = rfile.read(ln) if ln else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, op, data

class Conn:
    def __init__(self, handler):
        self.h = handler
        self.alive = True
        self.lock = threading.Lock()
        self.frag_op = None
        self.frag_buf = b""
        self.rates = []

    def send(self, obj):
        if not self.alive:
            return
        data = json.dumps(obj).encode()
        try:
            with self.lock:
                send_frame(self.h.wfile, 1, data)
        except Exception:
            self.alive = False

    def recv_text(self):
        while True:
            try:
                f = read_frame(self.h.rfile)
            except Exception:
                return None
            if f is None:
                return None
            fin, op, data = f
            if op == 8:
                return None
            if op == 9:
                try:
                    with self.lock:
                        send_frame(self.h.wfile, 10, data)
                except Exception:
                    return None
                continue
            if op == 10:
                continue
            if op in (1, 2):
                if not fin:
                    self.frag_op, self.frag_buf = op, data
                    continue
                return data
            if op == 0:
                if self.frag_op is None:
                    continue
                self.frag_buf += data
                if fin:
                    payload = self.frag_buf
                    self.frag_op, self.frag_buf = None, b""
                    return payload

    def spam_ok(self):
        now = time.time()
        self.rates = [t for t in self.rates if now - t < 2.0]
        if len(self.rates) > 25:
            return False
        self.rates.append(now)
        return True

# ---------- Játéklogika ----------

next_pid = [1]
pid_lock = threading.Lock()
rooms = {}
rooms_lock = threading.Lock()

class Player:
    def __init__(self, pid, name, conn):
        self.pid = pid
        self.name = name
        self.conn = conn
        self.score = 0
        self.guessed = False
        self.host = False

class Room:
    def __init__(self, pin):
        self.pin = pin
        self.players = []
        self.state = "lobby"       # lobby | choosing | drawing | between | over
        self.drawer_pid = None
        self.word = ""
        self.choices = []
        self.hint = ""
        self.revealed = set()
        self.time_left = 0
        self.round = 0
        self.rounds = 3
        self.turn_no = 0
        self.draw_ops = []
        self.guessed = set()
        self.used_words = set()
        self.gen = 0
        self.lock = threading.RLock()

    def by_pid(self, pid):
        for p in self.players:
            if p.pid == pid:
                return p
        return None

    def drawer(self):
        return self.by_pid(self.drawer_pid)

def broadcast(room, obj, exclude_pid=None, only_guessed=None):
    with room.lock:
        targets = list(room.players)
    for p in targets:
        if exclude_pid is not None and p.pid == exclude_pid:
            continue
        if only_guessed is True and not (p.guessed or p.pid == room.drawer_pid):
            continue
        p.conn.send(obj)

def players_payload(room):
    return [{"id": p.pid, "name": p.name, "score": p.score,
             "guessed": p.guessed, "host": p.host} for p in room.players]

def make_hint(room):
    out = []
    for i, ch in enumerate(room.word):
        if ch == " ":
            out.append("\u00A0\u00A0") # dupla nem törő szóköz a szavak között
        elif ch.isalpha() and i in room.revealed:
            out.append(ch)
        else:
            out.append("_")
    return " ".join(out)

def send_state(room):
    with room.lock:
        base = {"t": "state", "state": room.state, "round": room.round,
                "rounds": room.rounds, "time": room.time_left,
                "drawer_pid": room.drawer_pid, "hint": room.hint,
                "players": players_payload(room)}
        for p in room.players:
            m = dict(base)
            if room.drawer_pid == p.pid and room.state == "drawing":
                m["your_word"] = room.word
            else:
                m["your_word"] = ""
            p.conn.send(m)

def broadcast_state(room):
    send_state(room)

def new_pin():
    with rooms_lock:
        while True:
            pin = str(random.randint(1000, 9999))
            if pin not in rooms:
                return pin

def unique_name(room, name):
    names = {p.name.lower() for p in room.players}
    base, i = name, 1
    while name.lower() in names:
        i += 1
        name = f"{base} ({i})"
    return name

def add_player(room, name, conn):
    with pid_lock:
        pid = next_pid[0]
        next_pid[0] += 1
    name = unique_name(room, name)
    p = Player(pid, name, conn)
    with room.lock:
        room.players.append(p)
        if not any(q.host for q in room.players):
            p.host = True
    return p

def start_game(room, by_pid):
    with room.lock:
        host = room.by_pid(by_pid)
        if not host or not host.host:
            return
        if len(room.players) < 2:
            host.conn.send({"t": "sys", "msg": "Legalább 2 játékos kell a kezdéshez!"})
            return
        room.rounds = max(1, min(5, room.rounds))
        room.turn_no = 0
        random.shuffle(room.players) # Véletlenszerű sorrend, így random ember kezd rajzolni!
        for p in room.players:
            p.score = 0
            p.guessed = False
        room.gen += 1
    next_turn(room)

def next_turn(room):
    with room.lock:
        if len(room.players) < 2:
            room.state = "lobby"
            room.round = 0
            room.hint = ""
            room.drawer_pid = None
            broadcast_state(room)
            return
        room.round = room.turn_no // max(1, len(room.players)) + 1
        if room.round > room.rounds:
            game_over(room)
            return
        d = room.players[room.turn_no % len(room.players)]
        room.drawer_pid = d.pid
        room.guessed = set()
        for p in room.players:
            p.guessed = False
        room.draw_ops = []
        room.word = ""
        room.hint = ""
        room.state = "choosing"
        room.time_left = CHOOSE_TIME
        pool = [w for w in WORDS if norm(w) not in room.used_words]
        if len(pool) < 3:
            room.used_words.clear()
            pool = list(WORDS)
        room.choices = random.sample(pool, min(3, len(pool)))
        g = room.gen
    broadcast(room, {"t": "clear"})
    broadcast_state(room)
    with room.lock:
        d = room.drawer()
        ch = room.choices
    if d:
        d.conn.send({"t": "choose", "words": ch})
    threading.Thread(target=choose_timer, args=(room, g), daemon=True).start()

def choose_timer(room, g):
    deadline = time.time() + CHOOSE_TIME
    while time.time() < deadline:
        time.sleep(0.5)
        with room.lock:
            if g != room.gen or room.state != "choosing":
                return
            room.time_left = max(0, int(deadline - time.time()))
        broadcast(room, {"t": "tick", "time": room.time_left})
    with room.lock:
        if g != room.gen or room.state != "choosing":
            return
        word = random.choice(room.choices)
    begin_draw(room, word, g)

def begin_draw(room, word, g):
    with room.lock:
        if g != room.gen:
            return
        room.word = word
        room.used_words.add(norm(word))
        room.state = "drawing"
        room.time_left = DRAW_TIME
        room.revealed = set()
        room.hint = make_hint(room)
    broadcast_state(room)
    broadcast(room, {"t": "sys", "msg": "Kezdődik a kör! " +
                (room.drawer().name if room.drawer() else "?") + " rajzol!"})
    threading.Thread(target=draw_timer, args=(room, g), daemon=True).start()

def draw_timer(room, g):
    while True:
        time.sleep(1)
        with room.lock:
            if g != room.gen or room.state != "drawing":
                return
            room.time_left -= 1
            want = 0
            if room.time_left <= DRAW_TIME * 0.5:
                want = 1
            if room.time_left <= DRAW_TIME * 0.25 and len(room.word) >= 6:
                want = 2
            alpha_idx = [i for i, c in enumerate(room.word) if c.isalpha()]
            want = min(want, len(alpha_idx))
            while len(room.revealed) < want and alpha_idx:
                i = random.choice(alpha_idx)
                if i not in room.revealed:
                    room.revealed.add(i)
            room.hint = make_hint(room)
            t, h = room.time_left, room.hint
        broadcast(room, {"t": "tick", "time": t, "hint": h})
        with room.lock:
            drawers = [p for p in room.players if p.pid != room.drawer_pid]
            allg = all(p.guessed for p in drawers) if drawers else False
        if t <= 0 or allg:
            end_turn(room, g, "time" if t <= 0 else "all")
            return

def end_turn(room, g, reason):
    with room.lock:
        if g != room.gen or room.state != "drawing":
            return
        word = room.word
        d = room.drawer()
        guessers = len(room.guessed)
        if d and guessers and reason != "skip":
            d.score += 20 * guessers
        room.state = "between"
        room.gen += 1
        g = room.gen
        room.hint = ""
        scores = players_payload(room)
        rnd = room.round
    broadcast(room, {"t": "turn_end", "word": word, "reason": reason, "players": scores, "round": rnd})
    threading.Timer(BETWEEN_TIME, advance, args=(room, g)).start()

def advance(room, g):
    with room.lock:
        if g != room.gen:
            return
        room.turn_no += 1
    next_turn(room)

def game_over(room):
    with room.lock:
        room.state = "over"
        room.gen += 1
        room.hint = ""
        room.drawer_pid = None
        board = players_payload(room)
    board.sort(key=lambda p: -p["score"])
    broadcast(room, {"t": "game_over", "players": board})

def handle_guess(room, p, text):
    text = text[:120]
    with room.lock:
        st = room.state
    if st != "drawing":
        chat_all(room, p, text)
        return
    w = norm(room.word)
    g = norm(text)
    if p.pid == room.drawer_pid:
        if w and w in g:
            p.conn.send({"t": "sys", "msg": "A szót nem fedheted fel chatben!"})
            return
        chat_all(room, p, text)
        return
    if p.guessed:
        broadcast(room, {"t": "chat", "name": p.name, "msg": text, "kind": "chat"},
                  only_guessed=True)
        return
    if w and g == w:
        pts = 100 + room.time_left * 3
        with room.lock:
            p.score += pts
            p.guessed = True
            room.guessed.add(p.pid)
        broadcast(room, {"t": "guessed_ok", "pid": p.pid, "name": p.name,
                         "pts": pts, "players": players_payload(room)})
        p.conn.send({"t": "you_guessed", "word": room.word})
        with room.lock:
            drawers = [q for q in room.players if q.pid != room.drawer_pid]
            allg = all(q.guessed for q in drawers) if drawers else False
        if allg:
            end_turn(room, room.gen, "all")
    elif w and w in g:
        p.conn.send({"t": "sys", "msg": "Ne spammeld a szót! ;)"})
    elif w and len(w) >= 4 and lev(g, w) <= 2:
        p.conn.send({"t": "sys", "msg": "Nagyon közel jársz! 🔥"})
    else:
        chat_all(room, p, text)

def chat_all(room, p, text):
    broadcast(room, {"t": "chat", "name": p.name, "msg": text, "kind": "chat"})

def remove_player(room, pid):
    with room.lock:
        gone = room.by_pid(pid)
        room.players = [p for p in room.players if p.pid != pid]
        was_drawer = room.drawer_pid == pid
        st = room.state
        if room.players and all(not q.host for q in room.players):
            room.players[0].host = True
    if gone:
        broadcast(room, {"t": "sys", "msg": gone.name + " kilépett."})
    with rooms_lock:
        if not room.players:
            rooms.pop(room.pin, None)
            room.gen += 1
            return
    if was_drawer and st in ("choosing", "drawing"):
        room.gen += 1
        room.state = "between"
        broadcast_state(room)
        threading.Timer(2.0, advance, args=(room, room.gen)).start()
    else:
        broadcast_state(room)

# ---------- Üzenetkezelő ----------

def handle_msg(room, p, m):
    t = m.get("t")
    if t == "start":
        start_game(room, p.pid)
    elif t == "choose_word":
        with room.lock:
            ok = room.state == "choosing" and room.drawer_pid == p.pid and m.get("word") in room.choices
            g = room.gen
        if ok:
            begin_draw(room, m["word"], g)
    elif t == "draw":
        with room.lock:
            can = (room.drawer_pid == p.pid and room.state == "drawing") or room.state in ("lobby", "over")
            if can and len(room.draw_ops) < 4000:
                seg = m.get("s")
                if (isinstance(seg, list) and len(seg) == 4 and
                        all(isinstance(v, (int, float)) and 0 <= v <= 1.5 for v in seg)):
                    op = {"s": [round(v, 3) for v in seg],
                          "c": str(m.get("c", "#000000"))[:9],
                          "w": max(1, min(60, int(m.get("w", 6))))}
                    room.draw_ops.append(op)
                else:
                    op = None
            else:
                op = None
        if op:
            broadcast(room, {"t": "draw", "s": op["s"], "c": op["c"], "w": op["w"]},
                      exclude_pid=p.pid)
    elif t == "clear":
        with room.lock:
            can = (room.drawer_pid == p.pid and room.state == "drawing") or room.state in ("lobby", "over")
            if can:
                room.draw_ops = []
        if can:
            broadcast(room, {"t": "clear"}, exclude_pid=p.pid)
    elif t == "guess":
        if p.conn.spam_ok():
            handle_guess(room, p, str(m.get("text", "")))
    elif t == "chat":
        if p.conn.spam_ok():
            chat_all(room, p, str(m.get("text", ""))[:120])
    elif t == "skip":
        with room.lock:
            ok = room.state == "drawing" and room.drawer_pid == p.pid
            g = room.gen
        if ok:
            end_turn(room, g, "skip")
    elif t == "again":
        with room.lock:
            ok = p.host and room.state == "over"
        if ok:
            start_game(room, p.pid)

# ---------- HTTP + WS handler ----------

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            try:
                with open(os.path.join(BASE, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(500)
        elif path == "/ws":
            self.ws()
        else:
            self.send_error(404)

    def ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        conn = Conn(self)
        room = None
        me = None
        try:
            while True:
                raw = conn.recv_text()
                if raw is None:
                    break
                try:
                    m = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(m, dict) or not conn.spam_ok():
                    continue
                t = m.get("t")
                if room is None:
                    if t == "create":
                        name = str(m.get("name", ""))[:16].strip() or "Névtelen"
                        rounds = max(1, min(5, int(m.get("rounds", 3)) if str(m.get("rounds", "3")).isdigit() else 3))
                        pin = new_pin()
                        r = Room(pin)
                        r.rounds = rounds
                        with rooms_lock:
                            rooms[pin] = r
                        me = add_player(r, name, conn)
                        room = r
                        conn.send({"t": "welcome", "pin": pin, "you": me.pid,
                                   "rounds": rounds, "host": True})
                        broadcast_state(r)
                    elif t == "join":
                        pin = str(m.get("pin", "")).strip()
                        name = str(m.get("name", ""))[:16].strip() or "Névtelen"
                        with rooms_lock:
                            r = rooms.get(pin)
                        if not r:
                            conn.send({"t": "error", "msg": "Nincs ilyen szoba (rossz PIN?)"})
                            continue
                        with r.lock:
                            full = len(r.players) >= MAX_PLAYERS
                        if full:
                            conn.send({"t": "error", "msg": "A szoba tele van!"})
                            continue
                        me = add_player(r, name, conn)
                        room = r
                        with room.lock:
                            ops = list(room.draw_ops[-1500:])
                        conn.send({"t": "welcome", "pin": pin, "you": me.pid,
                                   "rounds": room.rounds, "host": False})
                        conn.send({"t": "replay", "ops": ops})
                        broadcast_state(room)
                        broadcast(room, {"t": "sys", "msg": me.name + " csatlakozott!"},
                                  exclude_pid=me.pid)
                    else:
                        conn.send({"t": "error", "msg": "Előbb csatlakozz egy szobához!"})
                    continue
                if me is None or room.by_pid(me.pid) is None:
                    break
                try:
                    handle_msg(room, me, m)
                except Exception:
                    pass
        finally:
            conn.alive = False
            if room is not None and me is not None:
                remove_player(room, me.pid)

    def log_message(self, *a):
        pass

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"FIRKA szerver fut:  http://localhost:{PORT}")
    print(f"Szavak száma: {len(WORDS)}")
    srv.serve_forever()

if __name__ == "__main__":
    main()