import socket
import threading
import json
import sqlite3
import hashlib
import time
import random
import os

HOST = "0.0.0.0"
PORT = int(os.environ.get("LOBBY_PORT", 55200))
DB   = "lobby.db"

# ─────────────────────────────────────────────
#  Database
# ─────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            elo      INTEGER DEFAULT 1000,
            wins     INTEGER DEFAULT 0,
            losses   INTEGER DEFAULT 0,
            created  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            room     TEXT NOT NULL,
            username TEXT NOT NULL,
            message  TEXT NOT NULL,
            ts       INTEGER DEFAULT 0
        );
    """)
    con.commit()
    con.close()

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def db_register(username, password):
    try:
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO users (username,password,created) VALUES (?,?,?)",
                    (username, hash_pw(password), int(time.time())))
        con.commit()
        con.close()
        return True, "ok"
    except sqlite3.IntegrityError:
        return False, "Username already taken"
    except Exception as e:
        return False, str(e)

def db_login(username, password):
    con = sqlite3.connect(DB)
    row = con.execute("SELECT id,password,elo,wins,losses FROM users WHERE username=?",
                      (username,)).fetchone()
    con.close()
    if not row:
        return False, "User not found", {}
    if row[1] != hash_pw(password):
        return False, "Wrong password", {}
    return True, "ok", {"elo": row[2], "wins": row[3], "losses": row[4]}

def db_update_elo(username, new_elo, won):
    con = sqlite3.connect(DB)
    if won:
        con.execute("UPDATE users SET elo=?,wins=wins+1 WHERE username=?", (new_elo, username))
    else:
        con.execute("UPDATE users SET elo=?,losses=losses+1 WHERE username=?", (new_elo, username))
    con.commit()
    con.close()

def db_leaderboard(limit=10):
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT username,elo,wins,losses FROM users ORDER BY elo DESC LIMIT ?",
                       (limit,)).fetchall()
    con.close()
    return [{"username":r[0],"elo":r[1],"wins":r[2],"losses":r[3]} for r in rows]

def db_save_chat(room, username, message):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO chat_log (room,username,message,ts) VALUES (?,?,?,?)",
                (room, username, message, int(time.time())))
    con.commit()
    con.close()

def db_recent_chat(room, limit=50):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT username,message,ts FROM chat_log WHERE room=? ORDER BY ts DESC LIMIT ?",
        (room, limit)).fetchall()
    con.close()
    return [{"username":r[0],"message":r[1],"ts":r[2]} for r in reversed(rows)]

# ─────────────────────────────────────────────
#  Message framing
# ─────────────────────────────────────────────
def send_msg(sock, msg):
    try:
        data   = json.dumps(msg).encode()
        length = len(data).to_bytes(4, 'big')
        sock.sendall(length + data)
        return True
    except Exception:
        return False

def recv_msg(sock):
    def recv_all(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data
    raw = recv_all(4)
    if not raw: return None
    length = int.from_bytes(raw, 'big')
    data   = recv_all(length)
    if not data: return None
    return json.loads(data.decode())

# ─────────────────────────────────────────────
#  Server state
# ─────────────────────────────────────────────
lock           = threading.Lock()
clients        = {}   # username -> {"sock", "room", "queue", "elo"}
ranked_queue   = []   # list of usernames waiting for ranked match
unranked_queue = []   # list of usernames waiting for unranked match
active_matches = {}   # match_id -> {"p1","p2","seed","type"}
GAME_SERVER    = os.environ.get("GAME_SERVER", "trolley.proxy.rlwy.net:36942")

# ─────────────────────────────────────────────
#  Broadcast helpers
# ─────────────────────────────────────────────
def broadcast_room(room, msg, exclude=None):
    with lock:
        targets = [(u, c["sock"]) for u, c in clients.items()
                   if c["room"] == room and u != exclude]
    for u, s in targets:
        send_msg(s, msg)

def broadcast_all(msg, exclude=None):
    with lock:
        targets = [(u, c["sock"]) for u, c in clients.items() if u != exclude]
    for u, s in targets:
        send_msg(s, msg)

def player_list():
    with lock:
        return [{"username": u, "elo": c["elo"], "room": c["room"]}
                for u, c in clients.items()]

def send_player_list_update():
    broadcast_all({"type": "player_list", "players": player_list()})

# ─────────────────────────────────────────────
#  ELO calculation
# ─────────────────────────────────────────────
def calc_elo(winner_elo, loser_elo, k=32):
    exp_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    exp_l = 1 - exp_w
    new_w = round(winner_elo + k * (1 - exp_w))
    new_l = round(loser_elo  + k * (0 - exp_l))
    return new_w, new_l

# ─────────────────────────────────────────────
#  Matchmaking
# ─────────────────────────────────────────────
def try_make_match(queue, match_type):
    with lock:
        if len(queue) < 2:
            return
        p1 = queue.pop(0)
        p2 = queue.pop(0)
        # Verify both still connected
        if p1 not in clients or p2 not in clients:
            # Put back whoever is still here
            if p1 in clients: queue.insert(0, p1)
            if p2 in clients: queue.insert(0, p2)
            return
        seed      = random.randint(1, 999999)
        match_id  = f"{p1}_{p2}_{seed}"
        active_matches[match_id] = {
            "p1": p1, "p2": p2, "seed": seed, "type": match_type,
            "p1_elo": clients[p1]["elo"], "p2_elo": clients[p2]["elo"],
        }
        clients[p1]["queue"] = None
        clients[p2]["queue"] = None
        s1 = clients[p1]["sock"]
        s2 = clients[p2]["sock"]

    send_msg(s1, {"type":"match_found","role":"p1","seed":seed,
                  "opponent":p2,"match_id":match_id,"match_type":match_type,
                  "game_server":GAME_SERVER})
    send_msg(s2, {"type":"match_found","role":"p2","seed":seed,
                  "opponent":p1,"match_id":match_id,"match_type":match_type,
                  "game_server":GAME_SERVER})
    print(f"[LOBBY] Match made: {p1} vs {p2} (seed={seed}, type={match_type})")

def matchmaking_loop():
    while True:
        try_make_match(ranked_queue,   "ranked")
        try_make_match(unranked_queue, "unranked")
        time.sleep(1)

# ─────────────────────────────────────────────
#  Client handler
# ─────────────────────────────────────────────
def handle_client(sock, addr):
    username = None
    try:
        sock.settimeout(30)
        # ── Auth ──────────────────────────────────────────────────────────
        msg = recv_msg(sock)
        if not msg or msg.get("type") not in ("login", "register"):
            send_msg(sock, {"type":"error","msg":"Must login or register first"})
            return

        uname = str(msg.get("username","")).strip()[:20]
        pw    = str(msg.get("password",""))

        if msg["type"] == "register":
            ok, reason = db_register(uname, pw)
            if not ok:
                send_msg(sock, {"type":"auth_fail","msg":reason}); return
            _, _, stats = db_login(uname, pw)
        else:
            ok, reason, stats = db_login(uname, pw)
            if not ok:
                send_msg(sock, {"type":"auth_fail","msg":reason}); return

        # Check not already logged in
        with lock:
            if uname in clients:
                send_msg(sock, {"type":"auth_fail","msg":"Already logged in"}); return
            username = uname
            clients[username] = {"sock":sock,"room":"#Main","queue":None,"elo":stats["elo"]}

        send_msg(sock, {"type":"auth_ok","username":username,"stats":stats,
                        "game_server":GAME_SERVER})

        # Send recent chat history
        history = db_recent_chat("#Main")
        send_msg(sock, {"type":"chat_history","room":"#Main","messages":history})

        # Notify others
        broadcast_all({"type":"player_join","username":username,"elo":stats["elo"]}, exclude=username)
        send_player_list_update()

        print(f"[LOBBY] {username} logged in from {addr}")

        sock.settimeout(None)

        # ── Main loop ─────────────────────────────────────────────────────
        while True:
            msg = recv_msg(sock)
            if not msg:
                break

            t = msg.get("type")

            if t == "chat":
                room    = msg.get("room", "#Main")
                message = str(msg.get("message",""))[:200]
                db_save_chat(room, username, message)
                broadcast_room(room, {
                    "type":"chat","room":room,
                    "username":username,"message":message,
                    "ts":int(time.time())
                })

            elif t == "join_room":
                room = msg.get("room","#Main")
                with lock:
                    clients[username]["room"] = room
                history = db_recent_chat(room)
                send_msg(sock, {"type":"room_joined","room":room})
                send_msg(sock, {"type":"chat_history","room":room,"messages":history})
                send_player_list_update()

            elif t == "queue_ranked":
                with lock:
                    if username not in ranked_queue:
                        ranked_queue.append(username)
                        clients[username]["queue"] = "ranked"
                send_msg(sock, {"type":"queued","queue":"ranked"})

            elif t == "queue_unranked":
                with lock:
                    if username not in unranked_queue:
                        unranked_queue.append(username)
                        clients[username]["queue"] = "unranked"
                send_msg(sock, {"type":"queued","queue":"unranked"})

            elif t == "dequeue":
                with lock:
                    if username in ranked_queue:   ranked_queue.remove(username)
                    if username in unranked_queue: unranked_queue.remove(username)
                    clients[username]["queue"] = None
                send_msg(sock, {"type":"dequeued"})

            elif t == "challenge":
                target = msg.get("target")
                with lock:
                    target_sock = clients[target]["sock"] if target in clients else None
                if target_sock:
                    send_msg(target_sock, {"type":"challenge","from":username,"elo":clients[username]["elo"]})

            elif t == "challenge_accept":
                challenger = msg.get("challenger")
                with lock:
                    c_sock = clients[challenger]["sock"] if challenger in clients else None
                    seed   = random.randint(1, 999999)
                    match_id = f"{challenger}_{username}_{seed}"
                    if challenger in clients and username in clients:
                        active_matches[match_id] = {
                            "p1":challenger,"p2":username,"seed":seed,"type":"unranked",
                            "p1_elo":clients[challenger]["elo"],"p2_elo":clients[username]["elo"],
                        }
                if c_sock:
                    send_msg(c_sock,  {"type":"match_found","role":"p1","seed":seed,
                                       "opponent":username,"match_id":match_id,
                                       "match_type":"unranked","game_server":GAME_SERVER})
                    send_msg(sock,    {"type":"match_found","role":"p2","seed":seed,
                                       "opponent":challenger,"match_id":match_id,
                                       "match_type":"unranked","game_server":GAME_SERVER})

            elif t == "challenge_decline":
                challenger = msg.get("challenger")
                with lock:
                    c_sock = clients[challenger]["sock"] if challenger in clients else None
                if c_sock:
                    send_msg(c_sock, {"type":"challenge_declined","by":username})

            elif t == "match_result":
                match_id = msg.get("match_id")
                winner   = msg.get("winner")
                with lock:
                    match = active_matches.pop(match_id, None)
                if match and match["type"] == "ranked" and winner:
                    p1, p2 = match["p1"], match["p2"]
                    p1_elo, p2_elo = match["p1_elo"], match["p2_elo"]
                    if winner == p1:
                        new_p1, new_p2 = calc_elo(p1_elo, p2_elo)
                    else:
                        new_p2, new_p1 = calc_elo(p2_elo, p1_elo)
                    db_update_elo(p1, new_p1, winner==p1)
                    db_update_elo(p2, new_p2, winner==p2)
                    with lock:
                        if p1 in clients: clients[p1]["elo"] = new_p1
                        if p2 in clients: clients[p2]["elo"] = new_p2
                    send_player_list_update()

            elif t == "leaderboard":
                send_msg(sock, {"type":"leaderboard","data":db_leaderboard()})

            elif t == "ping":
                send_msg(sock, {"type":"pong"})

    except Exception as e:
        print(f"[LOBBY] Client error ({username or addr}): {e}")
    finally:
        if username:
            with lock:
                clients.pop(username, None)
                if username in ranked_queue:   ranked_queue.remove(username)
                if username in unranked_queue: unranked_queue.remove(username)
            broadcast_all({"type":"player_leave","username":username})
            send_player_list_update()
            print(f"[LOBBY] {username} disconnected")
        try: sock.close()
        except: pass

# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=matchmaking_loop, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    print(f"[LOBBY] Server running on port {PORT}")
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
