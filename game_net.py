"""
Client-side connection to the authoritative game server.
Replaces the lockstep input sync for online play.
"""
import socket, threading, json, time

GAME_SRV_HOST = "your-game-server.railway.app"  # update after deploy
GAME_SRV_PORT = 55300

def _send(sock, msg):
    try:
        data = json.dumps(msg).encode()
        sock.sendall(len(data).to_bytes(4,'big') + data)
        return True
    except: return False

class GameNet:
    def __init__(self, host=GAME_SRV_HOST, port=GAME_SRV_PORT):
        self.host        = host
        self.port        = port
        self.sock        = None
        self.role        = None
        self.match_id    = None
        self._status     = "disconnected"
        self._lock       = threading.Lock()
        self._state_buf  = None   # latest game_state from server
        self._pending    = []     # round_end / match_end messages

    @property
    def status(self):
        with self._lock: return self._status

    def connect(self, match_id, role, mode, seed):
        self.match_id = match_id
        self.role     = role
        threading.Thread(target=self._connect_thread,
                         args=(match_id, role, mode, seed), daemon=True).start()

    def _connect_thread(self, match_id, role, mode, seed):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((self.host, self.port))
            s.settimeout(None)
            self.sock = s
            # Send handshake
            _send(s, {"match_id": match_id, "role": role, "mode": mode, "seed": seed})
            with self._lock: self._status = "connected"
            print(f"[GN] Connected as {role} to match {match_id}")
            self._recv_loop()
        except Exception as e:
            print(f"[GN] Connect error: {e}")
            with self._lock: self._status = "disconnected"

    def _recv_loop(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(8192)
                if not chunk: break
                buf += chunk
                while len(buf) >= 4:
                    ln = int.from_bytes(buf[:4], 'big')
                    if len(buf) < 4 + ln: break
                    data = buf[4:4+ln]; buf = buf[4+ln:]
                    try:
                        msg = json.loads(data.decode())
                        self._handle(msg)
                    except: pass
            except Exception as e:
                print(f"[GN] Recv error: {e}"); break
        with self._lock: self._status = "disconnected"

    def _handle(self, msg):
        t = msg.get('type')
        if t == 'game_state':
            with self._lock: self._state_buf = msg
        elif t in ('round_end', 'match_end', 'round_start'):
            with self._lock: self._pending.append(msg)

    def get_state(self):
        with self._lock:
            s = self._state_buf; self._state_buf = None; return s

    def pop_event(self):
        with self._lock:
            if self._pending: return self._pending.pop(0)
        return None

    def send_input(self, keys, events, frame):
        if self.sock:
            _send(self.sock, {"type": "input", "keys": keys, "events": events, "frame": frame})

    def send_mode(self, mode):
        if self.sock:
            _send(self.sock, {"type": "mode", "mode": mode})

    def send_ready(self):
        if self.sock:
            _send(self.sock, {"type": "ready"})

    def is_disconnected(self):
        with self._lock: return self._status == "disconnected"

    def disconnect(self):
        with self._lock: self._status = "disconnected"
        try:
            if self.sock: self.sock.close()
        except: pass
