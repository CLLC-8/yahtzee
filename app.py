"""
Yahtzee — feuille de score pour la table (Flask + Socket.IO, fichier unique).

Concept : une personne (le créateur) tient la feuille de score sur son téléphone.
Les autres ouvrent le même lien et regardent en LECTURE SEULE (pas de code).
Saisie manuelle des scores (dés réels) ; dés virtuels en option.

Lancer :  python app.py   ->  http://localhost:5000
Prod   :  gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:$PORT app:app
"""

import os
import random
import string
from collections import Counter

from flask import Flask, request, Response
from flask_socketio import SocketIO, join_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "yahtzee-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ---------------------------------------------------------------------------
# Catégories / scores
# ---------------------------------------------------------------------------

UPPER = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6}
UPPER_CATS = ["un", "deux", "trois", "quatre", "cinq", "six"]
LOWER_CATS = ["brelan", "carre", "full", "petite_suite", "grande_suite", "yahtzee", "chance"]
CATEGORIES = UPPER_CATS + LOWER_CATS
FIXED = {"full": 25, "petite_suite": 30, "grande_suite": 40, "yahtzee": 50}


def score_for(category, dice):
    """Score théorique d'une combinaison (utilisé pour la suggestion en mode dés)."""
    c = Counter(dice)
    total = sum(dice)
    counts = c.values()
    if category in UPPER:
        return c[UPPER[category]] * UPPER[category]
    if category == "brelan":
        return total if max(counts) >= 3 else 0
    if category == "carre":
        return total if max(counts) >= 4 else 0
    if category == "full":
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


def totals(player):
    s = player["scores"]
    upper = sum(s[c] or 0 for c in UPPER_CATS)
    bonus = 35 if upper >= 63 else 0
    lower = sum(s[c] or 0 for c in LOWER_CATS)
    return {"upper": upper, "bonus": bonus, "lower": lower,
            "total": upper + bonus + lower,
            "complete": all(s[c] is not None for c in CATEGORIES)}


# ---------------------------------------------------------------------------
# Parties (en mémoire)
# ---------------------------------------------------------------------------

games = {}


def new_id():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        gid = "".join(random.choice(alphabet) for _ in range(5))
        if gid not in games:
            return gid


def new_token():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(24))


def make_player(name):
    return {"name": name, "scores": {c: None for c in CATEGORIES}}


def make_game(gid, token, names, dice_enabled):
    return {
        "id": gid, "token": token,
        "players": [make_player(n) for n in names],
        "dice_enabled": bool(dice_enabled),
        "dice": [1, 1, 1, 1, 1], "held": [False] * 5,
        "rolls_left": 3, "turn_rolled": False,
        "current": 0,
    }


def serialize(g):
    ps = [{"name": p["name"], "scores": p["scores"], "totals": totals(p)} for p in g["players"]]
    complete = bool(ps) and all(p["totals"]["complete"] for p in ps)
    leader = -1
    best = -1
    for i, p in enumerate(ps):
        if p["totals"]["total"] > best:
            best = p["totals"]["total"]
            leader = i
    if best <= 0:
        leader = -1
    return {
        "id": g["id"], "players": ps,
        "dice_enabled": g["dice_enabled"],
        "dice": g["dice"], "held": g["held"],
        "rolls_left": g["rolls_left"], "turn_rolled": g["turn_rolled"],
        "complete": complete, "leader": leader, "current": g["current"],
    }


def broadcast(gid):
    g = games.get(gid)
    if g:
        socketio.emit("state", serialize(g), to=gid)


def auth(data):
    """Retourne (game, True) si le token correspond au créateur, sinon (game, False)."""
    g = games.get((data or {}).get("id"))
    if not g:
        return None, False
    return g, ((data or {}).get("token") == g["token"])


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Événements
# ---------------------------------------------------------------------------

@socketio.on("create_game")
def on_create(data):
    names = data.get("names") or ["Joueur 1"]
    names = [(n or f"Joueur {i+1}").strip()[:18] or f"Joueur {i+1}" for i, n in enumerate(names)][:10]
    gid, token = new_id(), new_token()
    games[gid] = make_game(gid, token, names, data.get("dice_enabled"))
    join_room(gid)
    emit("created", {"id": gid, "token": token})
    emit("role", {"editor": True})
    broadcast(gid)


@socketio.on("resume")
def on_resume(data):
    gid = (data or {}).get("id")
    token = (data or {}).get("token")
    g = games.get(gid)
    if not g:
        # le serveur a peut-être redémarré : on recrée depuis l'instantané du créateur
        snap = (data or {}).get("snapshot") or {}
        names = [p.get("name", f"Joueur {i+1}") for i, p in enumerate(snap.get("players", []))]
        if not names or not token or not gid:
            emit("no_game", {})
            return
        g = make_game(gid, token, names, snap.get("dice_enabled"))
        for i, p in enumerate(snap.get("players", [])):
            sc = p.get("scores", {})
            for c in CATEGORIES:
                v = sc.get(c)
                g["players"][i]["scores"][c] = v if isinstance(v, int) else None
        cu = snap.get("current", 0)
        g["current"] = cu if isinstance(cu, int) and 0 <= cu < len(g["players"]) else 0
        games[gid] = g
        join_room(gid)
        emit("role", {"editor": True})
        broadcast(gid)
        return
    join_room(gid)
    emit("role", {"editor": token == g["token"]})
    emit("state", serialize(g))


@socketio.on("join_game")
def on_join(data):
    gid = (data or {}).get("id")
    g = games.get(gid)
    if not g:
        emit("no_game", {})
        return
    join_room(gid)
    emit("role", {"editor": False})
    emit("state", serialize(g))


@socketio.on("set_score")
def on_set_score(data):
    g, ok = auth(data)
    if not ok:
        return
    i = data.get("player")
    cat = data.get("category")
    val = data.get("value")
    if not isinstance(i, int) or i < 0 or i >= len(g["players"]) or cat not in CATEGORIES:
        return
    if val is None:
        g["players"][i]["scores"][cat] = None
    elif isinstance(val, (int, float)):
        was = g["players"][i]["scores"][cat]
        g["players"][i]["scores"][cat] = max(0, min(999, int(val)))
        # quand le joueur en cours remplit une case vide -> au suivant
        if was is None and i == g["current"]:
            g["current"] = (g["current"] + 1) % len(g["players"])
    broadcast(g["id"])


@socketio.on("set_name")
def on_set_name(data):
    g, ok = auth(data)
    if not ok:
        return
    i = data.get("player")
    name = (data.get("name") or "").strip()[:18]
    if isinstance(i, int) and 0 <= i < len(g["players"]) and name:
        g["players"][i]["name"] = name
        broadcast(g["id"])


@socketio.on("set_current")
def on_set_current(data):
    g, ok = auth(data)
    if not ok:
        return
    i = data.get("player")
    if isinstance(i, int) and 0 <= i < len(g["players"]):
        g["current"] = i
        broadcast(g["id"])


@socketio.on("add_player")
def on_add_player(data):
    g, ok = auth(data)
    if not ok or len(g["players"]) >= 10:
        return
    g["players"].append(make_player(f"Joueur {len(g['players'])+1}"))
    broadcast(g["id"])


@socketio.on("remove_player")
def on_remove_player(data):
    g, ok = auth(data)
    if not ok:
        return
    i = data.get("player")
    if isinstance(i, int) and 0 <= i < len(g["players"]) and len(g["players"]) > 1:
        g["players"].pop(i)
        if i < g["current"]:
            g["current"] -= 1
        if g["current"] >= len(g["players"]):
            g["current"] = 0
        broadcast(g["id"])


@socketio.on("toggle_dice")
def on_toggle_dice(data):
    g, ok = auth(data)
    if not ok:
        return
    g["dice_enabled"] = bool(data.get("enabled"))
    broadcast(g["id"])


@socketio.on("roll")
def on_roll(data):
    g, ok = auth(data)
    if not ok or not g["dice_enabled"]:
        return
    if g["rolls_left"] <= 0:  # nouveau tour
        g["held"] = [False] * 5
        g["rolls_left"] = 3
        g["turn_rolled"] = False
    for i in range(5):
        if not g["held"][i] or not g["turn_rolled"]:
            g["dice"][i] = random.randint(1, 6)
    g["rolls_left"] -= 1
    g["turn_rolled"] = True
    broadcast(g["id"])


@socketio.on("toggle_hold")
def on_hold(data):
    g, ok = auth(data)
    if not ok or not g["turn_rolled"] or g["rolls_left"] <= 0:
        return
    i = data.get("index")
    if isinstance(i, int) and 0 <= i < 5:
        g["held"][i] = not g["held"][i]
        broadcast(g["id"])


@socketio.on("reset_dice")
def on_reset_dice(data):
    g, ok = auth(data)
    if not ok:
        return
    g["dice"] = [1, 1, 1, 1, 1]
    g["held"] = [False] * 5
    g["rolls_left"] = 3
    g["turn_rolled"] = False
    broadcast(g["id"])


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="theme-color" content="#0b1a18">
<title>Yahtzee — feuille de score</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg-0:#0b1a18; --bg-1:#0f2421; --panel:#143029; --panel-2:#1a3a32;
    --line:#27473e; --ivory:#f2ebd8; --muted:#8aa39b; --muted-2:#6f8a82;
    --gold:#f0b53d; --gold-deep:#caa033; --mint:#57e0bf; --red:#e7705f; --pip:#15110b;
    --r:14px; --safe-b:env(safe-area-inset-bottom,0px);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;height:100%}
  body{font-family:Inter,system-ui,sans-serif;color:var(--ivory);
    background:radial-gradient(120% 80% at 50% -10%, #16352f 0%, var(--bg-1) 45%, var(--bg-0) 100%);
    background-attachment:fixed;min-height:100dvh;-webkit-font-smoothing:antialiased;overscroll-behavior-y:none}
  .wrap{max-width:760px;margin:0 auto;padding:14px 12px calc(20px + var(--safe-b));min-height:100dvh;display:flex;flex-direction:column}
  .hidden{display:none !important}
  button{font-family:inherit;cursor:pointer;border:none;font-size:16px}

  /* titre */
  .brand{display:flex;align-items:center;justify-content:center;gap:11px;margin:6px 0 16px}
  .brand h1{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:30px;letter-spacing:-1px;margin:0;line-height:1}
  .brand h1 b{color:var(--gold)}
  .die-logo{width:32px;height:32px;border-radius:9px;background:var(--ivory);display:grid;
    grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);padding:6px;gap:2px;
    box-shadow:0 5px 14px rgba(0,0,0,.35),inset 0 2px 0 rgba(255,255,255,.7)}
  .die-logo i{background:var(--pip);border-radius:50%;visibility:hidden}
  .die-logo i:nth-child(1),.die-logo i:nth-child(5),.die-logo i:nth-child(9){visibility:visible}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:0 12px 30px rgba(0,0,0,.25)}
  .lead{color:var(--muted);font-size:14px;margin:0 0 16px;line-height:1.5}
  .fld{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 8px}

  .stepper{display:flex;align-items:center;gap:14px;justify-content:center;margin:2px 0 18px}
  .stepper button{width:52px;height:52px;border-radius:14px;background:var(--panel-2);border:1px solid var(--line);
    color:var(--ivory);font-size:26px;font-weight:600;line-height:1}
  .stepper button:active{transform:scale(.95)}
  .stepper .n{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:38px;min-width:54px;text-align:center}

  .names{display:flex;flex-direction:column;gap:9px;margin-bottom:6px}
  .names input{width:100%;background:var(--bg-0);border:1px solid var(--line);color:var(--ivory);
    border-radius:11px;padding:12px 14px;font-size:16px;font-family:inherit;outline:none}
  .names input:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(240,181,61,.16)}
  .names .num{display:flex;align-items:center;gap:10px}
  .names .num span{width:26px;text-align:center;color:var(--muted);font-weight:600;font-size:14px;flex:none}

  .toggle-row{display:flex;align-items:center;justify-content:space-between;background:var(--bg-0);
    border:1px solid var(--line);border-radius:12px;padding:13px 14px;margin:16px 0}
  .toggle-row .t{font-weight:500}.toggle-row .t small{display:block;color:var(--muted);font-size:12.5px;font-weight:400;margin-top:2px}
  .sw{width:52px;height:30px;border-radius:30px;background:var(--line);position:relative;transition:background .15s;flex:none}
  .sw.on{background:var(--gold)}
  .sw::after{content:"";position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:var(--ivory);transition:left .15s}
  .sw.on::after{left:25px}

  .btn{width:100%;padding:15px;border-radius:12px;font-weight:600;background:var(--panel-2);color:var(--ivory);border:1px solid var(--line);transition:transform .08s}
  .btn:active{transform:scale(.985)}
  .btn.primary{background:var(--gold);color:#2a1d04;border-color:var(--gold-deep);box-shadow:0 8px 20px rgba(240,181,61,.22)}

  /* barre du tableau */
  .topbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}
  .topbar .tt{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;line-height:1}
  .topbar .tt b{color:var(--gold)}
  .role{font-size:11px;padding:3px 9px;border-radius:20px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
  .role.edit{background:rgba(87,224,191,.16);color:var(--mint)}
  .role.view{background:rgba(240,181,61,.15);color:var(--gold)}
  .iconbtn{margin-left:auto;width:42px;height:42px;border-radius:11px;background:var(--panel);border:1px solid var(--line);
    color:var(--ivory);display:flex;align-items:center;justify-content:center;flex:none}
  .iconbtn + .iconbtn{margin-left:8px}
  .iconbtn svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

  .viewbanner{background:rgba(240,181,61,.1);border:1px solid var(--gold-deep);color:var(--gold);
    border-radius:11px;padding:9px 13px;font-size:13px;margin-bottom:12px;text-align:center}

  /* panneau dés */
  .dicepanel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px;margin-bottom:12px}
  .dice{display:flex;justify-content:center;gap:8px}
  .die{width:48px;height:48px;border-radius:11px;background:var(--ivory);position:relative;display:grid;
    grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);padding:7px;gap:2px;flex:none;
    box-shadow:0 5px 12px rgba(0,0,0,.4),inset 0 2px 0 rgba(255,255,255,.65);transition:transform .12s,box-shadow .15s}
  .die.idle{opacity:.45}
  .die.held{transform:translateY(-6px);box-shadow:0 10px 16px rgba(0,0,0,.45),0 0 0 3px var(--mint),inset 0 2px 0 rgba(255,255,255,.65)}
  .pip{width:7px;height:7px;border-radius:50%;background:var(--pip);place-self:center;visibility:hidden}
  .pip.on{visibility:visible}
  @keyframes tumble{0%{transform:translateY(0) rotate(0)}30%{transform:translateY(-11px) rotate(-16deg) scale(1.05)}60%{transform:translateY(2px) rotate(12deg)}100%{transform:translateY(0) rotate(0)}}
  .die.rolling{animation:tumble .5s cubic-bezier(.3,.8,.3,1)}
  .dicebtns{display:flex;gap:9px;margin-top:13px}
  .dicebtns .btn{margin:0}
  .rollsdots{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:11px}
  .rollsdots .pd{width:8px;height:8px;border-radius:50%;background:var(--line)}
  .rollsdots .pd.on{background:var(--gold)}
  .rollsdots .txt{font-size:12px;color:var(--muted);margin-left:5px}

  /* feuille */
  .sheet-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);background:var(--panel);-webkit-overflow-scrolling:touch}
  table.sheet{border-collapse:collapse;width:100%;font-size:14px}
  table.sheet th,table.sheet td{padding:0;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap}
  table.sheet thead th{position:sticky;top:0;background:var(--panel-2);z-index:3}
  .rowlabel{position:sticky;left:0;background:var(--panel);text-align:left !important;z-index:2;min-width:120px;
    padding:10px 9px !important;font-weight:500;font-size:13.5px}
  thead .rowlabel{background:var(--panel-2);z-index:4}
  .rowlabel .hint{display:block;font-size:10.5px;color:var(--muted-2);font-weight:400;margin-top:1px}
  .phead{padding:9px 6px !important;min-width:66px}
  .phead .nm{display:block;max-width:80px;overflow:hidden;text-overflow:ellipsis;margin:0 auto;font-weight:600;font-size:13px}
  .phead.lead .nm{color:var(--gold)}
  .phead .crown{display:block;font-size:11px;color:var(--gold);height:13px;line-height:1}
  .cell{height:46px;font-variant-numeric:tabular-nums;font-size:16px}
  .cell.editable{cursor:pointer}
  .cell.editable:active{background:rgba(240,181,61,.12)}
  .cell .v{font-weight:600}
  .cell .empty{color:var(--muted-2);font-size:18px}
  .cell.zero .v{color:var(--muted)}
  tr.sep td,tr.sep th{border-top:2px solid var(--line)}
  tr.sub td,tr.sub .rowlabel{background:var(--bg-1);color:var(--muted);font-size:12.5px}
  tr.sub .cell{color:var(--ivory);font-weight:500;height:36px;font-size:14px}
  tr.total td,tr.total .rowlabel{background:var(--panel-2);font-weight:700}
  tr.total .cell{color:var(--gold);font-family:'Bricolage Grotesque',sans-serif;font-size:19px;font-weight:800}
  tr.total .rowlabel{font-size:15px}
  .bonus-mini{font-size:10px;color:var(--muted-2);font-weight:400}

  .donebar{margin-top:12px;text-align:center;background:linear-gradient(180deg,rgba(240,181,61,.16),rgba(240,181,61,.04));
    border:1px solid var(--gold);border-radius:12px;padding:13px;font-weight:600}
  .donebar b{color:var(--gold);font-family:'Bricolage Grotesque',sans-serif}
  .foot{margin-top:auto;padding-top:16px;text-align:center;color:var(--muted-2);font-size:11.5px}

  /* modales */
  .overlay{position:fixed;inset:0;background:rgba(5,12,11,.66);display:flex;align-items:flex-end;justify-content:center;z-index:40;padding:0}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:18px 18px 0 0;width:100%;max-width:480px;
    padding:18px 18px calc(18px + var(--safe-b));box-shadow:0 -10px 40px rgba(0,0,0,.4)}
  .modal h3{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:20px;margin:2px 0 3px;text-align:center}
  .modal .sub{color:var(--muted);text-align:center;font-size:13px;margin:0 0 16px}
  .display{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:44px;text-align:center;
    background:var(--bg-0);border:1px solid var(--line);border-radius:12px;padding:8px;margin-bottom:14px;min-height:64px;
    display:flex;align-items:center;justify-content:center;font-variant-numeric:tabular-nums}
  .display.empty{color:var(--muted-2)}
  .keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
  .keypad button{height:58px;border-radius:13px;background:var(--panel-2);border:1px solid var(--line);color:var(--ivory);
    font-size:24px;font-weight:600;font-family:'Bricolage Grotesque',sans-serif}
  .keypad button:active{transform:scale(.96);background:#234a40}
  .suggest{text-align:center;margin:-4px 0 14px}
  .suggest button{background:rgba(87,224,191,.14);border:1px solid var(--mint);color:var(--mint);border-radius:20px;padding:7px 16px;font-size:14px;font-weight:600}
  .mbtns{display:flex;gap:9px;margin-top:14px}
  .mbtns .btn{margin:0}
  .mbtns .btn.danger{background:transparent;color:var(--red);border-color:rgba(231,112,95,.5)}
  .fixedbtns{display:flex;flex-direction:column;gap:10px}
  .fixedbtns .btn{margin:0}
  .modal input.name{width:100%;background:var(--bg-0);border:1px solid var(--line);color:var(--ivory);
    border-radius:11px;padding:13px 14px;font-size:17px;font-family:inherit;outline:none;text-align:center;margin-bottom:6px}
  .modal input.name:focus{border-color:var(--gold)}

  .turntag{display:block;height:14px;line-height:14px;font-size:10px;color:var(--gold);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
  .phead.cur{background:rgba(240,181,61,.12)}
  .phead.cur .nm{color:var(--gold)}
  .cell.cur{background:rgba(240,181,61,.06)}
  .cell.cur.editable:active{background:rgba(240,181,61,.18)}

  .multi{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-bottom:4px}
  .mbtn{height:62px;border-radius:13px;background:var(--panel-2);border:1px solid var(--line);color:var(--ivory);
    display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px}
  .mbtn b{font-family:'Bricolage Grotesque',sans-serif;font-size:23px;font-weight:800;line-height:1}
  .mbtn small{font-size:11px;color:var(--muted)}
  .mbtn:active{transform:scale(.96);background:#234a40}
  .mbtn.sug{border-color:var(--mint);box-shadow:0 0 0 2px rgba(87,224,191,.3)}

  .namewrap{position:relative;margin-bottom:6px}
  .modal .namewrap input.name{margin-bottom:0;padding-right:46px}
  .clearname{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:30px;height:30px;border-radius:50%;
    background:var(--panel-2);border:1px solid var(--line);color:var(--muted);font-size:20px;line-height:1;
    display:flex;align-items:center;justify-content:center}

  .toast{position:fixed;left:50%;bottom:calc(20px + var(--safe-b));transform:translateX(-50%) translateY(20px);
    background:#222;color:#fff;padding:11px 18px;border-radius:11px;font-size:14px;opacity:0;transition:.2s;pointer-events:none;z-index:60;border:1px solid #3a3a3a}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  @media (prefers-reduced-motion:reduce){.die.rolling{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <div class="die-logo"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <h1>YAH<b>TZEE</b></h1>
  </div>

  <!-- ============ SETUP ============ -->
  <section id="setup" class="hidden">
    <div class="card">
      <p class="lead">Feuille de score pour la table. Tu tiens les scores, les autres peuvent suivre en lecture seule via le lien.</p>
      <span class="fld">Nombre de joueurs</span>
      <div class="stepper">
        <button id="minus">−</button>
        <span class="n" id="np">4</span>
        <button id="plus">+</button>
      </div>
      <span class="fld">Noms (modifiables)</span>
      <div class="names" id="names"></div>
      <div class="toggle-row">
        <div class="t">Dés virtuels<small>Optionnel — sinon dés réels sur la table</small></div>
        <div class="sw" id="diceSw"></div>
      </div>
      <button class="btn primary" id="btnStart">Commencer</button>
    </div>
  </section>

  <!-- ============ BOARD ============ -->
  <section id="board" class="hidden">
    <div class="topbar">
      <div>
        <div class="tt">Feuille de <b>score</b></div>
      </div>
      <span class="role" id="roleBadge"></span>
      <button class="iconbtn" id="btnShare" title="Partager"><svg viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg></button>
      <button class="iconbtn editonly" id="btnDice" title="Dés"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="15.5" cy="15.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="15.5" cy="8.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="8.5" cy="15.5" r="1.3" fill="currentColor" stroke="none"/></svg></button>
      <button class="iconbtn editonly" id="btnMenu" title="Menu"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none"/></svg></button>
    </div>

    <div class="viewbanner hidden" id="viewBanner">Lecture seule — seul le créateur peut modifier les scores.</div>

    <div class="dicepanel hidden" id="dicePanel">
      <div class="dice" id="dice"></div>
      <div class="rollsdots" id="rollsDots"></div>
      <div class="dicebtns">
        <button class="btn primary" id="btnRoll" style="flex:2">Lancer les dés</button>
        <button class="btn" id="btnResetDice" style="flex:1">Reset</button>
      </div>
    </div>

    <div class="sheet-wrap">
      <table class="sheet" id="sheet"></table>
    </div>

    <div class="donebar hidden" id="doneBar"></div>
    <div class="foot">Touche une case pour saisir · bonus +35 dès 63 en haut</div>
  </section>

  <div class="foot" id="loading">Connexion…</div>
</div>

<div class="toast" id="toast"></div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const socket = io({transports:["websocket","polling"]});
let S=null, editor=false, gid=null, token=null;
let prevDice=[1,1,1,1,1], prevRolled=false;

const CATS=[
  {k:"un",label:"As",hint:"somme des 1"},
  {k:"deux",label:"Deux",hint:"somme des 2"},
  {k:"trois",label:"Trois",hint:"somme des 3"},
  {k:"quatre",label:"Quatre",hint:"somme des 4"},
  {k:"cinq",label:"Cinq",hint:"somme des 5"},
  {k:"six",label:"Six",hint:"somme des 6"},
  {k:"brelan",label:"Brelan",hint:"3 identiques · somme"},
  {k:"carre",label:"Carré",hint:"4 identiques · somme"},
  {k:"full",label:"Full",hint:"25 pts"},
  {k:"petite_suite",label:"Petite suite",hint:"30 pts"},
  {k:"grande_suite",label:"Grande suite",hint:"40 pts"},
  {k:"yahtzee",label:"Yahtzee",hint:"50 pts"},
  {k:"chance",label:"Chance",hint:"somme des dés"},
];
const FIXED={full:25,petite_suite:30,grande_suite:40,yahtzee:50};
const UPPER=["un","deux","trois","quatre","cinq","six"];
const FACE={un:1,deux:2,trois:3,quatre:4,cinq:5,six:6};
const PIP={1:[4],2:[0,8],3:[0,4,8],4:[0,2,6,8],5:[0,2,4,6,8],6:[0,2,3,5,6,8]};
const LS="yahtzee_table_game";

const $=id=>document.getElementById(id);
function show(sec){["setup","board"].forEach(s=>$(s).classList.toggle("hidden",s!==sec));$("loading").classList.add("hidden");}
function toast(m){const t=$("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),1700);}
function save(){if(editor&&gid&&token&&S){localStorage.setItem(LS,JSON.stringify({id:gid,token,snapshot:{players:S.players.map(p=>({name:p.name,scores:p.scores})),dice_enabled:S.dice_enabled,current:S.current}}));}}

function scoreFor(cat,dice){
  const c={};dice.forEach(d=>c[d]=(c[d]||0)+1);
  const counts=Object.values(c), sum=dice.reduce((a,b)=>a+b,0), mx=Math.max(...counts), ds=new Set(dice);
  if(FACE[cat])return (c[FACE[cat]]||0)*FACE[cat];
  if(cat==="brelan")return mx>=3?sum:0;
  if(cat==="carre")return mx>=4?sum:0;
  if(cat==="full")return (counts.sort().join()==="2,3"||mx===5)?25:0;
  if(cat==="petite_suite")return [[1,2,3,4],[2,3,4,5],[3,4,5,6]].some(s=>s.every(x=>ds.has(x)))?30:0;
  if(cat==="grande_suite")return ([1,2,3,4,5].every(x=>ds.has(x))||[2,3,4,5,6].every(x=>ds.has(x)))?40:0;
  if(cat==="yahtzee")return mx===5?50:0;
  if(cat==="chance")return sum;
  return 0;
}

/* ---------- connexion / rôle ---------- */
socket.on("connect",()=>{
  const params=new URLSearchParams(location.search);
  const g=params.get("game");
  const stored=JSON.parse(localStorage.getItem(LS)||"null");
  if(g){
    if(stored&&stored.id===g){gid=g;token=stored.token;socket.emit("resume",stored);}
    else{gid=g;socket.emit("join_game",{id:g});}
  }else if(stored){
    gid=stored.id;token=stored.token;socket.emit("resume",stored);
  }else{
    openSetup();
  }
});
socket.on("created",d=>{gid=d.id;token=d.token;editor=true;history.replaceState(null,"","?game="+gid);save();});
socket.on("role",d=>{editor=!!d.editor;});
socket.on("no_game",()=>{toast("Partie introuvable");localStorage.removeItem(LS);history.replaceState(null,"",location.pathname);openSetup();});
socket.on("state",s=>{S=s;if(!gid)gid=s.id;save();render();});

/* ---------- SETUP ---------- */
let nPlayers=4, diceOn=false;
function openSetup(){show("setup");buildNames();$("diceSw").classList.toggle("on",diceOn);}
function buildNames(){
  const box=$("names");const old={};box.querySelectorAll("input").forEach((inp,i)=>old[i]=inp.value);
  box.innerHTML="";
  for(let i=0;i<nPlayers;i++){
    const row=document.createElement("div");row.className="num";
    row.innerHTML='<span>'+(i+1)+'</span>';
    const inp=document.createElement("input");inp.type="text";inp.maxLength=18;
    inp.placeholder="Joueur "+(i+1);inp.value=old[i]||"";
    row.appendChild(inp);box.appendChild(row);
  }
}
$("minus").onclick=()=>{if(nPlayers>1){nPlayers--;$("np").textContent=nPlayers;buildNames();}};
$("plus").onclick=()=>{if(nPlayers<10){nPlayers++;$("np").textContent=nPlayers;buildNames();}};
$("diceSw").onclick=()=>{diceOn=!diceOn;$("diceSw").classList.toggle("on",diceOn);};
$("btnStart").onclick=()=>{
  const names=[...$("names").querySelectorAll("input")].map((inp,i)=>inp.value.trim()||("Joueur "+(i+1)));
  localStorage.removeItem(LS);
  socket.emit("create_game",{names,dice_enabled:diceOn});
};

/* ---------- BOARD ---------- */
function render(){
  if(!S)return;
  show("board");
  $("roleBadge").textContent=editor?"éditeur":"lecture seule";
  $("roleBadge").className="role "+(editor?"edit":"view");
  $("viewBanner").classList.toggle("hidden",editor);
  document.querySelectorAll(".editonly").forEach(e=>e.classList.toggle("hidden",!editor));

  // dés
  const dp=$("dicePanel");
  dp.classList.toggle("hidden",!S.dice_enabled);
  if(S.dice_enabled)renderDice();

  renderSheet();

  if(S.complete&&S.leader>=0){
    $("doneBar").classList.remove("hidden");
    $("doneBar").innerHTML="Partie terminée — <b>"+escapeHtml(S.players[S.leader].name)+"</b> gagne avec "+S.players[S.leader].totals.total+" pts";
  }else{$("doneBar").classList.add("hidden");}
}

function dieEl(v,i){
  const d=document.createElement("div");d.className="die";d.dataset.i=i;
  for(let k=0;k<9;k++){const p=document.createElement("span");p.className="pip"+(PIP[v].includes(k)?" on":"");d.appendChild(p);}
  return d;
}
function renderDice(){
  const box=$("dice");box.innerHTML="";
  S.dice.forEach((v,i)=>{
    const d=dieEl(v,i);
    if(S.held[i])d.classList.add("held");
    if(!S.turn_rolled)d.classList.add("idle");
    if(editor&&S.turn_rolled&&S.rolls_left>0)d.onclick=()=>emit("toggle_hold",{index:i});
    if(S.turn_rolled&&prevRolled&&v!==prevDice[i]&&!S.held[i]){d.classList.add("rolling");d.style.animationDelay=(i*40)+"ms";}
    box.appendChild(d);
  });
  prevDice=S.dice.slice();prevRolled=S.turn_rolled;
  const r=$("rollsDots");r.innerHTML="";
  for(let i=0;i<3;i++){const p=document.createElement("span");p.className="pd"+(i<S.rolls_left?" on":"");r.appendChild(p);}
  const t=document.createElement("span");t.className="txt";
  t.textContent=S.turn_rolled?(S.rolls_left+" lancer"+(S.rolls_left>1?"s":"")+" restant"+(S.rolls_left>1?"s":"")):"3 lancers";
  r.appendChild(t);
  $("btnRoll").textContent=!S.turn_rolled?"Lancer les dés":(S.rolls_left>0?"Relancer ("+S.rolls_left+")":"Nouveau tour");
  $("btnRoll").disabled=!editor;
  $("btnResetDice").disabled=!editor;
}

function renderSheet(){
  const t=$("sheet");t.innerHTML="";
  const thead=document.createElement("thead");
  let tr=document.createElement("tr");
  tr.innerHTML='<th class="rowlabel">Catégorie</th>';
  S.players.forEach((p,i)=>{
    const th=document.createElement("th");
    th.className="phead"+(i===S.leader?" lead":"")+(i===S.current?" cur":"");
    th.innerHTML='<span class="nm">'+(i===S.leader?"♛ ":"")+escapeHtml(p.name)+'</span><span class="turntag">'+(i===S.current?"à jouer":"")+'</span>';
    if(editor)th.onclick=()=>openRename(i);
    tr.appendChild(th);
  });
  thead.appendChild(tr);t.appendChild(thead);

  const tb=document.createElement("tbody");
  const addCatRow=(cat,cls)=>{
    const row=document.createElement("tr");if(cls)row.className=cls;
    const lab=document.createElement("td");lab.className="rowlabel";
    lab.innerHTML=cat.label+'<span class="hint">'+cat.hint+'</span>';
    row.appendChild(lab);
    S.players.forEach((p,i)=>{
      const td=document.createElement("td");td.className="cell"+(i===S.current?" cur":"");
      const v=p.scores[cat.k];
      if(v!==null&&v!==undefined){td.innerHTML='<span class="v">'+v+'</span>';if(v===0)td.classList.add("zero");}
      else td.innerHTML='<span class="empty">+</span>';
      if(editor){td.classList.add("editable");td.onclick=()=>openCell(i,cat.k);}
      row.appendChild(td);
    });
    tb.appendChild(row);
  };
  const addTotalRow=(label,fn,cls,bonus)=>{
    const row=document.createElement("tr");row.className=cls;
    const lab=document.createElement("td");lab.className="rowlabel";
    lab.innerHTML=label+(bonus?' <span class="bonus-mini">(+35 dès 63)</span>':"");
    row.appendChild(lab);
    S.players.forEach(p=>{const td=document.createElement("td");td.className="cell";td.innerHTML='<span class="v">'+fn(p)+'</span>';row.appendChild(td);});
    tb.appendChild(row);
  };

  CATS.slice(0,6).forEach(c=>addCatRow(c));
  addTotalRow("Total (haut)",p=>p.totals.upper,"sub");
  addTotalRow("Bonus",p=>p.totals.bonus>0?"+35":(p.totals.upper+"/63"),"sub",true);
  CATS.slice(6).forEach((c,i)=>addCatRow(c,i===0?"sep":""));
  addTotalRow("TOTAL",p=>p.totals.total,"total sep");
  t.appendChild(tb);
}

/* ---------- saisie d'une case ---------- */
let cur={player:null,cat:null,buf:""};
function openCell(player,cat){
  if(player!==S.current){
    const p=S.players[player];
    openModal('<h3>Pas le joueur en cours</h3><div class="sub">'+"C'est à "+escapeHtml(S.players[S.current].name)+" de jouer."+'</div><div class="fixedbtns"><button class="btn primary" data-w="ok">Éditer '+escapeHtml(p.name)+' quand même</button><button class="btn" data-w="cancel">Annuler</button></div>');
    document.querySelectorAll("#modalRoot [data-w]").forEach(b=>{b.onclick=()=>{const a=b.dataset.w;closeModal();if(a==="ok")openCellEditor(player,cat);};});
  }else{
    openCellEditor(player,cat);
  }
}

function openCellEditor(player,cat){
  cur={player,cat,buf:""};
  const p=S.players[player], val=p.scores[cat], catMeta=CATS.find(c=>c.k===cat);
  const filled=(val!==null&&val!==undefined);
  const head='<h3>'+catMeta.label+'</h3><div class="sub">'+escapeHtml(p.name)+(filled?(" · actuel : "+val):"")+'</div>';

  // section du haut : les multiples du chiffre (0 à 5 dés)
  if(UPPER.includes(cat)){
    const n=FACE[cat];
    const sug=(S.dice_enabled&&S.turn_rolled)?scoreFor(cat,S.dice):-1;
    let html=head+'<div class="multi">';
    for(let k=0;k<=5;k++){const v=k*n;html+='<button class="mbtn'+(v===sug?" sug":"")+'" data-v="'+v+'"><b>'+v+'</b><small>'+k+" dé"+(k>1?"s":"")+'</small></button>';}
    html+='</div><div class="mbtns">';
    if(filled)html+='<button class="btn danger" data-act="clear">Effacer</button>';
    html+='<button class="btn" data-act="cancel">Annuler</button></div>';
    openModal(html);
    document.querySelectorAll("#modalRoot .mbtn").forEach(b=>{b.onclick=()=>{emit("set_score",{player,category:cat,value:parseInt(b.dataset.v,10)});closeModal();};});
    document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{b.onclick=()=>{const a=b.dataset.act;if(a==="cancel")closeModal();else if(a==="clear"){emit("set_score",{player,category:cat,value:null});closeModal();}};});
    return;
  }

  // cases à valeur fixe
  if(FIXED[cat]){
    const fv=FIXED[cat];
    let html=head+'<div class="fixedbtns">';
    html+='<button class="btn primary" data-act="fixed">Mettre '+fv+'</button>';
    html+='<button class="btn" data-act="zero">Mettre 0</button>';
    if(filled)html+='<button class="btn danger" data-act="clear">Effacer cette case</button>';
    html+='<button class="btn" data-act="cancel">Annuler</button></div>';
    openModal(html);
    bindFixed(cat);
    return;
  }

  // brelan, carré, chance : pavé numérique
  cur.buf=filled?String(val):"";
  let html=head+'<div class="display'+(cur.buf?"":" empty")+'" id="disp">'+(cur.buf||"0")+'</div>';
  if(S.dice_enabled&&S.turn_rolled){const sug=scoreFor(cat,S.dice);html+='<div class="suggest"><button data-act="sug" data-v="'+sug+'">Score des dés : '+sug+'</button></div>';}
  html+='<div class="keypad" id="kp"></div><div class="mbtns">';
  if(filled)html+='<button class="btn danger" data-act="clear">Effacer</button>';
  html+='<button class="btn" data-act="cancel">Annuler</button>';
  html+='<button class="btn primary" data-act="ok">Valider</button></div>';
  openModal(html);
  buildKeypad();
  bindNumber();
}
function buildKeypad(){
  const kp=$("kp");kp.innerHTML="";
  ["1","2","3","4","5","6","7","8","9","⌫","0","C"].forEach(t=>{
    const b=document.createElement("button");b.textContent=t;
    b.onclick=()=>{
      if(t==="⌫")cur.buf=cur.buf.slice(0,-1);
      else if(t==="C")cur.buf="";
      else{if(cur.buf.length<3)cur.buf+=t;}
      const d=$("disp");d.textContent=cur.buf||"0";d.classList.toggle("empty",!cur.buf);
    };
    kp.appendChild(b);
  });
}
function bindNumber(){
  document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{
    const a=b.dataset.act;
    b.onclick=()=>{
      if(a==="cancel")closeModal();
      else if(a==="clear"){emit("set_score",{player:cur.player,category:cur.cat,value:null});closeModal();}
      else if(a==="ok"){emit("set_score",{player:cur.player,category:cur.cat,value:cur.buf===""?0:parseInt(cur.buf,10)});closeModal();}
      else if(a==="sug"){cur.buf=b.dataset.v;const d=$("disp");d.textContent=cur.buf;d.classList.remove("empty");}
    };
  });
}
function bindFixed(cat){
  document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{
    const a=b.dataset.act;
    b.onclick=()=>{
      if(a==="cancel")closeModal();
      else if(a==="fixed"){emit("set_score",{player:cur.player,category:cur.cat,value:FIXED[cat]});closeModal();}
      else if(a==="zero"){emit("set_score",{player:cur.player,category:cur.cat,value:0});closeModal();}
      else if(a==="clear"){emit("set_score",{player:cur.player,category:cur.cat,value:null});closeModal();}
    };
  });
}

/* ---------- renommer / gérer joueur ---------- */
function openRename(i){
  const p=S.players[i];
  let html='<h3>Joueur</h3><div class="sub">Modifier le nom</div>';
  html+='<div class="namewrap"><input class="name" id="nameInp" maxlength="18" value="'+escapeAttr(p.name)+'"><button class="clearname" data-act="clr">×</button></div>';
  if(i!==S.current)html+='<button class="btn" data-act="turn" style="margin-bottom:10px">'+"C'est à "+escapeHtml(p.name)+" de jouer"+'</button>';
  html+='<div class="mbtns">';
  if(S.players.length>1)html+='<button class="btn danger" data-act="del">Supprimer</button>';
  html+='<button class="btn" data-act="cancel">Annuler</button>';
  html+='<button class="btn primary" data-act="save">Enregistrer</button></div>';
  openModal(html);
  const inp=$("nameInp");
  setTimeout(()=>{inp.focus();inp.select();},60);
  document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{
    const a=b.dataset.act;
    b.onclick=()=>{
      if(a==="cancel")closeModal();
      else if(a==="clr"){inp.value="";inp.focus();}
      else if(a==="turn"){emit("set_current",{player:i});closeModal();}
      else if(a==="save"){const n=inp.value.trim();if(n)emit("set_name",{player:i,name:n});closeModal();}
      else if(a==="del"){if(confirm("Supprimer "+p.name+" ?")){emit("remove_player",{player:i});closeModal();}}
    };
  });
}

/* ---------- menu ---------- */
$("btnMenu").onclick=()=>{
  let html='<h3>Menu</h3><div class="fixedbtns">';
  html+='<button class="btn" data-m="add">Ajouter un joueur</button>';
  html+='<button class="btn" data-m="share">Copier le lien (lecture seule)</button>';
  html+='<button class="btn danger" data-m="new">Nouvelle partie</button>';
  html+='<button class="btn" data-m="close">Fermer</button>';
  html+='</div>';
  openModal(html);
  document.querySelectorAll("#modalRoot [data-m]").forEach(b=>{
    b.onclick=()=>{
      const m=b.dataset.m;closeModal();
      if(m==="add")emit("add_player",{});
      else if(m==="share")shareLink();
      else if(m==="new"){if(confirm("Démarrer une nouvelle partie ? La feuille actuelle sera quittée.")){localStorage.removeItem(LS);location.href=location.pathname;}}
    };
  });
};
$("btnShare").onclick=shareLink;
function shareLink(){
  const url=location.origin+location.pathname+"?game="+gid;
  if(navigator.share){navigator.share({title:"Yahtzee — scores",url}).catch(()=>{});}
  else if(navigator.clipboard){navigator.clipboard.writeText(url).then(()=>toast("Lien copié !"),()=>toast(url));}
  else toast(url);
}

/* ---------- dés ---------- */
$("btnDice").onclick=()=>emit("toggle_dice",{enabled:!S.dice_enabled});
$("btnRoll").onclick=()=>emit("roll",{});
$("btnResetDice").onclick=()=>emit("reset_dice",{});

/* ---------- modale générique ---------- */
function openModal(html){
  let root=$("modalRoot");
  if(!root){root=document.createElement("div");root.id="modalRoot";document.body.appendChild(root);}
  root.innerHTML='<div class="overlay" id="ov"><div class="modal">'+html+'</div></div>';
  $("ov").onclick=e=>{if(e.target.id==="ov")closeModal();};
}
function closeModal(){const r=$("modalRoot");if(r)r.innerHTML="";}

/* ---------- util ---------- */
function emit(ev,data){socket.emit(ev,Object.assign({id:gid,token},data));}
function escapeHtml(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function escapeAttr(s){return (s||"").replace(/"/g,"&quot;").replace(/</g,"&lt;");}
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)