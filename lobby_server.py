import socket, threading, json, sqlite3, hashlib, time, random, os

HOST = "0.0.0.0"
PORT = int(os.environ.get("LOBBY_PORT", 55200))
DB   = "lobby.db"

# ─── Database ────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            elo INTEGER DEFAULT 1000,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            created INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            username TEXT NOT NULL,
            message TEXT NOT NULL,
            ts INTEGER DEFAULT 0
        );
    """)
    con.commit(); con.close()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def db_register(username, password):
    try:
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO users (username,password,created) VALUES (?,?,?)",
                    (username, hash_pw(password), int(time.time())))
        con.commit(); con.close()
        return True, "ok"
    except sqlite3.IntegrityError: return False, "Username already taken"
    except Exception as e: return False, str(e)

def db_login(username, password):
    con = sqlite3.connect(DB)
    row = con.execute("SELECT id,password,elo,wins,losses FROM users WHERE username=?",
                      (username,)).fetchone()
    con.close()
    if not row: return False, "User not found", {}
    if row[1] != hash_pw(password): return False, "Wrong password", {}
    return True, "ok", {"elo": row[2], "wins": row[3], "losses": row[4]}

def db_update_elo(username, new_elo, won):
    con = sqlite3.connect(DB)
    if won: con.execute("UPDATE users SET elo=?,wins=wins+1 WHERE username=?", (new_elo, username))
    else:   con.execute("UPDATE users SET elo=?,losses=losses+1 WHERE username=?", (new_elo, username))
    con.commit(); con.close()

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
    con.commit(); con.close()

def db_recent_chat(room, limit=50):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT username,message,ts FROM chat_log WHERE room=? ORDER BY ts DESC LIMIT ?",
        (room, limit)).fetchall()
    con.close()
    return [{"username":r[0],"message":r[1],"ts":r[2]} for r in reversed(rows)]

# ─── Framing ─────────────────────────────────────────────────────────────────
def send_msg(sock, msg):
    try:
        data = json.dumps(msg).encode()
        sock.sendall(len(data).to_bytes(4,'big') + data)
        return True
    except: return False

def recv_msg(sock):
    def ra(n):
        d = b""
        while len(d)<n:
            c=sock.recv(n-len(d))
            if not c: return None
            d+=c
        return d
    raw=ra(4)
    if not raw: return None
    data=ra(int.from_bytes(raw,'big'))
    if not data: return None
    return json.loads(data.decode())

# ─── Server state ─────────────────────────────────────────────────────────────
lock         = threading.Lock()
clients      = {}        # username -> {sock, room, queue, elo}
ranked_queue = []
active_matches = {}
rooms        = {}        # room_id -> {name, host, players, max_players, mode, password, status}
GAME_SERVER  = os.environ.get("GAME_SERVER", "trolley.proxy.rlwy.net:36942")

# ─── Helpers ─────────────────────────────────────────────────────────────────
def broadcast_room(room, msg, exclude=None):
    with lock:
        targets = [(u,c["sock"]) for u,c in clients.items()
                   if c["room"]==room and u!=exclude]
    for _,s in targets: send_msg(s, msg)

def broadcast_all(msg, exclude=None):
    with lock:
        targets = [(u,c["sock"]) for u,c in clients.items() if u!=exclude]
    for _,s in targets: send_msg(s, msg)

def player_list():
    with lock:
        return [{"username":u,"elo":c["elo"],"room":c["room"]}
                for u,c in clients.items()]

def send_player_list_update():
    broadcast_all({"type":"player_list","players":player_list()})

def rooms_list():
    with lock:
        return [{"id":rid,"name":r["name"],"host":r["host"],
                 "players":len(r["players"]),"max_players":r["max_players"],
                 "mode":r["mode"],"has_password":bool(r["password"]),
                 "status":r["status"]}
                for rid,r in rooms.items()]

def broadcast_rooms_update():
    broadcast_all({"type":"rooms_update","rooms":rooms_list()})

def calc_elo(w_elo, l_elo, k=32):
    exp_w = 1/(1+10**((l_elo-w_elo)/400))
    new_w = round(w_elo + k*(1-exp_w))
    new_l = round(l_elo  + k*(0-(1-exp_w)))
    return new_w, new_l

# ─── Ranked matchmaking ───────────────────────────────────────────────────────
def try_make_ranked_match():
    with lock:
        if len(ranked_queue) < 2: return
        p1 = ranked_queue.pop(0)
        p2 = ranked_queue.pop(0)
        if p1 not in clients or p2 not in clients:
            if p1 in clients: ranked_queue.insert(0,p1)
            if p2 in clients: ranked_queue.insert(0,p2)
            return
        seed     = random.randint(1,999999)
        match_id = f"{p1}_{p2}_{seed}"
        active_matches[match_id] = {"p1":p1,"p2":p2,"seed":seed,"type":"ranked",
                                     "p1_elo":clients[p1]["elo"],"p2_elo":clients[p2]["elo"]}
        clients[p1]["queue"] = None
        clients[p2]["queue"] = None
        s1 = clients[p1]["sock"]
        s2 = clients[p2]["sock"]
    send_msg(s1,{"type":"match_found","role":"p1","seed":seed,"opponent":p2,
                  "match_id":match_id,"match_type":"ranked","game_server":GAME_SERVER})
    send_msg(s2,{"type":"match_found","role":"p2","seed":seed,"opponent":p1,
                  "match_id":match_id,"match_type":"ranked","game_server":GAME_SERVER})
    print(f"[LOBBY] Ranked match: {p1} vs {p2}")

def matchmaking_loop():
    while True:
        try_make_ranked_match()
        time.sleep(1)

# ─── Room helpers ─────────────────────────────────────────────────────────────
def start_room_match(room_id):
    with lock:
        room = rooms.get(room_id)
        if not room or len(room["players"]) < 2: return
        room["status"] = "playing"
        players = room["players"][:]
        seed    = random.randint(1,999999)
        match_id = f"room_{room_id}_{seed}"
        socks = {u: clients[u]["sock"] for u in players if u in clients}
    # For now handle 2-player rooms; 3-4 will be added later
    if len(players) >= 2:
        p1,p2 = players[0],players[1]
        active_matches[match_id] = {"p1":p1,"p2":p2,"seed":seed,"type":"room",
                                     "room_id":room_id}
        if p1 in socks:
            send_msg(socks[p1],{"type":"match_found","role":"p1","seed":seed,
                                 "opponent":p2,"match_id":match_id,
                                 "match_type":"room","game_server":GAME_SERVER})
        if p2 in socks:
            send_msg(socks[p2],{"type":"match_found","role":"p2","seed":seed,
                                 "opponent":p1,"match_id":match_id,
                                 "match_type":"room","game_server":GAME_SERVER})

# ─── Client handler ───────────────────────────────────────────────────────────
def handle_client(sock, addr):
    username = None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(30)
        msg = recv_msg(sock)
        if not msg or msg.get("type") not in ("login","register"):
            send_msg(sock,{"type":"error","msg":"Must login or register first"}); return

        uname = str(msg.get("username","")).strip()[:20]
        pw    = str(msg.get("password",""))

        if msg["type"] == "register":
            ok,reason = db_register(uname,pw)
            if not ok: send_msg(sock,{"type":"auth_fail","msg":reason}); return
            _,_,stats = db_login(uname,pw)
        else:
            ok,reason,stats = db_login(uname,pw)
            if not ok: send_msg(sock,{"type":"auth_fail","msg":reason}); return

        with lock:
            if uname in clients:
                send_msg(sock,{"type":"auth_fail","msg":"Already logged in"}); return
            username = uname
            clients[username] = {"sock":sock,"room":"#Main","queue":None,"elo":stats["elo"]}

        send_msg(sock,{"type":"auth_ok","username":username,"stats":stats,
                       "game_server":GAME_SERVER})
        history = db_recent_chat("#Main")
        send_msg(sock,{"type":"chat_history","room":"#Main","messages":history})
        broadcast_all({"type":"player_join","username":username,"elo":stats["elo"]},exclude=username)
        send_player_list_update()
        send_msg(sock,{"type":"rooms_update","rooms":rooms_list()})
        print(f"[LOBBY] {username} logged in from {addr}")
        sock.settimeout(None)

        while True:
            msg = recv_msg(sock)
            if not msg: break
            t = msg.get("type")

            if t == "chat":
                room    = msg.get("room","#Main")
                message = str(msg.get("message",""))[:200]
                db_save_chat(room,username,message)
                broadcast_room(room,{"type":"chat","room":room,"username":username,
                                     "message":message,"ts":int(time.time())})

            elif t == "join_room_chat":
                room = msg.get("room","#Main")
                with lock: clients[username]["room"] = room
                history = db_recent_chat(room)
                send_msg(sock,{"type":"room_joined","room":room})
                send_msg(sock,{"type":"chat_history","room":room,"messages":history})
                send_player_list_update()

            elif t == "queue_ranked":
                with lock:
                    if username not in ranked_queue:
                        ranked_queue.append(username)
                        clients[username]["queue"] = "ranked"
                send_msg(sock,{"type":"queued","queue":"ranked"})

            elif t == "dequeue":
                with lock:
                    if username in ranked_queue: ranked_queue.remove(username)
                    clients[username]["queue"] = None
                send_msg(sock,{"type":"dequeued"})

            elif t == "challenge":
                target = msg.get("target")
                with lock:
                    tsock = clients[target]["sock"] if target in clients else None
                    telo  = clients[target]["elo"]  if target in clients else 1000
                if tsock:
                    send_msg(tsock,{"type":"challenge","from":username,
                                    "elo":clients[username]["elo"]})

            elif t == "challenge_accept":
                challenger = msg.get("challenger")
                with lock:
                    csock = clients[challenger]["sock"] if challenger in clients else None
                    seed  = random.randint(1,999999)
                    mid   = f"{challenger}_{username}_{seed}"
                    if challenger in clients and username in clients:
                        active_matches[mid] = {"p1":challenger,"p2":username,"seed":seed,
                                                "type":"challenge",
                                                "p1_elo":clients[challenger]["elo"],
                                                "p2_elo":clients[username]["elo"]}
                if csock:
                    send_msg(csock, {"type":"match_found","role":"p1","seed":seed,
                                     "opponent":username,"match_id":mid,
                                     "match_type":"challenge","game_server":GAME_SERVER})
                    send_msg(sock,  {"type":"match_found","role":"p2","seed":seed,
                                     "opponent":challenger,"match_id":mid,
                                     "match_type":"challenge","game_server":GAME_SERVER})

            elif t == "challenge_decline":
                challenger = msg.get("challenger")
                with lock:
                    csock = clients[challenger]["sock"] if challenger in clients else None
                if csock:
                    send_msg(csock,{"type":"challenge_declined","by":username})

            # ── Room management ───────────────────────────────────────────
            elif t == "create_room":
                room_id  = f"room_{username}_{int(time.time())}"
                name     = str(msg.get("name","Room"))[:30]
                max_p    = max(2, min(4, int(msg.get("max_players",2))))
                mode     = msg.get("mode","versus")
                password = str(msg.get("password",""))
                with lock:
                    rooms[room_id] = {
                        "name":name,"host":username,"players":[username],
                        "max_players":max_p,"mode":mode,"password":password,
                        "status":"waiting"
                    }
                send_msg(sock,{"type":"room_created","room_id":room_id,"name":name})
                broadcast_rooms_update()

            elif t == "join_room":
                room_id  = msg.get("room_id")
                password = str(msg.get("password",""))
                with lock:
                    room = rooms.get(room_id)
                    if not room:
                        send_msg(sock,{"type":"room_error","msg":"Room not found"}); continue
                    if room["status"] != "waiting":
                        send_msg(sock,{"type":"room_error","msg":"Game already started"}); continue
                    if len(room["players"]) >= room["max_players"]:
                        send_msg(sock,{"type":"room_error","msg":"Room is full"}); continue
                    if room["password"] and room["password"] != password:
                        send_msg(sock,{"type":"room_error","msg":"Wrong password"}); continue
                    if username not in room["players"]:
                        room["players"].append(username)
                    # Notify room members
                    members = room["players"][:]
                    rsocks  = {u:clients[u]["sock"] for u in members if u in clients}
                send_msg(sock,{"type":"room_joined_game","room_id":room_id,
                               "name":room["name"],"players":members,
                               "host":room["host"],"mode":room["mode"]})
                for u,s in rsocks.items():
                    if u != username:
                        send_msg(s,{"type":"room_player_joined","room_id":room_id,
                                    "username":username,"players":members})
                broadcast_rooms_update()

            elif t == "leave_room":
                room_id = msg.get("room_id")
                with lock:
                    room = rooms.get(room_id)
                    if room and username in room["players"]:
                        room["players"].remove(username)
                        if not room["players"]:
                            del rooms[room_id]
                        elif room["host"] == username:
                            room["host"] = room["players"][0]
                        members = room["players"][:] if room_id in rooms else []
                        rsocks  = {u:clients[u]["sock"] for u in members if u in clients}
                for u,s in rsocks.items():
                    send_msg(s,{"type":"room_player_left","room_id":room_id,
                                "username":username,"players":members,
                                "new_host":rooms[room_id]["host"] if room_id in rooms else None})
                broadcast_rooms_update()

            elif t == "start_room":
                room_id = msg.get("room_id")
                with lock:
                    room = rooms.get(room_id)
                    is_host = room and room["host"] == username
                if is_host:
                    start_room_match(room_id)

            elif t == "match_result":
                mid    = msg.get("match_id")
                winner = msg.get("winner")
                with lock:
                    match = active_matches.pop(mid, None)
                if match and match["type"] == "ranked" and winner:
                    p1,p2 = match["p1"],match["p2"]
                    if winner==p1: new_p1,new_p2 = calc_elo(match["p1_elo"],match["p2_elo"])
                    else:          new_p2,new_p1 = calc_elo(match["p2_elo"],match["p1_elo"])
                    db_update_elo(p1,new_p1,winner==p1)
                    db_update_elo(p2,new_p2,winner==p2)
                    with lock:
                        if p1 in clients: clients[p1]["elo"]=new_p1
                        if p2 in clients: clients[p2]["elo"]=new_p2
                    send_player_list_update()

            elif t == "leaderboard":
                send_msg(sock,{"type":"leaderboard","data":db_leaderboard()})

            elif t == "ping":
                send_msg(sock,{"type":"pong"})

    except Exception as e:
        print(f"[LOBBY] Error ({username or addr}): {e}")
    finally:
        if username:
            with lock:
                clients.pop(username,None)
                if username in ranked_queue: ranked_queue.remove(username)
                # Remove from any rooms
                for rid,room in list(rooms.items()):
                    if username in room["players"]:
                        room["players"].remove(username)
                        if not room["players"]: del rooms[rid]
                        elif room["host"]==username and room["players"]:
                            room["host"]=room["players"][0]
            broadcast_all({"type":"player_leave","username":username})
            send_player_list_update()
            broadcast_rooms_update()
            print(f"[LOBBY] {username} disconnected")
        try: sock.close()
        except: pass

if __name__=="__main__":
    init_db()
    threading.Thread(target=matchmaking_loop,daemon=True).start()
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind((HOST,PORT)); srv.listen()
    print(f"[LOBBY] Server on port {PORT}")
    while True:
        conn,addr=srv.accept()
        threading.Thread(target=handle_client,args=(conn,addr),daemon=True).start()
