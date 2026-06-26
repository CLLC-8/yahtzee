"""
Yahtzee en ligne — serveur Flask + Socket.IO.
Logique de jeu côté serveur (autoritaire) : dés, scores, tours, fin de partie.
Chaque joueur se connecte depuis son téléphone via un code de partie.

Lancer en local :
    python app.py
puis ouvrir http://localhost:5000 (et http://TON-IP-LOCALE:5000 sur les autres tels).
"""

import os
import random
import string
from collections import Counter

from flask import Flask, request, Response
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "yahtzee-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ---------------------------------------------------------------------------
# Logique de score
# ---------------------------------------------------------------------------

UPPER = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6}
UPPER_CATS = ["un", "deux", "trois", "quatre", "cinq", "six"]
LOWER_CATS = ["brelan", "carre", "full", "petite_suite", "grande_suite", "yahtzee", "chance"]
CATEGORIES = UPPER_CATS + LOWER_CATS


def score_for(category, dice):
    """Score d'une combinaison de 5 dés pour une catégorie donnée."""
    c = Counter(dice)
    total = sum(dice)
    counts = c.values()

    if category in UPPER:
        n = UPPER[category]
        return c[n] * n
    if category == "brelan":
        return total if max(counts) >= 3 else 0
    if category == "carre":
        return total if max(counts) >= 4 else 0
    if category == "full":
        # Full classique (3+2). Un yahtzee (5 identiques) compte aussi comme full.
        return 25 if (sorted(counts) == [2, 3] or max(counts) == 5) else 0
    if category == "petite_suite":
        ds = set(dice)
        return 30 if any(s <= ds for s in ({1, 2, 3, 4}, {2, 3, 4, 5}, {3, 4, 5, 6})) else 0
    if category == "grande_suite":
        ds = set(dice)
        return 40 if ds in ({1, 2, 3, 4, 5}, {2, 3, 4, 5, 6}) else 0
    if category == "yahtzee":
        return 50 if max(counts) == 5 else 0
    if category == "chance":
        return total
    return 0


def is_yahtzee(dice):
    return max(Counter(dice).values()) == 5


def player_totals(p):
    s = p["scores"]
    upper = sum(s[c] or 0 for c in UPPER_CATS)
    bonus = 35 if upper >= 63 else 0
    lower = sum(s[c] or 0 for c in LOWER_CATS) + p["yahtzee_bonus"]
    return {
        "upper": upper,
        "bonus": bonus,
        "lower": lower,
        "total": upper + bonus + lower,
        "complete": all(s[c] is not None for c in CATEGORIES),
    }


# ---------------------------------------------------------------------------
# État des parties (en mémoire)
# ---------------------------------------------------------------------------

rooms = {}  # code -> room


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans I,O,0,1 ambigus
    while True:
        code = "".join(random.choice(alphabet) for _ in range(4))
        if code not in rooms:
            return code


def new_player(pid, sid, name):
    return {
        "pid": pid,
        "sid": sid,
        "name": name,
        "connected": True,
        "scores": {c: None for c in CATEGORIES},
        "yahtzee_bonus": 0,
    }


def new_pid():
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


def find_room_by_sid(sid):
    for code, room in rooms.items():
        for p in room["players"]:
            if p["sid"] == sid:
                return code, room, p
    return None, None, None


def current_player(room):
    if not room["players"]:
        return None
    return room["players"][room["current_index"] % len(room["players"])]


def reset_turn(room):
    room["dice"] = [1, 1, 1, 1, 1]
    room["held"] = [False] * 5
    room["rolls_left"] = 3
    room["turn_rolled"] = False


def advance_turn(room):
    """Passe au joueur connecté suivant."""
    n = len(room["players"])
    for step in range(1, n + 1):
        idx = (room["current_index"] + step) % n
        if room["players"][idx]["connected"]:
            room["current_index"] = idx
            reset_turn(room)
            return
    # personne de connecté : on reste, le tour reprendra à la reconnexion
    reset_turn(room)


def compute_potentials(room):
    """Score que donnerait chaque catégorie avec les dés actuels."""
    dice = room["dice"]
    return {c: score_for(c, dice) for c in CATEGORIES}


def serialize(room):
    cur = current_player(room)
    return {
        "code": room["code"],
        "state": room["state"],
        "host_pid": room["host_pid"],
        "players": [
            {
                "pid": p["pid"],
                "name": p["name"],
                "connected": p["connected"],
                "scores": p["scores"],
                "yahtzee_bonus": p["yahtzee_bonus"],
                "totals": player_totals(p),
            }
            for p in room["players"]
        ],
        "current_pid": cur["pid"] if (cur and room["state"] == "playing") else None,
        "dice": room["dice"],
        "held": room["held"],
        "rolls_left": room["rolls_left"],
        "turn_rolled": room["turn_rolled"],
        "potential": compute_potentials(room) if (room["state"] == "playing" and room["turn_rolled"]) else {},
        "winner_pids": room.get("winner_pids", []),
    }


def broadcast(code):
    room = rooms.get(code)
    if room:
        socketio.emit("state", serialize(room), to=code)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="theme-color" content="#0b1a18">
<title>Yahtzee</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg-0:#0b1a18; --bg-1:#0f2421; --panel:#143029; --panel-2:#1a3a32;
    --line:#27473e; --ivory:#f2ebd8; --muted:#8aa39b; --muted-2:#6f8a82;
    --gold:#f0b53d; --gold-deep:#caa033; --mint:#57e0bf; --red:#e7705f;
    --pip:#15110b;
    --r:14px;
    --safe-b:env(safe-area-inset-bottom,0px);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;height:100%}
  body{
    font-family:Inter,system-ui,sans-serif;color:var(--ivory);
    background:
      radial-gradient(120% 80% at 50% -10%, #16352f 0%, var(--bg-1) 45%, var(--bg-0) 100%);
    background-attachment:fixed;
    min-height:100dvh;
    -webkit-font-smoothing:antialiased;
    overscroll-behavior-y:none;
  }
  .wrap{max-width:560px;margin:0 auto;padding:18px 16px calc(22px + var(--safe-b));min-height:100dvh;display:flex;flex-direction:column}
  .hidden{display:none !important}

  /* ---------- titre ---------- */
  .brand{display:flex;align-items:center;justify-content:center;gap:12px;margin:8px 0 22px}
  .brand h1{
    font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:40px;letter-spacing:-1.5px;
    margin:0;line-height:1;color:var(--ivory);
  }
  .brand h1 b{color:var(--gold);font-weight:800}
  .brand .die-logo{width:38px;height:38px;border-radius:10px;background:var(--ivory);
    display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);
    padding:7px;gap:3px;box-shadow:0 6px 18px rgba(0,0,0,.35),inset 0 2px 0 rgba(255,255,255,.7)}
  .brand .die-logo i{background:var(--pip);border-radius:50%}
  .brand .die-logo i:nth-child(1),.brand .die-logo i:nth-child(5),.brand .die-logo i:nth-child(9){visibility:visible}
  .brand .die-logo i:not(:nth-child(1)):not(:nth-child(5)):not(:nth-child(9)){visibility:hidden}

  /* ---------- cartes / lobby ---------- */
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
    padding:18px;box-shadow:0 12px 30px rgba(0,0,0,.25)}
  .card + .card{margin-top:14px}
  .lead{color:var(--muted);font-size:14px;margin:0 0 14px;line-height:1.5}
  label.fld{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 6px}
  input[type=text]{
    width:100%;background:var(--bg-0);border:1px solid var(--line);color:var(--ivory);
    border-radius:11px;padding:13px 14px;font-size:16px;font-family:inherit;outline:none;
  }
  input[type=text]:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(240,181,61,.16)}
  input.code{letter-spacing:.35em;text-transform:uppercase;font-weight:600;text-align:center;
    font-family:'Bricolage Grotesque',sans-serif}
  .row{display:flex;gap:10px}
  .row > *{flex:1}

  button{font-family:inherit;cursor:pointer;border:none;font-size:16px}
  .btn{
    width:100%;padding:15px;border-radius:12px;font-weight:600;letter-spacing:.01em;
    background:var(--panel-2);color:var(--ivory);border:1px solid var(--line);
    transition:transform .08s ease,filter .15s ease;
  }
  .btn:active{transform:scale(.985)}
  .btn.primary{background:var(--gold);color:#2a1d04;border-color:var(--gold-deep);
    box-shadow:0 8px 20px rgba(240,181,61,.22)}
  .btn.primary:disabled{background:var(--panel-2);color:var(--muted-2);box-shadow:none;border-color:var(--line)}
  .btn:disabled{cursor:not-allowed;color:var(--muted-2)}
  .btn.ghost{background:transparent}
  .divider{display:flex;align-items:center;gap:12px;color:var(--muted-2);font-size:12px;
    text-transform:uppercase;letter-spacing:.1em;margin:16px 0}
  .divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--line)}

  /* ---------- liste joueurs (lobby) ---------- */
  .players{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
  .players li{display:flex;align-items:center;gap:10px;background:var(--bg-0);
    border:1px solid var(--line);border-radius:11px;padding:11px 13px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--mint);flex:none}
  .dot.off{background:var(--muted-2)}
  .pname{font-weight:500}
  .tag{margin-left:auto;font-size:11px;color:var(--gold);text-transform:uppercase;letter-spacing:.08em;font-weight:600}
  .tag.you{color:var(--mint)}

  .codechip{display:inline-flex;align-items:center;gap:9px;background:var(--bg-0);
    border:1px dashed var(--gold-deep);border-radius:11px;padding:9px 14px;cursor:pointer}
  .codechip .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
  .codechip .val{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;
    letter-spacing:.18em;color:var(--gold)}

  /* ---------- en-tête de jeu ---------- */
  .ghead{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;gap:10px}
  .turnbox{min-width:0}
  .turnbox .who{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;line-height:1.1;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .turnbox .who.mine{color:var(--gold)}
  .turnbox .sub{font-size:12.5px;color:var(--muted);margin-top:2px}
  .mini-code{font-size:11px;color:var(--muted);text-align:right;cursor:pointer;flex:none}
  .mini-code b{font-family:'Bricolage Grotesque',sans-serif;color:var(--ivory);letter-spacing:.12em;font-size:15px;display:block}

  /* ---------- dés ---------- */
  .dice-area{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
    padding:18px 14px;margin-bottom:14px}
  .dice{display:flex;justify-content:center;gap:9px;flex-wrap:nowrap}
  .die{
    width:54px;height:54px;border-radius:12px;background:var(--ivory);position:relative;
    display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);
    padding:8px;gap:2px;box-shadow:0 6px 14px rgba(0,0,0,.4),inset 0 2px 0 rgba(255,255,255,.65),inset 0 -3px 5px rgba(0,0,0,.12);
    transition:transform .12s ease,box-shadow .15s ease;flex:none;
  }
  .die.idle{opacity:.45}
  .die.holdable{cursor:pointer}
  .die.held{transform:translateY(-7px);
    box-shadow:0 12px 18px rgba(0,0,0,.45),0 0 0 3px var(--mint),inset 0 2px 0 rgba(255,255,255,.65)}
  .die.held::after{content:"gardé";position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);
    font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--mint);font-weight:600}
  .pip{width:100%;height:100%;border-radius:50%;background:var(--pip);place-self:center;
    width:8px;height:8px;visibility:hidden}
  .pip.on{visibility:visible}
  @keyframes tumble{
    0%{transform:translateY(0) rotate(0)}
    25%{transform:translateY(-12px) rotate(-18deg) scale(1.06)}
    55%{transform:translateY(2px) rotate(14deg) scale(.97)}
    80%{transform:translateY(-3px) rotate(-6deg)}
    100%{transform:translateY(0) rotate(0)}
  }
  .die.rolling{animation:tumble .5s cubic-bezier(.3,.8,.3,1)}

  .rolls{display:flex;align-items:center;justify-content:center;gap:7px;margin:18px 0 14px}
  .rolls .pd{width:9px;height:9px;border-radius:50%;background:var(--line)}
  .rolls .pd.on{background:var(--gold)}
  .rolls .txt{font-size:12px;color:var(--muted);margin-left:4px}

  /* ---------- feuille de score ---------- */
  .sheet-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);
    background:var(--panel);-webkit-overflow-scrolling:touch}
  table.sheet{border-collapse:collapse;width:100%;font-size:13.5px}
  table.sheet th,table.sheet td{padding:9px 8px;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap}
  table.sheet thead th{position:sticky;top:0;background:var(--panel-2);font-weight:600;font-size:12.5px;z-index:3}
  .rowlabel{position:sticky;left:0;background:var(--panel);text-align:left !important;z-index:2;
    min-width:118px;font-weight:500}
  thead .rowlabel{background:var(--panel-2);z-index:4}
  .rowlabel .hint{display:block;font-size:10.5px;color:var(--muted-2);font-weight:400;margin-top:1px}
  .pcol{min-width:62px}
  .pcol .nm{display:block;max-width:74px;overflow:hidden;text-overflow:ellipsis;margin:0 auto}
  .pcol.active{color:var(--gold)}
  th.pcol.active::after{content:"";display:block;height:2px;background:var(--gold);border-radius:2px;margin:5px auto 0;width:70%}
  .cell-val{font-variant-numeric:tabular-nums;font-weight:500}
  .cell-empty{color:var(--muted-2)}
  td.pick{cursor:pointer;background:rgba(240,181,61,.06)}
  td.pick .pv{font-variant-numeric:tabular-nums;color:var(--gold);font-weight:600}
  td.pick.zero .pv{color:var(--red)}
  td.pick:active{background:rgba(240,181,61,.16)}
  tr.sep td,tr.sep th{border-top:2px solid var(--line)}
  tr.sub td,tr.sub .rowlabel{background:var(--bg-1);font-size:12.5px;color:var(--muted)}
  tr.sub .cell-val{color:var(--ivory)}
  tr.total td,tr.total .rowlabel{background:var(--panel-2);font-weight:700;font-size:15px}
  tr.total .cell-val{color:var(--gold);font-family:'Bricolage Grotesque',sans-serif}
  .bonus-mini{font-size:10px;color:var(--muted-2);display:block;font-weight:400}

  .hintline{font-size:12.5px;color:var(--muted);text-align:center;margin:12px 2px 0;line-height:1.5}

  /* ---------- fin de partie ---------- */
  .podium{display:flex;flex-direction:column;gap:10px;margin:6px 0 4px}
  .prow{display:flex;align-items:center;gap:12px;background:var(--bg-0);border:1px solid var(--line);
    border-radius:12px;padding:13px 14px}
  .prow.win{border-color:var(--gold);background:linear-gradient(180deg,rgba(240,181,61,.14),rgba(240,181,61,.04))}
  .rank{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:20px;color:var(--muted);width:26px;text-align:center}
  .prow.win .rank{color:var(--gold)}
  .prow .pn{font-weight:600;font-size:16px}
  .prow .crown{color:var(--gold);font-size:13px}
  .prow .pt{margin-left:auto;font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;
    font-variant-numeric:tabular-nums}
  h2.win-title{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:26px;margin:0 0 2px;text-align:center}
  .win-sub{color:var(--muted);text-align:center;font-size:14px;margin:0 0 16px}

  .toast{position:fixed;left:50%;bottom:calc(20px + var(--safe-b));transform:translateX(-50%) translateY(20px);
    background:#222;color:#fff;padding:11px 18px;border-radius:11px;font-size:14px;opacity:0;
    transition:opacity .2s,transform .2s;pointer-events:none;z-index:50;border:1px solid #3a3a3a}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .err{color:var(--red);font-size:13px;margin:8px 0 0;min-height:1px;text-align:center}
  .foot{margin-top:auto;padding-top:18px;text-align:center;color:var(--muted-2);font-size:11.5px}

  @media (prefers-reduced-motion:reduce){.die.rolling{animation:none}}
  @media (max-width:360px){.die{width:48px;height:48px}.brand h1{font-size:34px}}
</style>
</head>
<body>
<div class="wrap">

  <div class="brand">
    <div class="die-logo"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <h1>YAH<b>TZEE</b></h1>
  </div>

  <!-- ====================== LOBBY ====================== -->
  <section id="lobby">
    <div class="card" id="joinCard">
      <p class="lead">Crée une partie ou rejoins celle de tes potes. Chacun joue depuis son téléphone.</p>
      <label class="fld" for="name">Ton prénom</label>
      <input type="text" id="name" placeholder="Charles" maxlength="16" autocomplete="off">

      <div style="margin-top:14px">
        <button class="btn primary" id="btnCreate">Créer une partie</button>
      </div>

      <div class="divider">ou</div>

      <label class="fld" for="code">Code de partie</label>
      <div class="row">
        <input type="text" id="code" class="code" placeholder="A B C D" maxlength="4" autocomplete="off">
        <button class="btn" id="btnJoin" style="flex:0 0 42%">Rejoindre</button>
      </div>
      <p class="err" id="lobbyErr"></p>
    </div>

    <!-- salle d'attente -->
    <div class="card hidden" id="waitCard">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <span class="fld" style="margin:0">Salle d'attente</span>
        <div class="codechip" id="codeChip" title="Copier le lien">
          <span class="lbl">Code</span><span class="val" id="waitCode">----</span>
          <i class="ti"></i>
        </div>
      </div>
      <ul class="players" id="playerList"></ul>
      <div style="margin-top:16px">
        <button class="btn primary" id="btnStart">Lancer la partie</button>
        <p class="hintline" id="waitHint">En attente de l'hôte…</p>
      </div>
    </div>
  </section>

  <!-- ====================== JEU ====================== -->
  <section id="game" class="hidden">
    <div class="ghead">
      <div class="turnbox">
        <div class="who" id="turnWho">—</div>
        <div class="sub" id="turnSub"></div>
      </div>
      <div class="mini-code" id="miniCode" title="Copier le lien">partie<b id="gameCode">----</b></div>
    </div>

    <div class="dice-area">
      <div class="dice" id="dice"></div>
      <div class="rolls" id="rolls"></div>
      <button class="btn primary" id="btnRoll">Lancer les dés</button>
    </div>

    <div class="sheet-wrap">
      <table class="sheet" id="sheet"></table>
    </div>
    <p class="hintline" id="gameHint"></p>
  </section>

  <!-- ====================== FIN ====================== -->
  <section id="finished" class="hidden">
    <div class="card">
      <h2 class="win-title" id="winTitle">Partie terminée</h2>
      <p class="win-sub" id="winSub"></p>
      <div class="podium" id="podium"></div>
      <div style="margin-top:16px">
        <button class="btn primary" id="btnAgain">Rejouer</button>
        <p class="hintline" id="againHint"></p>
      </div>
    </div>
  </section>

  <div class="foot">Yahtzee · 3 lancers par tour · bonus +35 dès 63 en haut</div>
</div>

<div class="toast" id="toast"></div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const socket = io({transports:["websocket","polling"]});
let myPid=null, myCode=null, S=null;
let prevDice=[1,1,1,1,1], prevRolled=false;

const CATS=[
  {k:'un',label:'As',hint:'somme des 1'},
  {k:'deux',label:'Deux',hint:'somme des 2'},
  {k:'trois',label:'Trois',hint:'somme des 3'},
  {k:'quatre',label:'Quatre',hint:'somme des 4'},
  {k:'cinq',label:'Cinq',hint:'somme des 5'},
  {k:'six',label:'Six',hint:'somme des 6'},
  {k:'brelan',label:'Brelan',hint:'3 identiques · somme'},
  {k:'carre',label:'Carré',hint:'4 identiques · somme'},
  {k:'full',label:'Full',hint:'25 pts'},
  {k:'petite_suite',label:'Petite suite',hint:'4 à la suite · 30'},
  {k:'grande_suite',label:'Grande suite',hint:'5 à la suite · 40'},
  {k:'yahtzee',label:'Yahtzee',hint:'5 identiques · 50'},
  {k:'chance',label:'Chance',hint:'somme des dés'},
];
const UPPER=['un','deux','trois','quatre','cinq','six'];
const PIP_MAP={1:[4],2:[0,8],3:[0,4,8],4:[0,2,6,8],5:[0,2,4,6,8],6:[0,2,3,5,6,8]};

const $=id=>document.getElementById(id);
function show(sec){['lobby','game','finished'].forEach(s=>$(s).classList.toggle('hidden',s!==sec));}
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1600);}
function me(){return S?S.players.find(p=>p.pid===myPid):null;}
function isMyTurn(){return S&&S.state==='playing'&&S.current_pid===myPid;}
function shareLink(){return location.origin+location.pathname+'?room='+myCode;}
function copyLink(){
  const url=shareLink();
  if(navigator.clipboard){navigator.clipboard.writeText(url).then(()=>toast('Lien copié !'),()=>toast(url));}
  else{toast(url);}
}

/* ---------- socket ---------- */
socket.on('joined',d=>{myPid=d.pid;myCode=d.code;
  history.replaceState(null,'','?room='+myCode);});
socket.on('error_msg',d=>{$('lobbyErr').textContent=d.message;});
socket.on('state',s=>{S=s;render();});

/* ---------- rendu ---------- */
function render(){
  if(!S){return;}
  if(S.state==='lobby'){show('lobby');renderLobby();}
  else if(S.state==='playing'){show('game');renderGame();}
  else if(S.state==='finished'){show('finished');renderFinished();}
}

function renderLobby(){
  $('joinCard').classList.add('hidden');
  $('waitCard').classList.remove('hidden');
  $('waitCode').textContent=S.code;
  const ul=$('playerList');ul.innerHTML='';
  S.players.forEach(p=>{
    const li=document.createElement('li');
    const tags=[];
    if(p.pid===S.host_pid)tags.push('<span class="tag">hôte</span>');
    if(p.pid===myPid)tags.push('<span class="tag you">toi</span>');
    li.innerHTML=`<span class="dot${p.connected?'':' off'}"></span><span class="pname"></span>${tags.join('')}`;
    li.querySelector('.pname').textContent=p.name;
    ul.appendChild(li);
  });
  const host=myPid===S.host_pid;
  $('btnStart').classList.toggle('hidden',!host);
  $('btnStart').disabled=S.players.length<1;
  $('waitHint').textContent=host
    ? (S.players.length<2?'Tu peux jouer en solo, ou partage le code et attends du monde.':'Tout le monde est là ? Lance la partie.')
    : "En attente que l'hôte lance la partie…";
}

function dieEl(v,idx){
  const d=document.createElement('div');
  d.className='die';d.dataset.index=idx;
  for(let i=0;i<9;i++){const p=document.createElement('span');p.className='pip'+(PIP_MAP[v].includes(i)?' on':'');d.appendChild(p);}
  return d;
}

function renderGame(){
  const cur=S.players.find(p=>p.pid===S.current_pid);
  const mine=isMyTurn();
  $('turnWho').textContent=mine?'À toi de jouer':('Tour de '+(cur?cur.name:'…'));
  $('turnWho').classList.toggle('mine',mine);
  $('turnSub').textContent=mine
    ? (S.rolls_left===3?'Lance les dés pour commencer':'Garde des dés, relance, puis choisis une case')
    : (cur&&!cur.connected?'(déconnecté)':'Regarde et prépare ta stratégie');
  $('gameCode').textContent=S.code;

  /* dés */
  const box=$('dice');box.innerHTML='';
  S.dice.forEach((v,i)=>{
    const d=dieEl(v,i);
    if(S.held[i])d.classList.add('held');
    if(!S.turn_rolled)d.classList.add('idle');
    if(mine&&S.turn_rolled&&S.rolls_left>0){d.classList.add('holdable');d.onclick=()=>socket.emit('toggle_hold',{index:i});}
    if(S.turn_rolled&&prevRolled&&v!==prevDice[i]&&!S.held[i]){
      d.classList.add('rolling');d.style.animationDelay=(i*40)+'ms';
    }
    box.appendChild(d);
  });
  prevDice=S.dice.slice();prevRolled=S.turn_rolled;

  /* lancers restants */
  const r=$('rolls');r.innerHTML='';
  for(let i=0;i<3;i++){const p=document.createElement('span');p.className='pd'+(i<S.rolls_left?' on':'');r.appendChild(p);}
  const txt=document.createElement('span');txt.className='txt';
  txt.textContent=S.rolls_left+' lancer'+(S.rolls_left>1?'s':'')+' restant'+(S.rolls_left>1?'s':'');
  r.appendChild(txt);

  /* bouton lancer */
  const roll=$('btnRoll');
  roll.disabled=!mine||S.rolls_left<=0;
  roll.textContent=!mine?'En attente…':(S.rolls_left===3?'Lancer les dés':(S.rolls_left>0?'Relancer ('+S.rolls_left+')':'Plus de lancers'));

  renderSheet(mine);

  $('gameHint').textContent=mine
    ? (S.turn_rolled?'Touche une case de ta colonne pour valider ton score.':'')
    : '';
}

function renderSheet(mine){
  const t=$('sheet');t.innerHTML='';
  const canPick=mine&&S.turn_rolled;
  /* en-tête */
  const thead=document.createElement('thead');
  let tr=document.createElement('tr');
  tr.innerHTML='<th class="rowlabel">Catégorie</th>';
  S.players.forEach(p=>{
    const th=document.createElement('th');
    th.className='pcol'+(p.pid===S.current_pid?' active':'');
    const nm=document.createElement('span');nm.className='nm';nm.textContent=p.name+(p.pid===myPid?' (toi)':'');
    th.appendChild(nm);tr.appendChild(th);
  });
  thead.appendChild(tr);t.appendChild(thead);

  const tb=document.createElement('tbody');
  const addRow=(cat,opts={})=>{
    const row=document.createElement('tr');
    if(opts.cls)row.className=opts.cls;
    const lab=document.createElement('td');lab.className='rowlabel';
    lab.innerHTML=`${cat.label}${cat.hint?`<span class="hint">${cat.hint}</span>`:''}`;
    row.appendChild(lab);
    S.players.forEach(p=>{
      const td=document.createElement('td');
      if(p.pid===S.current_pid)td.classList.add('active');
      const val=p.scores[cat.k];
      if(val!==null&&val!==undefined){
        td.innerHTML=`<span class="cell-val">${val}</span>`;
      }else if(canPick&&p.pid===myPid){
        const pot=S.potential[cat.k]??0;
        td.className+=' pick'+(pot===0?' zero':'');
        td.innerHTML=`<span class="pv">${pot}</span>`;
        td.onclick=()=>pickCategory(cat.k,pot);
      }else{
        td.innerHTML='<span class="cell-empty">·</span>';
      }
      row.appendChild(td);
    });
    tb.appendChild(row);
  };

  CATS.slice(0,6).forEach(c=>addRow(c));
  /* sous-totaux haut */
  addRowTotals(tb,'Total (haut)','upper','sub');
  addRowTotals(tb,'Bonus','bonus','sub',true);
  CATS.slice(6).forEach((c,i)=>addRow(c,{cls:i===0?'sep':''}));
  addRowTotals(tb,'TOTAL','total','total sep');
  t.appendChild(tb);
}

function addRowTotals(tb,label,key,cls,isBonus){
  const row=document.createElement('tr');row.className=cls;
  const lab=document.createElement('td');lab.className='rowlabel';
  lab.innerHTML=isBonus?`${label}<span class="bonus-mini">+35 dès 63</span>`:label;
  row.appendChild(lab);
  S.players.forEach(p=>{
    const td=document.createElement('td');
    if(p.pid===S.current_pid)td.classList.add('active');
    let v=p.totals[key];
    if(isBonus)v=(p.totals.bonus>0?'+35':(p.totals.upper>=63?'+35':p.totals.upper+'/63'));
    td.innerHTML=`<span class="cell-val">${v}</span>`;
    row.appendChild(td);
  });
  tb.appendChild(row);
}

function pickCategory(cat,pot){
  if(pot===0){
    if(!confirm('Valider 0 dans « '+catLabel(cat)+' » ? (case sacrifiée)'))return;
  }
  socket.emit('score',{category:cat});
}
function catLabel(k){const c=CATS.find(c=>c.k===k);return c?c.label:k;}

function renderFinished(){
  const ranked=[...S.players].sort((a,b)=>b.totals.total-a.totals.total);
  const winners=S.winner_pids||[];
  const iWon=winners.includes(myPid);
  const winNames=S.players.filter(p=>winners.includes(p.pid)).map(p=>p.name);
  $('winTitle').textContent=iWon?'Tu as gagné !':(winNames.length>1?'Égalité !':(winNames[0]+' gagne'));
  $('winSub').textContent=winners.length>1?('À égalité : '+winNames.join(', ')):'';
  const pod=$('podium');pod.innerHTML='';
  ranked.forEach((p,i)=>{
    const win=winners.includes(p.pid);
    const row=document.createElement('div');row.className='prow'+(win?' win':'');
    row.innerHTML=`<span class="rank">${i+1}</span>
      <span class="pn"></span>${win?'<span class="crown ti"></span>':''}
      <span class="pt">${p.totals.total}</span>`;
    row.querySelector('.pn').textContent=p.name+(p.pid===myPid?' (toi)':'');
    pod.appendChild(row);
  });
  const host=myPid===S.host_pid;
  $('btnAgain').classList.toggle('hidden',!host);
  $('againHint').textContent=host?'Relance une partie avec les mêmes joueurs.':"En attente de l'hôte pour rejouer…";
}

/* ---------- actions lobby ---------- */
$('btnCreate').onclick=()=>{
  const name=$('name').value.trim();
  if(!name){$('lobbyErr').textContent='Mets ton prénom d\'abord.';return;}
  $('lobbyErr').textContent='';
  socket.emit('create_room',{name});
};
$('btnJoin').onclick=()=>{
  const name=$('name').value.trim();
  const code=$('code').value.trim().toUpperCase();
  if(!name){$('lobbyErr').textContent='Mets ton prénom d\'abord.';return;}
  if(code.length!==4){$('lobbyErr').textContent='Le code fait 4 lettres.';return;}
  $('lobbyErr').textContent='';
  socket.emit('join_room',{name,code});
};
$('code').addEventListener('input',e=>{e.target.value=e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,'');});
$('name').addEventListener('keydown',e=>{if(e.key==='Enter')$('btnCreate').click();});
$('code').addEventListener('keydown',e=>{if(e.key==='Enter')$('btnJoin').click();});

$('btnStart').onclick=()=>socket.emit('start_game');
$('btnRoll').onclick=()=>{if(isMyTurn()&&S.rolls_left>0)socket.emit('roll');};
$('btnAgain').onclick=()=>socket.emit('play_again');
$('codeChip').onclick=copyLink;
$('miniCode').onclick=copyLink;

/* préremplir depuis ?room= */
(function(){
  const params=new URLSearchParams(location.search);
  const r=params.get('room');
  if(r){$('code').value=r.toUpperCase().slice(0,4);}
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Événements Socket.IO
# ---------------------------------------------------------------------------

@socketio.on("create_room")
def on_create(data):
    name = (data.get("name") or "Joueur").strip()[:16] or "Joueur"
    code = new_code()
    pid = new_pid()
    room = {
        "code": code,
        "state": "lobby",
        "players": [new_player(pid, request.sid, name)],
        "host_pid": pid,
        "current_index": 0,
        "dice": [1, 1, 1, 1, 1],
        "held": [False] * 5,
        "rolls_left": 3,
        "turn_rolled": False,
        "winner_pids": [],
    }
    rooms[code] = room
    join_room(code)
    emit("joined", {"pid": pid, "code": code})
    broadcast(code)


@socketio.on("join_room")
def on_join(data):
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "Joueur").strip()[:16] or "Joueur"
    room = rooms.get(code)
    if not room:
        emit("error_msg", {"message": "Partie introuvable."})
        return
    if room["state"] != "lobby":
        emit("error_msg", {"message": "La partie a déjà commencé."})
        return
    if len(room["players"]) >= 8:
        emit("error_msg", {"message": "Partie pleine (8 max)."})
        return
    pid = new_pid()
    room["players"].append(new_player(pid, request.sid, name))
    join_room(code)
    emit("joined", {"pid": pid, "code": code})
    broadcast(code)


@socketio.on("start_game")
def on_start():
    code, room, player = find_room_by_sid(request.sid)
    if not room or player["pid"] != room["host_pid"]:
        return
    if room["state"] != "lobby" or len(room["players"]) < 1:
        return
    room["state"] = "playing"
    room["current_index"] = 0
    room["winner_pids"] = []
    for p in room["players"]:
        p["scores"] = {c: None for c in CATEGORIES}
        p["yahtzee_bonus"] = 0
    reset_turn(room)
    broadcast(code)


@socketio.on("roll")
def on_roll():
    code, room, player = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    if current_player(room)["pid"] != player["pid"]:
        return
    if room["rolls_left"] <= 0:
        return
    for i in range(5):
        if not room["held"][i] or not room["turn_rolled"]:
            room["dice"][i] = random.randint(1, 6)
    room["rolls_left"] -= 1
    room["turn_rolled"] = True
    broadcast(code)


@socketio.on("toggle_hold")
def on_hold(data):
    code, room, player = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    if current_player(room)["pid"] != player["pid"]:
        return
    if not room["turn_rolled"] or room["rolls_left"] <= 0:
        return
    i = data.get("index")
    if isinstance(i, int) and 0 <= i < 5:
        room["held"][i] = not room["held"][i]
        broadcast(code)


@socketio.on("score")
def on_score(data):
    code, room, player = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    if current_player(room)["pid"] != player["pid"]:
        return
    if not room["turn_rolled"]:
        return
    cat = data.get("category")
    if cat not in CATEGORIES or player["scores"][cat] is not None:
        return

    dice = room["dice"]
    # Bonus yahtzee : yahtzee déjà rempli à 50 + nouveau yahtzee => +100
    if is_yahtzee(dice) and player["scores"]["yahtzee"] == 50:
        player["yahtzee_bonus"] += 100

    player["scores"][cat] = score_for(cat, dice)

    # Fin de partie ?
    if all(player_totals(p)["complete"] for p in room["players"]):
        room["state"] = "finished"
        best = max(player_totals(p)["total"] for p in room["players"])
        room["winner_pids"] = [p["pid"] for p in room["players"] if player_totals(p)["total"] == best]
    else:
        advance_turn(room)

    broadcast(code)


@socketio.on("play_again")
def on_again():
    code, room, player = find_room_by_sid(request.sid)
    if not room or player["pid"] != room["host_pid"]:
        return
    room["state"] = "lobby"
    room["winner_pids"] = []
    reset_turn(room)
    room["current_index"] = 0
    broadcast(code)


@socketio.on("disconnect")
def on_disconnect():
    code, room, player = find_room_by_sid(request.sid)
    if not room:
        return
    if room["state"] == "lobby":
        room["players"] = [p for p in room["players"] if p["sid"] != request.sid]
        if not room["players"]:
            rooms.pop(code, None)
            return
        if player["pid"] == room["host_pid"]:
            room["host_pid"] = room["players"][0]["pid"]
    else:
        player["connected"] = False
        # si c'était son tour, on passe au suivant
        if current_player(room) and current_player(room)["pid"] == player["pid"]:
            advance_turn(room)
        if all(not p["connected"] for p in room["players"]):
            rooms.pop(code, None)
            return
    broadcast(code)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)