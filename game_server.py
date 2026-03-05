"""
PuyoXTetris – Authoritative Game Server
Runs on Railway alongside server.py (the relay).
Clients connect, send inputs, receive full game state each tick.
"""
import socket, threading, json, random, time, os, sys, copy

HOST = "0.0.0.0"
PORT = int(os.environ.get("GAME_SRV_PORT", 55300))

# ─── Shared constants (must match main.py) ───────────────────────────────────
COLS=6; ROWS=13; HIDDEN_ROWS=1; SPAWN_COL=2
SETTLE_DELAY=300; CHAIN_DELAY=500; GRAVITY_DELAY=80
STATE_PLAYING="playing"; STATE_SETTLING="settling"
STATE_RESOLVING="resolving"; STATE_GRAVITY="gravity"

T_COLS=10; T_ROWS=40; T_VISIBLE=20; T_HIDDEN=20; BLOCK_SIZE=24
T_SPAWN_ROW=19; T_FALL_NORMAL=1000; T_FALL_SOFT=50
T_LOCK_DELAY=500; T_LINE_FLASH=300

PUYO_COLORS=["red","green","blue","yellow","purple"]

LINE_SCORES={1:100,2:300,3:500,4:800}
LINES_TO_GARBAGE={1:0,2:1,3:2,4:4}
PPT_GARBAGE={(1,False):0,(2,False):1,(3,False):2,(4,False):4,
             (1,True):2,(2,True):4,(3,True):6,(4,True):8}
BTB_BONUS=1; ALL_CLEAR_BONUS=10; PUYO_ALL_CLEAR_BONUS=30
CHAIN_GARBAGE=[0,0,1,1,2,2,3,3,4,4,4]

TETROMINOES={
    'I':[[(0,1),(1,1),(2,1),(3,1)],[(2,0),(2,1),(2,2),(2,3)],
         [(0,2),(1,2),(2,2),(3,2)],[(1,0),(1,1),(1,2),(1,3)]],
    'O':[[(1,0),(2,0),(1,1),(2,1)]]*4,
    'T':[[(0,1),(1,1),(2,1),(1,0)],[(1,0),(1,1),(1,2),(2,1)],
         [(0,1),(1,1),(2,1),(1,2)],[(1,0),(1,1),(1,2),(0,1)]],
    'S':[[(1,0),(2,0),(0,1),(1,1)],[(1,0),(1,1),(2,1),(2,2)],
         [(1,1),(2,1),(0,2),(1,2)],[(0,0),(0,1),(1,1),(1,2)]],
    'Z':[[(0,0),(1,0),(1,1),(2,1)],[(2,0),(1,1),(2,1),(1,2)],
         [(0,1),(1,1),(1,2),(2,2)],[(1,0),(0,1),(1,1),(0,2)]],
    'L':[[(0,1),(1,1),(2,1),(2,0)],[(1,0),(1,1),(1,2),(2,2)],
         [(0,2),(0,1),(1,1),(2,1)],[(0,0),(1,0),(1,1),(1,2)]],
    'J':[[(0,0),(0,1),(1,1),(2,1)],[(1,0),(2,0),(1,1),(1,2)],
         [(0,1),(1,1),(2,1),(2,2)],[(1,0),(1,1),(0,2),(1,2)]],
}
PIECE_COLORS={'I':'cyan','O':'yellow','T':'purple','S':'green','Z':'red','L':'orange','J':'blue'}

SRS_KICKS={
    (0,1):[(0,0),(-1,0),(-1,-1),(0,2),(-1,2)],
    (1,0):[(0,0),(1,0),(1,1),(0,-2),(1,-2)],
    (1,2):[(0,0),(1,0),(1,1),(0,-2),(1,-2)],
    (2,1):[(0,0),(-1,0),(-1,-1),(0,2),(-1,2)],
    (2,3):[(0,0),(1,0),(1,-1),(0,2),(1,2)],
    (3,2):[(0,0),(-1,0),(-1,1),(0,-2),(-1,-2)],
    (3,0):[(0,0),(-1,0),(-1,1),(0,-2),(-1,-2)],
    (0,3):[(0,0),(1,0),(1,-1),(0,2),(1,2)],
}
SRS_KICKS_I={
    (0,1):[(0,0),(-2,0),(1,0),(-2,1),(1,-2)],
    (1,0):[(0,0),(2,0),(-1,0),(2,-1),(-1,2)],
    (1,2):[(0,0),(-1,0),(2,0),(-1,-2),(2,1)],
    (2,1):[(0,0),(1,0),(-2,0),(1,2),(-2,-1)],
    (2,3):[(0,0),(2,0),(-1,0),(2,-1),(-1,2)],
    (3,2):[(0,0),(-2,0),(1,0),(-2,1),(1,-2)],
    (3,0):[(0,0),(1,0),(-2,0),(1,2),(-2,-1)],
    (0,3):[(0,0),(-1,0),(2,0),(-1,-2),(2,1)],
}

# ─── Headless Puyo ────────────────────────────────────────────────────────────
class HPuyo:
    def __init__(self,x,y,color):
        self.grid_x=x; self.grid_y=y; self.color=color; self.removed=False

class HIndependentPuyo:
    FALL_INTERVAL_NORMAL=1500; FALL_INTERVAL_GARBAGE=200
    def __init__(self,x,y,color,is_garbage=False):
        self.grid_x=x; self.grid_y=y; self.color=color
        self.is_garbage=is_garbage; self.falling=True
        self.settling=False; self.removed=False
        self.fall_timer=0; self.settle_timer=0
        self.interval=self.FALL_INTERVAL_GARBAGE if is_garbage else self.FALL_INTERVAL_NORMAL
    def update(self,dt,board):
        if self.removed: return
        if self.falling:
            self.fall_timer+=dt
            if self.fall_timer>=self.interval:
                self.fall_timer=0
                if self.grid_y<0: self.grid_y+=1
                elif board.grid_is_empty(self.grid_x,self.grid_y+1): self.grid_y+=1
                else:
                    self.falling=False; self.settling=True; self.settle_timer=0
        elif self.settling:
            self.settle_timer+=dt
            if self.settle_timer>=SETTLE_DELAY:
                self.settling=False
                if 0<=self.grid_y<ROWS and 0<=self.grid_x<COLS:
                    board.grid[self.grid_y][self.grid_x]=HPuyo(self.grid_x,self.grid_y,self.color)
                self.removed=True

class HBoard:
    def __init__(self,seed,queue_offset=0):
        rng=random.Random(seed)
        base=[(x,y) for x in PUYO_COLORS for y in PUYO_COLORS]
        self.seed_queue=[(rng.choice(base)) for _ in range(2000)]
        self.grid=[[None]*COLS for _ in range(ROWS)]
        self.independent=[]; self.falling_pair=None
        self.queue_index=queue_offset
        self.current_pair=self.seed_queue[self.queue_index]; self.queue_index+=1
        self.next_pairs=[self.seed_queue[self.queue_index+i] for i in range(2)]
        self.score=0; self.chain_count=0
        self.pending_garbage=0; self.incoming_garbage=0; self.garbage_queue=0
        self.offset_timer=0; self.lost=False; self.state=STATE_PLAYING
        self.state_timer=0; self.pair_orientation=0; self.pair_x=SPAWN_COL; self.pair_y=0
        self.spawn_pair()

    def grid_is_empty(self,x,y):
        if x<0 or x>=COLS or y>=ROWS: return False
        if y<0: return True
        return self.grid[y][x] is None

    def advance_queue(self):
        self.current_pair=self.seed_queue[self.queue_index]; self.queue_index+=1
        self.next_pairs=[self.seed_queue[self.queue_index+i] for i in range(2)]

    def spawn_pair(self):
        if self.check_loss(): return
        c=self.current_pair
        # store as orientation/x/y
        self.pair_orientation=0; self.pair_x=SPAWN_COL; self.pair_y=0
        self.state=STATE_PLAYING

    def check_loss(self):
        for row in [1,2]:
            if self.grid[row][SPAWN_COL] is not None:
                self.lost=True; return True
        return False

    def apply_input(self,inp):
        """Apply a dict of input flags to the falling pair."""
        pass  # handled in pair physics below

    def any_moving(self):
        if self.state!=STATE_PLAYING: return True
        return any(p.falling or p.settling for p in self.independent)

    def commit_incoming(self,force=False):
        if self.incoming_garbage>0:
            self.offset_timer+=1
            if force or self.offset_timer>=3:
                self.garbage_queue+=self.incoming_garbage
                self.incoming_garbage=0; self.offset_timer=0

    def drop_garbage(self):
        if self.garbage_queue<=0 or self.any_moving(): return
        amount=min(30,self.garbage_queue); self.garbage_queue-=amount
        col_counts=[0]*COLS
        for i in range(amount):
            col=i%COLS; spawn_y=-col_counts[col]; col_counts[col]+=1
            self.independent.append(HIndependentPuyo(col,spawn_y,"garbage",is_garbage=True))

    def find_groups(self):
        visited=[[False]*COLS for _ in range(ROWS)]; groups=[]
        for y in range(ROWS):
            for x in range(COLS):
                p=self.grid[y][x]
                if p and not visited[y][x] and p.color!="garbage":
                    stack=[(x,y)]; group=[]; color=p.color
                    while stack:
                        sx,sy=stack.pop()
                        if not(0<=sx<COLS and 0<=sy<ROWS): continue
                        if visited[sy][sx]: continue
                        t=self.grid[sy][sx]
                        if not t or t.color!=color: continue
                        visited[sy][sx]=True; group.append((sx,sy))
                        stack+=[(sx+1,sy),(sx-1,sy),(sx,sy+1),(sx,sy-1)]
                    if len(group)>=4: groups.append(group)
        return groups

    def pop_groups(self,groups):
        total=0; colours_popped=set()
        for group in groups:
            total+=len(group)
            for x,y in group:
                p=self.grid[y][x]
                if p:
                    if p.color!="garbage": colours_popped.add(p.color)
                    p.removed=True; self.grid[y][x]=None
                for dx,dy in [(0,1),(0,-1),(1,0),(-1,0)]:
                    nx,ny=x+dx,y+dy
                    if 0<=nx<COLS and 0<=ny<ROWS:
                        g=self.grid[ny][nx]
                        if g and g.color=="garbage":
                            g.removed=True; self.grid[ny][nx]=None
        return total,colours_popped,groups

    def score_chain(self,cleared,chain_num,groups,colours_popped):
        cpt=[0,8,16,32,64,96,128,160,192,224,256]
        cp=cpt[min(chain_num-1,len(cpt)-1)]
        cbt=[0,0,3,6,12,24]
        cb=cbt[min(len(colours_popped),len(cbt)-1)]
        gbt=[0,0,0,0,0,2,3,4,5,6,7,10]
        gb=sum(gbt[min(len(g),len(gbt)-1)] for g in groups)
        mult=max(1,cp+cb+gb); gain=cleared*10*mult
        self.score+=gain; self.pending_garbage+=gain//70

    def apply_gravity_step(self):
        moved=False
        for x in range(COLS):
            for y in range(ROWS-2,-1,-1):
                if self.grid[y][x] and self.grid[y+1][x] is None:
                    self.grid[y+1][x]=self.grid[y][x]
                    self.grid[y+1][x].grid_y+=1
                    self.grid[y][x]=None; moved=True
        return moved

    def _check_puyo_all_clear(self):
        for y in range(ROWS):
            for x in range(COLS):
                if self.grid[y][x] is not None: return False
        return True

    def serialize(self):
        grid=[[self.grid[y][x].color if self.grid[y][x] else None
               for x in range(COLS)] for y in range(ROWS)]
        indep=[{"x":p.grid_x,"y":p.grid_y,"color":p.color,"garbage":p.is_garbage}
               for p in self.independent if not p.removed]
        return {
            "type":"puyo","grid":grid,"independent":indep,
            "state":self.state,"score":self.score,"chain":self.chain_count,
            "pair_x":self.pair_x,"pair_y":self.pair_y,"pair_ori":self.pair_orientation,
            "current_pair":list(self.current_pair),
            "next_pairs":[list(p) for p in self.next_pairs],
            "pending":self.pending_garbage,"incoming":self.incoming_garbage,
            "garbage_q":self.garbage_queue,"lost":self.lost,
        }

    def update(self,dt,inp):
        if self.lost: return
        self.independent=[p for p in self.independent if not p.removed]
        for p in self.independent: p.update(dt,self)
        if self.state==STATE_SETTLING:
            self.state_timer+=dt
            if self.state_timer>=SETTLE_DELAY:
                self.state_timer=0; self.state=STATE_RESOLVING
        elif self.state==STATE_RESOLVING:
            self.state_timer+=dt
            if self.state_timer>=CHAIN_DELAY:
                self.state_timer=0
                groups=self.find_groups()
                if groups:
                    self.chain_count+=1
                    cleared,colours_popped,groups=self.pop_groups(groups)
                    self.score_chain(cleared,self.chain_count,groups,colours_popped)
                    self.state=STATE_GRAVITY; self.state_timer=0
                else:
                    if self._check_puyo_all_clear():
                        self.pending_garbage+=PUYO_ALL_CLEAR_BONUS
                    self.chain_count=0; self.state=STATE_PLAYING
                    self.drop_garbage()
        elif self.state==STATE_GRAVITY:
            self.state_timer+=dt
            if self.state_timer>=GRAVITY_DELAY:
                self.state_timer=0
                if not self.apply_gravity_step():
                    self.state=STATE_RESOLVING; self.state_timer=0

# ─── Headless TetrisBoard ────────────────────────────────────────────────────
def _make_queue(seed,length=500):
    rng=random.Random(seed); bag='IOTSZLJ'; q=[]
    while len(q)<length:
        b=list(bag); rng.shuffle(b); q+=b
    return q[:length]

class HTetrisBoard:
    def __init__(self,seed):
        self.grid=[[None]*T_COLS for _ in range(T_ROWS)]
        self.piece_queue=_make_queue(seed)
        self.queue_pos=0
        self.piece_type=self.piece_queue[self.queue_pos]; self.queue_pos+=1
        self.piece_rot=0; self.piece_col=T_COLS//2-2; self.piece_row=T_SPAWN_ROW
        self.held_piece=None; self.hold_used=False
        self.score=0; self.lines_cleared=0; self.level=1
        self.pending_garbage=0; self.incoming_garbage=0; self.garbage_queue=0
        self.offset_timer=0; self.lost=False; self.state='playing'
        self.locking=False; self.lock_timer=0
        self.lines_to_clear=[]; self.line_flash_timer=0
        self.fall_timer=0; self.das_timer=0; self.das_active=False; self.das_dir=0
        self.last_spin=False; self.last_kick=(0,0); self.back_to_back=False
        self.chain_count=0; self.last_move=None
        self.popup_label=""; self.popup_timer=0
        # DAS state
        self._held_left=False; self._held_right=False; self._held_down=False

    def _next_piece(self):
        p=self.piece_queue[self.queue_pos]; self.queue_pos+=1; return p

    def _cells(self,pt,rot,col,row):
        return [(col+dc,row+dr) for dc,dr in TETROMINOES[pt][rot]]

    def _valid(self,pt,rot,col,row):
        for c,r in self._cells(pt,rot,col,row):
            if c<0 or c>=T_COLS or r>=T_ROWS: return False
            if r>=0 and self.grid[r][c] is not None: return False
        return True

    def _ghost_row(self):
        r=self.piece_row
        while self._valid(self.piece_type,self.piece_rot,self.piece_col,r+1): r+=1
        return r

    def _spawn(self):
        self.piece_type=self._next_piece()
        self.piece_rot=0; self.piece_col=T_COLS//2-2
        self.piece_row=T_SPAWN_ROW; self.hold_used=False
        self.locking=False; self.lock_timer=0; self.fall_timer=0
        self.last_move=None; self.last_kick=(0,0); self.last_spin=False
        self._check_loss()

    def _check_loss(self):
        mid=range(T_COLS//2-2,T_COLS//2+2)
        for c in mid:
            if self.grid[T_SPAWN_ROW+1][c] is not None:
                self.lost=True; self.state='game_over'; return True
        return False

    def _is_spin(self):
        if self.last_move!='rotate': return False
        pt=self.piece_type; c=self.piece_col; r=self.piece_row
        if pt=='T':
            corners=[(c,r),(c+2,r),(c,r+2),(c+2,r+2)]
            filled=sum(1 for cc,rr in corners
                       if cc<0 or cc>=T_COLS or rr<0 or rr>=T_ROWS
                       or self.grid[rr][cc] is not None)
            return filled>=3
        return self.last_kick!=(0,0)

    def _lock_piece(self):
        color=PIECE_COLORS[self.piece_type]
        for c,r in self._cells(self.piece_type,self.piece_rot,self.piece_col,self.piece_row):
            if 0<=r<T_ROWS and 0<=c<T_COLS: self.grid[r][c]=color
        self.last_spin=self._is_spin()
        self.commit_incoming()
        full=[r for r in range(T_ROWS) if all(self.grid[r][c] is not None for c in range(T_COLS))]
        if full:
            self.lines_to_clear=full; self.line_flash_timer=T_LINE_FLASH; self.state='line_flash'
        else:
            self.chain_count=0; self._drop_garbage_lines(); self._spawn()

    def _clear_lines(self):
        n=len(self.lines_to_clear); spin=self.last_spin
        for r in sorted(self.lines_to_clear,reverse=True):
            del self.grid[r]; self.grid.insert(0,[None]*T_COLS)
        self.lines_to_clear=[]; self.lines_cleared+=n
        self.level=self.lines_cleared//10+1; self.score+=LINE_SCORES.get(n,0)*self.level
        all_clear=all(self.grid[r][c] is None for r in range(T_HIDDEN,T_ROWS) for c in range(T_COLS))
        base=PPT_GARBAGE.get((min(n,4),spin),PPT_GARBAGE.get((min(n,4),False),0))
        is_special=(n==4 or spin)
        if is_special and self.back_to_back: base+=BTB_BONUS
        self.back_to_back=is_special
        self.chain_count+=1
        chain_bonus=CHAIN_GARBAGE[min(self.chain_count,len(CHAIN_GARBAGE)-1)]
        ac_bonus=ALL_CLEAR_BONUS if all_clear else 0
        self.pending_garbage+=base+chain_bonus+ac_bonus
        if all_clear: self.popup_label="ALL CLEAR!"
        elif spin and self.piece_type=='T':
            self.popup_label=f"T-SPIN {['','SINGLE','DOUBLE','TRIPLE'].get(n,'') if isinstance(['','SINGLE','DOUBLE','TRIPLE'],list) else ''}!"
        elif n==4: self.popup_label="TETRIS!"
        elif self.chain_count>1: self.popup_label=f"{self.chain_count} CHAIN!"
        self.popup_timer=1500
        self._apply_gravity_t()
        new_full=[r for r in range(T_ROWS) if all(self.grid[r][c] is not None for c in range(T_COLS))]
        if new_full:
            self.lines_to_clear=new_full; self.line_flash_timer=T_LINE_FLASH; self.state='line_flash'
            self.last_spin=False
        else:
            self.chain_count=0; self._drop_garbage_lines(); self._spawn()

    def _apply_gravity_t(self):
        for c in range(T_COLS):
            col_cells=[self.grid[r][c] for r in range(T_ROWS) if self.grid[r][c] is not None]
            for r in range(T_ROWS-1,-1,-1):
                self.grid[r][c]=col_cells.pop() if col_cells else None

    def _drop_garbage_lines(self):
        if self.garbage_queue<=0: return
        n=min(self.garbage_queue,4); self.garbage_queue-=n
        for _ in range(n):
            hole=random.randint(0,T_COLS-1)
            line=['garbage']*T_COLS; line[hole]=None
            self.grid.pop(0); self.grid.append(line)
        self._check_loss()

    def commit_incoming(self,force=False):
        if self.incoming_garbage>0:
            self.offset_timer+=1
            if force or self.offset_timer>=3:
                self.garbage_queue+=self.incoming_garbage
                self.incoming_garbage=0; self.offset_timer=0

    def any_moving(self):
        return not self.locking and self.state=='playing'

    def _try_move(self,dx):
        if self._valid(self.piece_type,self.piece_rot,self.piece_col+dx,self.piece_row):
            self.piece_col+=dx
            if self.locking: self.lock_timer=0
            self.last_move='move'

    def _try_rotate(self,direction):
        new_rot=(self.piece_rot+direction)%4
        kicks=SRS_KICKS_I if self.piece_type=='I' else SRS_KICKS
        for dx,dy in kicks.get((self.piece_rot,new_rot),[(0,0)]):
            if self._valid(self.piece_type,new_rot,self.piece_col+dx,self.piece_row+dy):
                self.piece_col+=dx; self.piece_row+=dy; self.piece_rot=new_rot
                self.last_kick=(dx,dy); self.last_move='rotate'
                if self.locking: self.lock_timer=0
                return

    def _hard_drop(self):
        self.piece_row=self._ghost_row(); self._lock_piece()

    def _hold(self):
        if self.hold_used: return
        if self.held_piece is None:
            self.held_piece=self.piece_type; self._spawn()
        else:
            self.held_piece,self.piece_type=self.piece_type,self.held_piece
            self.piece_rot=0; self.piece_col=T_COLS//2-2; self.piece_row=T_SPAWN_ROW
        self.hold_used=True

    def serialize(self):
        grid=[[self.grid[r][c] for c in range(T_COLS)] for r in range(T_ROWS)]
        next_q=[self.piece_queue[self.queue_pos+i] for i in range(min(5,len(self.piece_queue)-self.queue_pos))]
        return {
            "type":"tetris","grid":grid,
            "piece_type":self.piece_type,"piece_rot":self.piece_rot,
            "piece_col":self.piece_col,"piece_row":self.piece_row,
            "ghost_row":self._ghost_row(),"held":self.held_piece,
            "hold_used":self.hold_used,"next":next_q,
            "score":self.score,"lines":self.lines_cleared,"level":self.level,
            "state":self.state,"popup":self.popup_label,"popup_timer":self.popup_timer,
            "pending":self.pending_garbage,"incoming":self.incoming_garbage,
            "garbage_q":self.garbage_queue,"lost":self.lost,
            "lines_to_clear":self.lines_to_clear,
        }

    def update(self,dt,inp):
        if self.lost: return
        if self.popup_timer>0: self.popup_timer=max(0,self.popup_timer-dt)

        if self.state=='line_flash':
            self.line_flash_timer-=dt
            if self.line_flash_timer<=0:
                self.state='playing'; self._clear_lines()
            return

        if self.state!='playing': return

        # Process events (one-shot actions)
        for ev in (inp.get('events') or []):
            if ev.get('rotate_cw'):  self._try_rotate(1)
            if ev.get('rotate_ccw'): self._try_rotate(-1)
            if ev.get('hard_drop'):  self._hard_drop(); return
            if ev.get('hold'):       self._hold()

        keys=inp.get('keys') or {}
        # DAS
        left=keys.get('left',False); right=keys.get('right',False)
        if left and not right:
            if not self._held_left:
                self._try_move(-1); self._held_left=True; self.das_timer=0
            else:
                self.das_timer+=dt
                if self.das_timer>=170:
                    if (self.das_timer-170)%(50)==0: self._try_move(-1)
        else:
            self._held_left=False
        if right and not left:
            if not self._held_right:
                self._try_move(1); self._held_right=True; self.das_timer=0
            else:
                self.das_timer+=dt
                if self.das_timer>=170:
                    if (self.das_timer-170)%(50)==0: self._try_move(1)
        else:
            self._held_right=False

        soft=keys.get('down',False)
        fall_interval=T_FALL_SOFT if soft else max(100,T_FALL_NORMAL-80*(self.level-1))
        self.fall_timer+=dt
        if self.fall_timer>=fall_interval:
            self.fall_timer=0
            if self._valid(self.piece_type,self.piece_rot,self.piece_col,self.piece_row+1):
                self.piece_row+=1; self.locking=False
            else:
                self.locking=True

        if self.locking:
            self.lock_timer+=dt
            if self.lock_timer>=T_LOCK_DELAY: self._lock_piece()

# ─── Garbage exchange ─────────────────────────────────────────────────────────
def _convert_garbage(amount,sender,receiver):
    if isinstance(sender,HBoard) and isinstance(receiver,HTetrisBoard):
        return max(1,amount//6)
    if isinstance(sender,HTetrisBoard) and isinstance(receiver,HBoard):
        return amount*6
    return amount

def exchange_garbage(b1,b2):
    def _ready(b):
        if isinstance(b,HTetrisBoard): return b.state=='playing' and not b.any_moving()
        return not b.any_moving()
    p1=b1.pending_garbage if _ready(b1) else 0
    p2=b2.pending_garbage if _ready(b2) else 0
    if p1>0:
        b1.pending_garbage=0; conv=_convert_garbage(p1,b1,b2)
        absorb=min(conv,b2.incoming_garbage); b2.incoming_garbage-=absorb
        rem=conv-absorb
        if rem>0: b2.incoming_garbage+=rem
    if p2>0:
        b2.pending_garbage=0; conv=_convert_garbage(p2,b2,b1)
        absorb=min(conv,b1.incoming_garbage); b1.incoming_garbage-=absorb
        rem=conv-absorb
        if rem>0: b1.incoming_garbage+=rem

# ─── Message framing ──────────────────────────────────────────────────────────
def send_msg(sock,msg):
    try:
        data=json.dumps(msg).encode()
        sock.sendall(len(data).to_bytes(4,'big')+data)
        return True
    except: return False

def recv_loop(sock,queue,stop_event):
    buf=b""
    try:
        sock.setblocking(True)
        while not stop_event.is_set():
            try:
                chunk=sock.recv(4096)
                if not chunk: break
                buf+=chunk
                while len(buf)>=4:
                    ln=int.from_bytes(buf[:4],'big')
                    if len(buf)<4+ln: break
                    data=buf[4:4+ln]; buf=buf[4+ln:]
                    try: queue.append(json.loads(data.decode()))
                    except: pass
            except: break
    except: pass
    stop_event.set()

# ─── Game session ─────────────────────────────────────────────────────────────
import queue as qmod

def run_session(p1_sock,p2_sock,seed,p1_mode,p2_mode,match_id,match_type):
    print(f"[GS] Session start: {match_id} p1={p1_mode} p2={p2_mode}")

    b1 = HBoard(seed+1000) if p1_mode=='puyo' else HTetrisBoard(seed+1000)
    b2 = HBoard(seed+2000) if p2_mode=='puyo' else HTetrisBoard(seed+2000)

    p1_inputs=[]; p2_inputs=[]
    stop1=threading.Event(); stop2=threading.Event()
    threading.Thread(target=recv_loop,args=(p1_sock,p1_inputs,stop1),daemon=True).start()
    threading.Thread(target=recv_loop,args=(p2_sock,p2_inputs,stop2),daemon=True).start()

    TICK=16  # ~60fps
    last_time=time.time()
    p1_wins=0; p2_wins=0; round_num=1

    while True:
        now=time.time(); dt=int((now-last_time)*1000); last_time=now

        # Drain inputs
        p1_inp={}; p2_inp={}
        while p1_inputs:
            m=p1_inputs.pop(0)
            if m.get('type')=='input': p1_inp=m
            elif m.get('type')=='mode':
                p1_mode=m.get('mode',p1_mode)
        while p2_inputs:
            m=p2_inputs.pop(0)
            if m.get('type')=='input': p2_inp=m
            elif m.get('type')=='mode':
                p2_mode=m.get('mode',p2_mode)

        if stop1.is_set() or stop2.is_set():
            print(f"[GS] Client disconnected in {match_id}")
            break

        b1.update(dt, p1_inp)
        b2.update(dt, p2_inp)
        exchange_garbage(b1,b2)

        state={
            "type":"game_state",
            "b1":b1.serialize(),"b2":b2.serialize(),
            "p1_wins":p1_wins,"p2_wins":p2_wins,"round":round_num,
        }
        ok1=send_msg(p1_sock,state)
        ok2=send_msg(p2_sock,state)
        if not ok1 or not ok2: break

        # Check loss
        if b1.lost or b2.lost:
            if b1.lost: p2_wins+=1; winner="p2"
            else:       p1_wins+=1; winner="p1"
            result={"type":"round_end","winner":winner,"p1_wins":p1_wins,"p2_wins":p2_wins,"round":round_num}
            send_msg(p1_sock,result); send_msg(p2_sock,result)

            if p1_wins>=2 or p2_wins>=2:
                match_winner="p1" if p1_wins>=2 else "p2"
                send_msg(p1_sock,{"type":"match_end","winner":match_winner})
                send_msg(p2_sock,{"type":"match_end","winner":match_winner})
                break

            # New round — wait for both ready
            round_num+=1
            p1_ready=False; p2_ready=False
            deadline=time.time()+30
            while time.time()<deadline and not(p1_ready and p2_ready):
                while p1_inputs:
                    m=p1_inputs.pop(0)
                    if m.get('type')=='ready': p1_ready=True
                while p2_inputs:
                    m=p2_inputs.pop(0)
                    if m.get('type')=='ready': p2_ready=True
                time.sleep(0.05)

            # Reset boards with new seed
            new_seed=seed+round_num*100
            b1=HBoard(new_seed+1000) if p1_mode=='puyo' else HTetrisBoard(new_seed+1000)
            b2=HBoard(new_seed+2000) if p2_mode=='puyo' else HTetrisBoard(new_seed+2000)
            send_msg(p1_sock,{"type":"round_start","round":round_num,"seed":new_seed})
            send_msg(p2_sock,{"type":"round_start","round":round_num,"seed":new_seed})

        elapsed=time.time()-now
        sleep=max(0,TICK/1000-elapsed)
        time.sleep(sleep)

    try: p1_sock.close()
    except: pass
    try: p2_sock.close()
    except: pass
    print(f"[GS] Session end: {match_id}")

# ─── Matchmaking waiting room ──────────────────────────────────────────────────
lock=threading.Lock()
waiting={}  # match_id -> {"sock","role","mode","seed","ts"}

def handle_client(sock,addr):
    try:
        sock.settimeout(30)
        buf=b""
        while len(buf)<4:
            chunk=sock.recv(4)
            if not chunk: return
            buf+=chunk
        ln=int.from_bytes(buf[:4],'big')
        data=b""
        while len(data)<ln:
            chunk=sock.recv(ln-len(data))
            if not chunk: return
            data+=chunk
        msg=json.loads(data.decode())
        match_id=msg.get('match_id')
        role=msg.get('role')
        mode=msg.get('mode','puyo')
        seed=msg.get('seed',12345)
        print(f"[GS] {role} joined match {match_id} mode={mode}")

        with lock:
            if match_id in waiting:
                other=waiting.pop(match_id)
                if role=='p1':
                    p1_sock,p1_mode=sock,mode; p2_sock,p2_mode=other['sock'],other['mode']
                else:
                    p2_sock,p2_mode=sock,mode; p1_sock,p1_mode=other['sock'],other['mode']
                # Both connected — start session
                threading.Thread(target=run_session,
                    args=(p1_sock,p2_sock,seed,p1_mode,p2_mode,match_id,'unranked'),
                    daemon=True).start()
            else:
                waiting[match_id]={'sock':sock,'role':role,'mode':mode,'seed':seed,'ts':time.time()}
                sock.settimeout(None)
    except Exception as e:
        print(f"[GS] Handshake error: {e}")
        try: sock.close()
        except: pass

def cleanup_waiting():
    while True:
        time.sleep(30)
        now=time.time()
        with lock:
            stale=[k for k,v in waiting.items() if now-v['ts']>60]
            for k in stale:
                try: waiting[k]['sock'].close()
                except: pass
                del waiting[k]

if __name__=='__main__':
    threading.Thread(target=cleanup_waiting,daemon=True).start()
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind((HOST,PORT)); srv.listen()
    print(f"[GS] Game server on port {PORT}")
    while True:
        conn,addr=srv.accept()
        threading.Thread(target=handle_client,args=(conn,addr),daemon=True).start()
