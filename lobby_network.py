import socket
import threading
import json
import time

LOBBY_HOST = "your-lobby.railway.app"   # replace with your Railway lobby URL
LOBBY_PORT = 55200

def _send_msg(sock, msg):
    try:
        data = json.dumps(msg).encode()
        sock.sendall(len(data).to_bytes(4,'big') + data)
        return True
    except: return False

class LobbyNetwork:
    def __init__(self, host=LOBBY_HOST, port=LOBBY_PORT):
        self.host            = host
        self.port            = port
        self.sock            = None
        self.username        = None
        self.elo             = 1000
        self.stats           = {}
        self._status         = "connecting"  # disconnected/connecting/connected
        self._lock           = threading.Lock()
        self._recv_buf       = b""
        self._pending        = []   # incoming messages queue
        self.players         = []   # online player list
        self.chat_messages   = []   # {"room","username","message","ts"}
        self.match_found     = None # set when server sends match_found
        self.challenge_from  = None # {"from","elo"} pending challenge
        self.leaderboard     = []
        self.rooms           = []   # list of room dicts from server
        self.current_room    = None # room_id if in a room

    @property
    def status(self):
        with self._lock: return self._status

    def connect(self):
        with self._lock: self._status = "connecting"
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((self.host, self.port))
            s.settimeout(None)
            self.sock = s
            with self._lock: self._status = "connected"
            print(f"[LOBBY] Connected to {self.host}:{self.port}")
            threading.Thread(target=self._recv_loop, daemon=True).start()
        except Exception as e:
            print(f"[LOBBY] Connection failed: {e}")
            with self._lock: self._status = "disconnected"

    def login(self, username, password):
        if not self.sock: return
        _send_msg(self.sock, {"type":"login","username":username,"password":password})

    def register(self, username, password):
        if not self.sock: return
        _send_msg(self.sock, {"type":"register","username":username,"password":password})

    def send_chat(self, room, message):
        if self.sock: _send_msg(self.sock, {"type":"chat","room":room,"message":message})

    def join_room(self, room):
        if self.sock: _send_msg(self.sock, {"type":"join_room","room":room})

    def queue_ranked(self):
        if self.sock: _send_msg(self.sock, {"type":"queue_ranked"})

    def queue_unranked(self):
        if self.sock: _send_msg(self.sock, {"type":"queue_unranked"})

    def dequeue(self):
        if self.sock: _send_msg(self.sock, {"type":"dequeue"})

    def challenge(self, target):
        if self.sock: _send_msg(self.sock, {"type":"challenge","target":target})

    def accept_challenge(self, challenger):
        if self.sock: _send_msg(self.sock, {"type":"challenge_accept","challenger":challenger})

    def decline_challenge(self, challenger):
        if self.sock: _send_msg(self.sock, {"type":"challenge_decline","challenger":challenger})

    def send_match_result(self, match_id, winner):
        if self.sock: _send_msg(self.sock, {"type":"match_result","match_id":match_id,"winner":winner})

    def get_leaderboard(self):
        if self.sock: _send_msg(self.sock, {"type":"leaderboard"})

    def create_room(self, name, max_players, mode, password=""):
        if self.sock: _send_msg(self.sock, {"type":"create_room","name":name,
                                             "max_players":max_players,"mode":mode,
                                             "password":password})

    def join_room(self, room_id, password=""):
        if self.sock: _send_msg(self.sock, {"type":"join_room","room_id":room_id,
                                             "password":password})

    def leave_room(self, room_id):
        if self.sock: _send_msg(self.sock, {"type":"leave_room","room_id":room_id})

    def start_room(self, room_id):
        if self.sock: _send_msg(self.sock, {"type":"start_room","room_id":room_id})

    def pop_message(self):
        with self._lock:
            if self._pending: return self._pending.pop(0)
        return None

    def disconnect(self):
        with self._lock: self._status = "disconnected"
        try:
            if self.sock: self.sock.close()
        except: pass
        self.sock = None

    def _recv_loop(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    print("[LOBBY] Disconnected")
                    with self._lock: self._status = "disconnected"
                    break
                buf += chunk
                while len(buf) >= 4:
                    length = int.from_bytes(buf[:4],'big')
                    if len(buf) < 4 + length: break
                    data   = buf[4:4+length]
                    buf    = buf[4+length:]
                    try:
                        msg = json.loads(data.decode())
                        self._handle(msg)
                    except: pass
            except Exception as e:
                print(f"[LOBBY] Recv error: {e}")
                with self._lock: self._status = "disconnected"
                break

    def _ping_loop(self):
        import time
        while True:
            time.sleep(15)  # ping every 15s to keep connection alive
            with self._lock:
                status = self._status
            if status != "connected":
                break
            if self.sock:
                try:
                    _send_msg(self.sock, {"type": "ping"})
                except:
                    break

    def _handle(self, msg):
        t = msg.get("type")
        if t == "auth_ok":
            self.username = msg["username"]
            self.stats    = msg.get("stats", {})
            self.elo      = self.stats.get("elo", 1000)
            with self._lock: self._pending.append(msg)
        elif t == "auth_fail":
            with self._lock: self._pending.append(msg)
        elif t == "chat":
            self.chat_messages.append(msg)
            if len(self.chat_messages) > 200:
                self.chat_messages = self.chat_messages[-200:]
            with self._lock: self._pending.append(msg)
        elif t == "chat_history":
            for m in msg.get("messages",[]):
                m["room"] = msg.get("room","#Main")
                self.chat_messages.append(m)
            with self._lock: self._pending.append(msg)
        elif t == "player_list":
            self.players = msg.get("players",[])
        elif t == "player_join":
            with self._lock: self._pending.append(msg)
        elif t == "player_leave":
            with self._lock: self._pending.append(msg)
        elif t == "match_found":
            self.match_found = msg
            with self._lock: self._pending.append(msg)
        elif t == "challenge":
            self.challenge_from = {"from": msg["from"], "elo": msg.get("elo",1000)}
            with self._lock: self._pending.append(msg)
        elif t == "challenge_declined":
            with self._lock: self._pending.append(msg)
        elif t == "queued":
            with self._lock: self._pending.append(msg)
        elif t == "dequeued":
            with self._lock: self._pending.append(msg)
        elif t == "leaderboard":
            self.leaderboard = msg.get("data",[])
            with self._lock: self._pending.append(msg)
        elif t == "pong":
            pass
        elif t == "rooms_update":
            self.rooms = msg.get("rooms", [])
        elif t in ("room_created","room_joined_game","room_player_joined",
                   "room_player_left","room_error"):
            with self._lock: self._pending.append(msg)
        elif t == "error":
            with self._lock: self._pending.append(msg)
