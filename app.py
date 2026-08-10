"""
Yahtzee — feuille de score pour la table (Flask + Socket.IO, fichier unique).

Concept : une personne (le créateur) tient la feuille de score sur son téléphone.
Les autres ouvrent le même lien et regardent en LECTURE SEULE (pas de code).
Saisie manuelle des scores (dés réels) ; dés virtuels en option.

Variantes (choisies à la création de la partie) :
  - Colonnes : chaque joueur joue 1 à 3 grilles en parallèle parmi
    désordre (libre), ordre descendant (haut -> bas) et ordre inversé
    (bas -> haut). À chaque tour, on remplit UNE case dans la colonne de
    son choix. Dans une colonne ordonnée, seule la prochaine case imposée
    est acceptée. Le total du joueur = somme de ses colonnes.
  - Mini & Maxi (règle Yamb) : deux cases "somme des dés" dans chaque
    colonne ; la colonne gagne (Maxi - Mini) x nombre de 1 de sa case As
    (0 si Maxi <= Mini ou si la case As vaut 0).

Lancer :  python app.py   ->  http://localhost:5000
Prod   :  gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:$PORT app:app
"""

import os
import random
import string
import time
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
MINIMAX_CATS = ["maxi", "mini"]
LOWER_CATS = ["brelan", "carre", "full", "petite_suite", "grande_suite", "yahtzee", "chance"]
CATEGORIES = UPPER_CATS + MINIMAX_CATS + LOWER_CATS  # superset de toutes les cases possibles
FIXED = {"full": 25, "petite_suite": 30, "grande_suite": 40, "yahtzee": 50}
MODES = ("libre", "ordre", "ordre_inverse")  # ordre canonique des colonnes


def clean_columns(cols):
    """Filtre/déduplique la liste de colonnes demandée, ordre canonique."""
    if not isinstance(cols, list):
        cols = None
    out = [m for m in MODES if cols and m in cols]
    return out or ["libre"]


def game_cats(g):
    """Cases actives pour une partie. L'ordre de la liste sert d'ordre imposé."""
    cats = UPPER_CATS[:]
    if g.get("minimax"):
        cats += MINIMAX_CATS
    return cats + LOWER_CATS


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
    if category in ("maxi", "mini"):
        return total
    if category == "chance":
        return total
    return 0


def totals(g, player):
    """Totaux par colonne + total général du joueur."""
    cats = game_cats(g)
    cols = []
    grand = 0
    complete = True
    for sc in player["scores"]:
        upper = sum(sc[c] or 0 for c in UPPER_CATS)
        bonus = 35 if upper >= 63 else 0
        lower = sum(sc[c] or 0 for c in LOWER_CATS)
        ecart = 0
        if g.get("minimax"):
            ma, mi = sc.get("maxi"), sc.get("mini")
            if ma is not None and mi is not None and ma > mi:
                # règle Yamb : (Maxi - Mini) x nombre de 1 de la case As
                # (la valeur de la case As = somme des 1 = leur nombre)
                ecart = (ma - mi) * (sc.get("un") or 0)
        tot = upper + bonus + lower + ecart
        grand += tot
        complete = complete and all(sc[c] is not None for c in cats)
        cols.append({"upper": upper, "bonus": bonus, "lower": lower, "ecart": ecart, "total": tot})
    return {"cols": cols, "total": grand, "complete": complete}


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


def make_player(name, ncols, cats):
    return {"name": name, "scores": [{c: None for c in cats} for _ in range(ncols)]}


def make_game(gid, names, dice_enabled, columns=None, minimax=False):
    g = {
        "id": gid,
        "players": [],
        "dice_enabled": bool(dice_enabled),
        "columns": clean_columns(columns),
        "minimax": bool(minimax),
        "dice": [1, 1, 1, 1, 1], "held": [False] * 5,
        "rolls_left": 3, "turn_rolled": False,
        "current": 0,
        "updated": time.time(),
    }
    cats = game_cats(g)
    g["players"] = [make_player(n, len(g["columns"]), cats) for n in names]
    return g


def next_required(g, player, col):
    """Prochaine case imposée dans une colonne ordonnée (None si colonne libre)."""
    mode = g["columns"][col]
    if mode == "libre":
        return None
    cats = game_cats(g)
    if mode == "ordre_inverse":
        cats = cats[::-1]
    sc = player["scores"][col]
    return next((c for c in cats if sc[c] is None), None)


def serialize(g):
    ps = [{"name": p["name"], "scores": p["scores"], "totals": totals(g, p)} for p in g["players"]]
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
        "columns": g["columns"], "minimax": g["minimax"],
        "dice": g["dice"], "held": g["held"],
        "rolls_left": g["rolls_left"], "turn_rolled": g["turn_rolled"],
        "complete": complete, "leader": leader, "current": g["current"],
    }


def broadcast(gid):
    g = games.get(gid)
    if g:
        g["updated"] = time.time()
        socketio.emit("state", serialize(g), to=gid)


def auth(data):
    """Tout le monde est admin : il suffit que la partie existe."""
    g = games.get((data or {}).get("id"))
    return g, (g is not None)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/ping")
def ping():
    return Response("ok", mimetype="text/plain")


# ---------------------------------------------------------------------------
# Événements
# ---------------------------------------------------------------------------

@socketio.on("create_game")
def on_create(data):
    names = data.get("names") or ["Joueur 1"]
    names = [(n or f"Joueur {i+1}").strip()[:18] or f"Joueur {i+1}" for i, n in enumerate(names)][:10]
    gid = new_id()
    games[gid] = make_game(gid, names, data.get("dice_enabled"),
                           data.get("columns"), data.get("minimax"))
    join_room(gid)
    emit("created", {"id": gid})
    broadcast(gid)


@socketio.on("open_game")
def on_open(data):
    gid = (data or {}).get("id")
    if not gid:
        emit("expired", {})
        return
    g = games.get(gid)
    if not g:
        # serveur redémarré / partie expirée : on recrée depuis l'instantané du client si dispo
        snap = (data or {}).get("snapshot") or {}
        names = [p.get("name", f"Joueur {i+1}") for i, p in enumerate(snap.get("players", []))]
        if not names:
            emit("expired", {})
            return
        cols = snap.get("columns")
        if cols is None and snap.get("mode"):  # anciens snapshots (une seule colonne)
            cols = [snap["mode"]]
        g = make_game(gid, names, snap.get("dice_enabled"), cols, snap.get("minimax"))
        for i, p in enumerate(snap.get("players", [])):
            sc = p.get("scores")
            if isinstance(sc, list):  # nouveau format : une grille par colonne
                for j in range(min(len(sc), len(g["columns"]))):
                    for c in game_cats(g):
                        v = (sc[j] or {}).get(c)
                        g["players"][i]["scores"][j][c] = v if isinstance(v, int) else None
            elif isinstance(sc, dict):  # ancien format à plat -> colonne 0
                for c in game_cats(g):
                    v = sc.get(c)
                    g["players"][i]["scores"][0][c] = v if isinstance(v, int) else None
        cu = snap.get("current", 0)
        g["current"] = cu if isinstance(cu, int) and 0 <= cu < len(g["players"]) else 0
        games[gid] = g
    join_room(gid)
    emit("state", serialize(g))


@socketio.on("list_games")
def on_list(data=None):
    items = []
    for gid, g in games.items():
        cats = game_cats(g)
        filled = sum(1 for p in g["players"] for sc in p["scores"]
                     for c in cats if sc[c] is not None)
        items.append({
            "id": gid,
            "players": [p["name"] for p in g["players"]],
            "filled": filled,
            "total": len(g["players"]) * len(g["columns"]) * len(cats),
            "updated": g.get("updated", 0),
        })
    items.sort(key=lambda x: x["updated"], reverse=True)
    emit("games_list", {"games": items[:12]})


@socketio.on("set_score")
def on_set_score(data):
    g, ok = auth(data)
    if not ok:
        return
    i = data.get("player")
    col = data.get("col", 0)
    cat = data.get("category")
    val = data.get("value")
    cats = game_cats(g)
    if not isinstance(i, int) or i < 0 or i >= len(g["players"]) or cat not in cats:
        return
    if not isinstance(col, int) or col < 0 or col >= len(g["columns"]):
        return
    p = g["players"][i]
    sc = p["scores"][col]
    if val is None:
        sc[cat] = None
    elif isinstance(val, (int, float)):
        was = sc[cat]
        # colonne ordonnée : une case vide ne peut être remplie que si c'est la prochaine imposée
        if was is None and g["columns"][col] != "libre" and cat != next_required(g, p, col):
            return
        hi = 30 if cat in MINIMAX_CATS else 999
        sc[cat] = max(0, min(hi, int(val)))
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
    g["players"].append(make_player(f"Joueur {len(g['players'])+1}",
                                    len(g["columns"]), game_cats(g)))
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
  .sw{width:52px;height:30px;border-radius:30px;background:var(--line);position:relative;transition:background .15s;flex:none;cursor:pointer}
  .sw.on{background:var(--gold)}
  .sw::after{content:"";position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:var(--ivory);transition:left .15s}
  .sw.on::after{left:25px}

  /* variantes (setup) : colonnes multi-sélection */
  .seg{display:flex;background:var(--bg-0);border:1px solid var(--line);border-radius:12px;padding:4px;gap:4px}
  .seg button{flex:1;padding:11px 4px;border-radius:9px;background:transparent;color:var(--muted);font-size:13.5px;font-weight:600;position:relative}
  .seg button.on{background:var(--gold);color:#2a1d04}
  .seg-hint{font-size:12px;color:var(--muted-2);margin:7px 2px 0;min-height:16px;line-height:1.4}

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
  .rowlabel{position:sticky;left:0;background:var(--panel);text-align:left !important;z-index:2;width:104px;min-width:88px;
    padding:9px 8px !important;font-weight:500;font-size:13px}
  table.sheet td.rowlabel,table.sheet th.rowlabel{white-space:normal;line-height:1.25;overflow-wrap:break-word}
  .rowlabel .hint,.rowlabel .bonus-mini{white-space:nowrap}
  thead .rowlabel{background:var(--panel-2);z-index:4}
  .rowlabel .hint{display:block;font-size:10.5px;color:var(--muted-2);font-weight:400;margin-top:1px}
  .phead{padding:9px 6px !important;min-width:66px}
  .phead .nm{display:block;max-width:110px;overflow:hidden;text-overflow:ellipsis;margin:0 auto;font-weight:600;font-size:13px}
  .phead.lead .nm{color:var(--gold)}
  .phead .crown{display:block;font-size:11px;color:var(--gold);height:13px;line-height:1}
  .cell{height:46px;font-variant-numeric:tabular-nums;font-size:16px}
  .cell.editable{cursor:pointer}
  .cell.editable:active{background:rgba(240,181,61,.12)}
  .cell .v{font-weight:600}
  .cell .empty{color:var(--muted-2);font-size:18px}
  .cell.next .empty{color:var(--gold);font-weight:600}
  .cell .lockdot{color:var(--muted-2);opacity:.45;font-size:18px}
  .cell.zero .v{color:var(--muted)}
  tr.sep td,tr.sep th{border-top:2px solid var(--line)}
  tr.sub td,tr.sub .rowlabel{background:var(--bg-1);color:var(--muted);font-size:12.5px}
  tr.sub .cell{color:var(--ivory);font-weight:500;height:36px;font-size:14px}
  tr.total td,tr.total .rowlabel{background:var(--panel-2);font-weight:700}
  tr.total .cell{color:var(--gold);font-family:'Bricolage Grotesque',sans-serif;font-size:19px;font-weight:800}
  tr.total .rowlabel{font-size:15px}
  .bonus-mini{font-size:10px;color:var(--muted-2);font-weight:400}

  /* mode multi-colonnes (classe "multicol" : ne pas réutiliser ".multi",
     déjà prise par la grille de boutons de la modale) */
  .psep{border-left:2px solid var(--line)}
  .colhead{font-size:10px;color:var(--muted);font-weight:600;padding:4px 3px !important;
    min-width:48px;text-transform:uppercase;letter-spacing:.03em}
  table.sheet.multicol thead tr:first-child th.phead{height:47px}
  table.sheet.multicol thead tr:nth-child(2) th{top:47px}
  table.sheet.multicol .cell{min-width:48px;font-size:14px;height:42px}
  table.sheet.multicol tr.total .cell{font-size:17px}
  table.sheet.multicol .phead{min-width:0}
  table.sheet.multicol .rowlabel{width:86px;min-width:76px;font-size:12.5px}

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

  .offlinebar{position:fixed;top:0;left:0;right:0;background:#3a2a12;color:var(--gold);text-align:center;
    font-size:12px;padding:6px;z-index:70;transform:translateY(-100%);transition:transform .2s}
  .offlinebar.show{transform:translateY(0)}

  .toast{position:fixed;left:50%;bottom:calc(20px + var(--safe-b));transform:translateX(-50%) translateY(20px);
    background:#222;color:#fff;padding:11px 18px;border-radius:11px;font-size:14px;opacity:0;transition:.2s;pointer-events:none;z-index:60;border:1px solid #3a3a3a}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .ongoing-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .linkbtn{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:9px;width:34px;height:34px;font-size:16px}
  .gamerow{width:100%;display:flex;align-items:center;gap:10px;background:var(--bg-0);border:1px solid var(--line);
    border-radius:11px;padding:12px 13px;color:var(--ivory);text-align:left;margin-bottom:8px;transition:transform .08s}
  .gamerow:last-child{margin-bottom:0}
  .gamerow:active{transform:scale(.99)}
  .gamerow .gi{min-width:0;flex:1}
  .gamerow .gn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .gamerow .gp{font-size:12px;color:var(--muted);margin-top:2px}
  .gamerow .join{color:var(--gold);font-weight:600;font-size:14px;flex:none}
  .btn.resume{display:flex;flex-direction:column;align-items:flex-start;gap:1px;margin-bottom:14px;text-align:left}
  .btn.resume small{font-weight:400;color:#6b551a;font-size:12px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  @media (prefers-reduced-motion:reduce){.die.rolling{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <div class="die-logo"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <h1>YAH<b>TZEE</b></h1>
  </div>

  <!-- ============ ACCUEIL ============ -->
  <section id="setup" class="hidden">
    <div id="resumeWrap"></div>
    <div class="card hidden" id="ongoingCard" style="padding-bottom:12px">
      <div class="ongoing-head">
        <span class="fld" style="margin:0">Parties en cours</span>
        <button id="refreshGames" class="linkbtn" title="Rafraîchir">↻</button>
      </div>
      <div id="gamesList"></div>
    </div>
    <div class="card">
      <p class="lead">Nouvelle partie — feuille de score pour la table. Partage le lien : tout le monde saisit les scores depuis son téléphone, sur la même feuille.</p>
      <span class="fld">Nombre de joueurs</span>
      <div class="stepper">
        <button id="minus">−</button>
        <span class="n" id="np">4</span>
        <button id="plus">+</button>
      </div>
      <span class="fld">Noms (modifiables)</span>
      <div class="names" id="names"></div>
      <span class="fld" style="margin-top:16px">Colonnes de la grille (cumulables)</span>
      <div class="seg" id="colSeg">
        <button type="button" data-mode="libre" class="on">Désordre</button>
        <button type="button" data-mode="ordre">Ordre ↓</button>
        <button type="button" data-mode="ordre_inverse">Ordre ↑</button>
      </div>
      <div class="seg-hint" id="colHint"></div>
      <div class="toggle-row" style="margin:14px 0 0">
        <div class="t">Mini &amp; Maxi<small>Règle Yamb : (Maxi − Mini) × nb de 1 de la case As s'ajoute au total</small></div>
        <div class="sw" id="mmSw"></div>
      </div>
      <button class="btn primary" id="btnStart" style="margin-top:16px">Commencer</button>
    </div>
  </section>

  <!-- ============ BOARD ============ -->
  <section id="board" class="hidden">
    <div class="topbar">
      <div>
        <div class="tt">Feuille de <b>score</b></div>
      </div>
      <button class="iconbtn" id="btnShare" title="Partager"><svg viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg></button>
      <button class="iconbtn" id="btnDice" title="Dés"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="15.5" cy="15.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="15.5" cy="8.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="8.5" cy="15.5" r="1.3" fill="currentColor" stroke="none"/></svg></button>
      <button class="iconbtn" id="btnMenu" title="Menu"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none"/></svg></button>
    </div>

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
    <div class="foot" id="sheetFoot">Touche une case pour saisir · bonus +35 dès 63 en haut</div>
  </section>

  <div class="foot" id="loading">Connexion à la partie…</div>
</div>

<div class="toast" id="toast"></div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const socket = io({transports:["websocket","polling"]});
let S=null, gid=null;
let prevDice=[1,1,1,1,1], prevRolled=false;

const CATS_UPPER=[
  {k:"un",label:"As",hint:"somme des 1"},
  {k:"deux",label:"Deux",hint:"somme des 2"},
  {k:"trois",label:"Trois",hint:"somme des 3"},
  {k:"quatre",label:"Quatre",hint:"somme des 4"},
  {k:"cinq",label:"Cinq",hint:"somme des 5"},
  {k:"six",label:"Six",hint:"somme des 6"},
];
const CATS_MM=[
  {k:"maxi",label:"Maxi",hint:"somme des dés<br>· viser haut"},
  {k:"mini",label:"Mini",hint:"somme des dés<br>· viser bas"},
];
const CATS_LOWER=[
  {k:"brelan",label:"Brelan",hint:"3 identiques ·<br>somme des dés"},
  {k:"carre",label:"Carré",hint:"4 identiques ·<br>somme des dés"},
  {k:"full",label:"Full",hint:"25 pts"},
  {k:"petite_suite",label:"Petite suite",hint:"30 pts"},
  {k:"grande_suite",label:"Grande suite",hint:"40 pts"},
  {k:"yahtzee",label:"Yahtzee",hint:"50 pts"},
  {k:"chance",label:"Chance",hint:"somme des dés"},
];
function gameCats(){let c=CATS_UPPER.slice();if(S&&S.minimax)c=c.concat(CATS_MM);return c.concat(CATS_LOWER);}
function labelOf(k){const c=gameCats().find(x=>x.k===k);return c?c.label:k;}
const COL_LABELS={libre:"Libre",ordre:"Ordre ↓",ordre_inverse:"Ordre ↑"};
const COL_NAMES={libre:"désordre",ordre:"ordre ↓",ordre_inverse:"ordre ↑"};
const MODES_ORDER=["libre","ordre","ordre_inverse"];
const FIXED={full:25,petite_suite:30,grande_suite:40,yahtzee:50};
const UPPER=["un","deux","trois","quatre","cinq","six"];
const FACE={un:1,deux:2,trois:3,quatre:4,cinq:5,six:6};
const PIP={1:[4],2:[0,8],3:[0,4,8],4:[0,2,6,8],5:[0,2,4,6,8],6:[0,2,3,5,6,8]};
const LS="yahtzee_table_game";

const $=id=>document.getElementById(id);
function show(sec){["setup","board"].forEach(s=>$(s).classList.toggle("hidden",s!==sec));$("loading").classList.add("hidden");}
function toast(m){const t=$("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),1700);}
function readStored(){try{return JSON.parse(localStorage.getItem(LS)||"null");}catch(e){return null;}}
function save(){if(gid){localStorage.setItem(LS,JSON.stringify({id:gid,snapshot:S?{players:S.players.map(p=>({name:p.name,scores:p.scores})),dice_enabled:S.dice_enabled,current:S.current,columns:S.columns,minimax:S.minimax}:((readStored()||{}).snapshot||null)}));}}

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
  if(cat==="maxi"||cat==="mini")return sum;
  if(cat==="chance")return sum;
  return 0;
}

/* colonne ordonnée : prochaine case imposée pour un joueur (null si colonne libre) */
function nextCatFor(p,col){
  if(!S)return null;
  const mode=S.columns[col];
  if(mode==="libre")return null;
  let ks=gameCats().map(c=>c.k);
  if(mode==="ordre_inverse")ks=ks.slice().reverse();
  return ks.find(k=>p.scores[col][k]==null)||null;
}

/* ---------- identité / connexion ----------
   Tout le monde est admin : il suffit d'avoir le lien de la partie.
   Chaque appareil garde une copie locale ; à la (re)connexion il l'envoie
   pour restaurer la partie si le serveur a redémarré. */
function identify(){
  const g=new URLSearchParams(location.search).get("game");
  if(g){const stored=readStored();gid=g;socket.emit("open_game",{id:g,snapshot:(stored&&stored.id===g)?stored.snapshot:null});}
  else openSetup();
}
function setOnline(on){
  let el=$("offlinebar");
  if(!el){el=document.createElement("div");el.id="offlinebar";el.className="offlinebar";el.textContent="Reconnexion…";document.body.appendChild(el);}
  el.classList.toggle("show",!on);
}
socket.on("connect",()=>{setOnline(true);identify();});
socket.on("disconnect",()=>setOnline(false));
socket.on("created",d=>{gid=d.id;history.replaceState(null,"","?game="+gid);save();});
socket.on("expired",()=>{
  // la partie n'existe plus (serveur en veille) : retour à l'accueil, jamais de page vide
  toast("Cette partie n'existe plus");
  history.replaceState(null,"",location.pathname);
  gid=null;S=null;
  openSetup();
});
socket.on("state",s=>{S=s;if(!gid)gid=s.id;save();render();});

/* garder le serveur réveillé pendant qu'on joue (Render s'endort sans trafic) */
setInterval(()=>{if(document.visibilityState==="visible")fetch("/ping",{cache:"no-store"}).catch(()=>{});},9*60*1000);
/* rafraîchir la liste des parties tant qu'on est sur l'accueil */
setInterval(()=>{const s=$("setup");if(s&&!s.classList.contains("hidden"))fetchGames();},6000);

/* retour précédent/suivant (page restaurée depuis le cache) */
window.addEventListener("pageshow",e=>{if(e.persisted){if(socket.connected)identify();else socket.connect();}});
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible"&&!socket.connected)socket.connect();});

/* ---------- SETUP ---------- */
let nPlayers=4;
let selCols=["libre"], selMM=false;
function colHintTxt(){
  if(selCols.length===1){
    return {
      libre:"1 colonne — chacun remplit ses cases dans l'ordre qu'il veut.",
      ordre:"1 colonne — à remplir de haut en bas (As → Chance).",
      ordre_inverse:"1 colonne — à remplir de bas en haut (Chance → As)."
    }[selCols[0]];
  }
  return selCols.length+" colonnes ("+selCols.map(m=>COL_NAMES[m]).join(" + ")+") — à chaque tour, on inscrit son score dans une seule case, dans la colonne de son choix.";
}
function refreshColSeg(){
  document.querySelectorAll("#colSeg button").forEach(b=>b.classList.toggle("on",selCols.includes(b.dataset.mode)));
  $("colHint").textContent=colHintTxt();
}
function openSetup(){show("setup");buildNames();renderResume();fetchGames();refreshColSeg();}

function fetchGames(){if(socket.connected)socket.emit("list_games",{});}
socket.on("games_list",d=>renderGames(d.games||[]));
function renderGames(list){
  const stored=readStored();
  list=list.filter(g=>!(stored&&stored.id===g.id));
  const card=$("ongoingCard"),box=$("gamesList");
  if(!list.length){card.classList.add("hidden");return;}
  card.classList.remove("hidden");box.innerHTML="";
  list.forEach(g=>{
    const row=document.createElement("button");row.className="gamerow";
    row.innerHTML='<div class="gi"><div class="gn"></div><div class="gp">'+g.filled+"/"+g.total+' cases remplies</div></div><span class="join">Rejoindre →</span>';
    row.querySelector(".gn").textContent=g.players.join(", ")||"Partie";
    row.onclick=()=>joinGame(g.id);
    box.appendChild(row);
  });
}
function renderResume(){
  const w=$("resumeWrap");w.innerHTML="";const stored=readStored();
  if(stored&&stored.id&&stored.snapshot&&stored.snapshot.players&&stored.snapshot.players.length){
    const names=stored.snapshot.players.map(p=>p.name).join(", ");
    const b=document.createElement("button");b.className="btn primary resume";
    b.innerHTML="Reprendre ma partie<small></small>";
    b.querySelector("small").textContent=names;
    b.onclick=()=>joinGame(stored.id);
    w.appendChild(b);
  }
}
function joinGame(id){
  const stored=readStored();gid=id;
  history.replaceState(null,"","?game="+id);
  socket.emit("open_game",{id,snapshot:(stored&&stored.id===id)?stored.snapshot:null});
}
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
document.querySelectorAll("#colSeg button").forEach(b=>{
  b.onclick=()=>{
    const m=b.dataset.mode;
    if(selCols.includes(m)){
      if(selCols.length===1){toast("Il faut au moins une colonne");return;}
      selCols=selCols.filter(x=>x!==m);
    }else{
      selCols=MODES_ORDER.filter(x=>selCols.includes(x)||x===m);
    }
    refreshColSeg();
  };
});
$("mmSw").onclick=()=>{selMM=!selMM;$("mmSw").classList.toggle("on",selMM);};
$("btnStart").onclick=()=>{
  const names=[...$("names").querySelectorAll("input")].map((inp,i)=>inp.value.trim()||("Joueur "+(i+1)));
  localStorage.removeItem(LS);
  socket.emit("create_game",{names,dice_enabled:false,columns:selCols,minimax:selMM});
};
$("refreshGames").onclick=fetchGames;

/* ---------- BOARD ---------- */
function render(){
  if(!S)return;
  show("board");

  // dés
  const dp=$("dicePanel");
  dp.classList.toggle("hidden",!S.dice_enabled);
  if(S.dice_enabled)renderDice();

  renderSheet();

  const nCols=S.columns.length;
  let foot="Touche une case pour saisir · bonus +35 dès 63 en haut";
  if(nCols>1)foot+=" · "+nCols+" colonnes par joueur";
  else if(S.columns[0]==="ordre")foot+=" · ordre ↓ imposé";
  else if(S.columns[0]==="ordre_inverse")foot+=" · ordre ↑ imposé";
  $("sheetFoot").textContent=foot;

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
    if(S.turn_rolled&&S.rolls_left>0)d.onclick=()=>emit("toggle_hold",{index:i});
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
}

function renderSheet(){
  const t=$("sheet");t.innerHTML="";
  const nCols=S.columns.length, multi=nCols>1;
  t.className="sheet"+(multi?" multicol":"");

  const thead=document.createElement("thead");
  const tr1=document.createElement("tr");
  const corner=document.createElement("th");corner.className="rowlabel";corner.textContent="Catégorie";
  if(multi)corner.rowSpan=2;
  tr1.appendChild(corner);
  S.players.forEach((p,i)=>{
    const th=document.createElement("th");
    th.className="phead"+(i===S.leader?" lead":"")+(i===S.current?" cur":"")+(multi&&i>0?" psep":"");
    th.colSpan=nCols;
    th.innerHTML='<span class="nm">'+(i===S.leader?"♛ ":"")+escapeHtml(p.name)+'</span><span class="turntag">'+(i===S.current?"à jouer":"")+'</span>';
    th.onclick=()=>openRename(i);
    tr1.appendChild(th);
  });
  thead.appendChild(tr1);
  if(multi){
    const tr2=document.createElement("tr");
    S.players.forEach((p,i)=>{
      S.columns.forEach((m,j)=>{
        const th=document.createElement("th");
        th.className="colhead"+((j===0&&i>0)?" psep":"");
        th.textContent=COL_LABELS[m];
        tr2.appendChild(th);
      });
    });
    thead.appendChild(tr2);
  }
  t.appendChild(thead);

  const tb=document.createElement("tbody");
  const addCatRow=(cat,cls)=>{
    const row=document.createElement("tr");if(cls)row.className=cls;
    const lab=document.createElement("td");lab.className="rowlabel";
    lab.innerHTML=cat.label+'<span class="hint">'+cat.hint+'</span>';
    row.appendChild(lab);
    S.players.forEach((p,i)=>{
      S.columns.forEach((m,j)=>{
        const td=document.createElement("td");
        td.className="cell"+(i===S.current?" cur":"")+((multi&&j===0&&i>0)?" psep":"");
        const v=p.scores[j][cat.k];
        if(v!==null&&v!==undefined){
          td.innerHTML='<span class="v">'+v+'</span>';if(v===0)td.classList.add("zero");
          td.classList.add("editable");td.onclick=()=>openCell(i,j,cat.k);
        }else{
          const nxt=nextCatFor(p,j);
          if(nxt&&nxt!==cat.k){
            // colonne ordonnée : case verrouillée tant que ce n'est pas son tour dans la grille
            td.innerHTML='<span class="lockdot">·</span>';
            td.onclick=()=>toast("Colonne "+COL_LABELS[m]+" — case suivante : "+labelOf(nxt));
          }else{
            if(nxt)td.classList.add("next");
            td.innerHTML='<span class="empty">+</span>';
            td.classList.add("editable");td.onclick=()=>openCell(i,j,cat.k);
          }
        }
        row.appendChild(td);
      });
    });
    tb.appendChild(row);
  };
  const addTotalRow=(label,fn,cls)=>{
    const row=document.createElement("tr");row.className=cls;
    const lab=document.createElement("td");lab.className="rowlabel";
    lab.innerHTML=label;
    row.appendChild(lab);
    S.players.forEach((p,i)=>{
      S.columns.forEach((m,j)=>{
        const td=document.createElement("td");
        td.className="cell"+((multi&&j===0&&i>0)?" psep":"");
        td.innerHTML='<span class="v">'+fn(p,j)+'</span>';
        row.appendChild(td);
      });
    });
    tb.appendChild(row);
  };

  CATS_UPPER.forEach(c=>addCatRow(c));
  addTotalRow("Total (haut)",(p,j)=>p.totals.cols[j].upper,"sub");
  addBonusRow(tb,multi);
  if(S.minimax){
    addCatRow(CATS_MM[0],"sep");
    addCatRow(CATS_MM[1]);
    addTotalRow('Écart × As<span class="bonus-mini" style="display:block">(Max − Min) × As</span>',(p,j)=>{
      const s=p.scores[j], ma=s.maxi, mi=s.mini;
      if(ma===null||ma===undefined||mi===null||mi===undefined)return "—";
      const d=Math.max(0,ma-mi);
      if(s.un===null||s.un===undefined)return d>0?d+"×?":0;  // en attente de la case As
      return p.totals.cols[j].ecart;
    },"sub");
  }
  CATS_LOWER.forEach((c,i)=>addCatRow(c,i===0?"sep":""));
  if(multi){
    addTotalRow("Total colonne",(p,j)=>p.totals.cols[j].total,"sub sep");
    const row=document.createElement("tr");row.className="total";
    const lab=document.createElement("td");lab.className="rowlabel";lab.textContent="TOTAL";
    row.appendChild(lab);
    S.players.forEach((p,i)=>{
      const td=document.createElement("td");
      td.className="cell"+(i>0?" psep":"");td.colSpan=nCols;
      td.innerHTML='<span class="v">'+p.totals.total+'</span>';
      row.appendChild(td);
    });
    tb.appendChild(row);
  }else{
    addTotalRow("TOTAL",(p,j)=>p.totals.total,"total sep");
  }
  t.appendChild(tb);
}

function addBonusRow(tb,multi){
  const row=document.createElement("tr");row.className="sub bonusrow";
  const lab=document.createElement("td");lab.className="rowlabel";
  lab.innerHTML='Bonus<span class="bonus-mini" style="display:block">(+35 dès 63)</span>';
  row.appendChild(lab);
  S.players.forEach((p,i)=>{
    S.columns.forEach((m,j)=>{
      const td=document.createElement("td");
      td.className="cell"+(i===S.current?" cur":"")+((multi&&j===0&&i>0)?" psep":"");
      const ct=p.totals.cols[j];
      if(ct.bonus>0){
        td.innerHTML='<span class="v" style="color:var(--mint)">+35 ✓</span>';
      }else{
        const reste=Math.max(0,63-ct.upper);
        td.innerHTML='<span class="v">'+ct.upper+'/63</span><span class="bonus-mini" style="display:block">reste '+reste+'</span>';
      }
      row.appendChild(td);
    });
  });
  tb.appendChild(row);
}

/* ---------- saisie d'une case ---------- */
let cur={player:null,col:0,cat:null,buf:""};
function openCell(player,col,cat){
  if(player!==S.current){
    const p=S.players[player];
    openModal('<h3>Pas le joueur en cours</h3><div class="sub">'+"C'est à "+escapeHtml(S.players[S.current].name)+" de jouer."+'</div><div class="fixedbtns"><button class="btn primary" data-w="ok">Éditer '+escapeHtml(p.name)+' quand même</button><button class="btn" data-w="cancel">Annuler</button></div>');
    document.querySelectorAll("#modalRoot [data-w]").forEach(b=>{b.onclick=()=>{const a=b.dataset.w;closeModal();if(a==="ok")openCellEditor(player,col,cat);};});
  }else{
    openCellEditor(player,col,cat);
  }
}

function openCellEditor(player,col,cat){
  cur={player,col,cat,buf:""};
  const p=S.players[player], val=p.scores[col][cat], catMeta=gameCats().find(c=>c.k===cat);
  const filled=(val!==null&&val!==undefined);
  const multi=S.columns.length>1;
  const sub=escapeHtml(p.name)+(multi?" · colonne "+COL_LABELS[S.columns[col]]:"")+(filled?(" · actuel : "+val):"");
  const head='<h3>'+catMeta.label+'</h3><div class="sub">'+sub+'</div>';

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
    document.querySelectorAll("#modalRoot .mbtn").forEach(b=>{b.onclick=()=>{emit("set_score",{player,col,category:cat,value:parseInt(b.dataset.v,10)});closeModal();};});
    document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{b.onclick=()=>{const a=b.dataset.act;if(a==="cancel")closeModal();else if(a==="clear"){emit("set_score",{player,col,category:cat,value:null});closeModal();}};});
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

  // brelan, carré, chance, maxi, mini : pavé numérique
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
      else if(a==="clear"){emit("set_score",{player:cur.player,col:cur.col,category:cur.cat,value:null});closeModal();}
      else if(a==="ok"){emit("set_score",{player:cur.player,col:cur.col,category:cur.cat,value:cur.buf===""?0:parseInt(cur.buf,10)});closeModal();}
      else if(a==="sug"){cur.buf=b.dataset.v;const d=$("disp");d.textContent=cur.buf;d.classList.remove("empty");}
    };
  });
}
function bindFixed(cat){
  document.querySelectorAll("#modalRoot [data-act]").forEach(b=>{
    const a=b.dataset.act;
    b.onclick=()=>{
      if(a==="cancel")closeModal();
      else if(a==="fixed"){emit("set_score",{player:cur.player,col:cur.col,category:cur.cat,value:FIXED[cat]});closeModal();}
      else if(a==="zero"){emit("set_score",{player:cur.player,col:cur.col,category:cur.cat,value:0});closeModal();}
      else if(a==="clear"){emit("set_score",{player:cur.player,col:cur.col,category:cur.cat,value:null});closeModal();}
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
  html+='<button class="btn" data-m="nextp">Passer au joueur suivant</button>';
  html+='<button class="btn" data-m="add">Ajouter un joueur</button>';
  html+='<button class="btn" data-m="delp">Supprimer un joueur</button>';
  html+='<button class="btn" data-m="share">Copier le lien de la partie</button>';
  html+='<button class="btn" data-m="home">Accueil / autre partie</button>';
  html+='<button class="btn" data-m="close">Fermer</button>';
  html+='</div>';
  openModal(html);
  document.querySelectorAll("#modalRoot [data-m]").forEach(b=>{
    b.onclick=()=>{
      const m=b.dataset.m;closeModal();
      if(m==="add")emit("add_player",{});
      else if(m==="delp")openDeletePlayer();
      else if(m==="nextp"){
        const nxt=(S.current+1)%S.players.length;
        emit("set_current",{player:nxt});
        toast("À "+S.players[nxt].name+" de jouer");
      }
      else if(m==="share")shareLink();
      else if(m==="home"){history.replaceState(null,"",location.pathname);gid=null;S=null;openSetup();}
    };
  });
};

function openDeletePlayer(){
  if(S.players.length<=1){toast("Il faut au moins un joueur");return;}
  let html='<h3>Supprimer un joueur</h3><div class="sub">Choisis le joueur à retirer (ses scores seront perdus)</div><div class="fixedbtns">';
  S.players.forEach((p,i)=>{html+='<button class="btn" data-del="'+i+'">'+escapeHtml(p.name)+'</button>';});
  html+='<button class="btn danger" data-del="cancel" style="border-color:var(--line);color:var(--muted)">Annuler</button></div>';
  openModal(html);
  document.querySelectorAll("#modalRoot [data-del]").forEach(b=>{
    b.onclick=()=>{
      const v=b.dataset.del;
      if(v==="cancel"){closeModal();return;}
      const i=parseInt(v,10);
      if(confirm("Supprimer "+S.players[i].name+" ?")){emit("remove_player",{player:i});closeModal();}
    };
  });
}
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
function emit(ev,data){socket.emit(ev,Object.assign({id:gid},data));}
function escapeHtml(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function escapeAttr(s){return (s||"").replace(/"/g,"&quot;").replace(/</g,"&lt;");}
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
