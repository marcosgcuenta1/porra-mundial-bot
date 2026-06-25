# -*- coding: utf-8 -*-
"""
Bot de Telegram MULTI-USUARIO — Porra Mundial 2026 (NFQ World Cup).

Cualquier participante puede usarlo: escribe al bot, dice quién es y, a partir de
ahí, en cada partido recibe:
  - Al comienzo: SU pronóstico ("Comienzo de 🇪🇸 ESP - KOR 🇰🇷", su ganador en negrita).
  - Al terminar: el resultado ("✅ Final: 🇪🇸 ESP 1-0 KOR 🇰🇷") + su zona de la clasificación.

Cada usuario ve sus propios datos: su porra oficial (de Supabase) y su posición.

Variables de entorno:
  TELEGRAM_TOKEN        token del bot de Telegram                 (obligatorio)
  FOOTBALL_DATA_TOKEN   token de https://www.football-data.org    (necesario para partidos)
  STATE_FILE            ruta del fichero de estado (opcional)
"""
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from data import TEAMS, PORRA

TG_API = "https://api.telegram.org/bot{token}/{method}"
SB_URL = "https://zavqpnsbsmivsuvwurkd.supabase.co"
SB_KEY = "sb_publishable_gzIrT1J8xG5ZGTIxJHFzfg_BAO-XGPm"
# Misma API de partidos que usa la web de la porra (resultados consistentes con el ranking).
SPORTS_URL = "https://sports.bzzoiro.com/api/v2/events/?league_id=27&season_id=188&limit=200"
SPORTS_KEY = os.environ.get("SPORTS_TOKEN", "65282d7cc77d80d27171566864fc427e7a6f1266")
# Goleadores reales (ESPN, sin registro): se suman partido a partido.
ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260611-20260720&limit=400"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={}"
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1104050697")  # solo el admin ve /usuarios
ENC_FILE = os.path.join(os.path.dirname(STATE_FILE), "state.enc")
STATE_KEY = os.environ.get("BOT_STATE_KEY")  # si está, el estado se guarda CIFRADO (privado)

COMIENZO_MAX_RETRASO_H = 6
GROUP_LETTERS = "ABCDEFGHIJKL"
LIVE_ST = {"inprogress", "1h", "ht", "2h", "et", "bt", "p", "break", "live", "penalties"}
FINISHED_ST = {"finished", "ft", "aet", "ap", "after_extra_time", "after_penalties"}
ESP_TZ = timezone(timedelta(hours=2))  # España en verano (CEST), todo el Mundial


# --------------------------------------------------------------------------- #
# Casado de equipos y claves de partido
# --------------------------------------------------------------------------- #
def normalize(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


_ALIAS_INDEX = {}
for _code, (_name, _flag, _aliases) in TEAMS.items():
    _ALIAS_INDEX[normalize(_code)] = _code
    _ALIAS_INDEX[normalize(_name)] = _code
    for _a in _aliases:
        _ALIAS_INDEX[normalize(_a)] = _code


def match_team(name):
    """Devuelve el code interno a partir del nombre de equipo de la API (string)."""
    return _ALIAS_INDEX.get(normalize(name)) if name else None


# Clave de grupo por pareja (igual que la web): frozenset -> (key, home_code, away_code)
GROUP_KEYS = {}
for _i, (_h, _a, _pick) in enumerate(PORRA):
    _key = "{}_{}".format(GROUP_LETTERS[_i // 6], _i % 6)
    GROUP_KEYS[frozenset((_h, _a))] = (_key, _h, _a)


def user_pick(gr, code_a, code_b):
    """Ganador pronosticado (code) por un participante para la pareja a-b, o None (empate/sin dato)."""
    gk = GROUP_KEYS.get(frozenset((code_a, code_b)))
    if not gk:
        return None
    key, hc, ac = gk
    pick = (gr or {}).get(key)
    return hc if pick == "1" else (ac if pick == "2" else None)


# --------------------------------------------------------------------------- #
# Estado (multi-usuario)
# --------------------------------------------------------------------------- #
def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(STATE_KEY.encode())


def _read_raw():
    """Lee el estado: cifrado (state.enc) si hay clave, si no texto plano (state.json)."""
    if STATE_KEY and os.path.exists(ENC_FILE):
        try:
            with open(ENC_FILE, "rb") as f:
                return _fernet().decrypt(f.read()).decode("utf-8")
        except Exception as e:
            print("decrypt error:", e, file=sys.stderr)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return None


def load_state():
    raw = _read_raw()
    try:
        st = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        st = {}
    st.setdefault("comienzo", [])
    st.setdefault("final", [])
    st.setdefault("seeded", False)
    st.setdefault("tg_offset", 0)
    st.setdefault("scorers", {"events": [], "goals": {}, "ts": None})
    # Migración del antiguo formato de un solo usuario.
    if "users" not in st:
        st["users"] = {}
        old = st.get("identity")
        cid = os.environ.get("TELEGRAM_CHAT_ID")
        if old and old.get("confirmed") and cid:
            st["users"][str(cid)] = {"pid": old["pid"], "name": old["name"],
                                     "confirmed": True, "asked": True,
                                     "stage": "done", "candidates": []}
    st.pop("identity", None)
    return st


def save_state(st):
    data = json.dumps(st, ensure_ascii=False, indent=2)
    if STATE_KEY:  # cifrado -> state.enc (privado)
        with open(ENC_FILE, "wb") as f:
            f.write(_fernet().encrypt(data.encode("utf-8")))
    else:          # local / sin clave -> texto plano
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(data)


_last_commit = [0.0]


def commit_state():
    """En GitHub Actions, guarda el estado (cifrado) en el repo para el siguiente relevo."""
    target = "state.enc" if STATE_KEY else "state.json"
    try:
        subprocess.run(["git", "add", target], check=False)
        r = subprocess.run(["git", "-c", "user.name=bot",
                            "-c", "user.email=bot@users.noreply.github.com",
                            "commit", "-m", "Estado del bot [skip ci]"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            subprocess.run(["git", "push", "origin", "HEAD:master"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception as e:
        print("commit_state error:", e, file=sys.stderr)


def persist(state, force=False):
    """Guarda el estado en disco y (en Actions) lo commitea, con un throttle de 30 s."""
    save_state(state)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    now = time.monotonic()
    if force or now - _last_commit[0] > 30:
        commit_state()
        _last_commit[0] = now


def new_user():
    return {"pid": None, "name": None, "confirmed": False, "muted": False,
            "asked": False, "stage": "awaiting_name", "candidates": [],
            "awaiting_compare": False,
            "msgs": 0, "first_seen": None, "last_active": None, "cmds": {}}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg(token, method, **payload):
    r = requests.post(TG_API.format(token=token, method=method), json=payload, timeout=30)
    data = r.json()
    if not data.get("ok"):
        print("ERROR Telegram {}: {}".format(method, data), file=sys.stderr)
    return data


def send(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg(token, "sendMessage", **payload).get("ok", False)


def get_updates(token, offset, timeout=0):
    r = requests.get(TG_API.format(token=token, method="getUpdates"),
                     params={"offset": offset, "timeout": timeout}, timeout=timeout + 25)
    data = r.json()
    return data.get("result", []) if data.get("ok") else []


# --------------------------------------------------------------------------- #
# Supabase (participantes y porras) + clasificación
# --------------------------------------------------------------------------- #
def fetch_porras():
    try:
        r = requests.get(
            SB_URL + "/rest/v1/porras",
            params={"select": "id,nombre,apellidos,active,gr,mvp,gol1,gol1n,gol2,gol2n,camp,sub,p3,p4",
                    "order": "created_at.asc"},
            headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY},
            timeout=30,
        )
        if r.status_code != 200:
            print("ERROR Supabase ({}): {}".format(r.status_code, r.text[:200]), file=sys.stderr)
            return []
        return r.json()
    except requests.RequestException as e:
        print("ERROR Supabase:", e, file=sys.stderr)
        return []


def display_name(nombre, apellidos):
    full = (nombre or "") + ((" " + apellidos) if apellidos else "")
    return re.sub(r"\b\w", lambda m: m.group().upper(), full, flags=re.UNICODE)


def group_results(matches):
    """Resultados de grupos terminados: clave 'A_0' -> '1'/'x'/'2' (local de la web)."""
    res = {}
    for m in matches:
        if (m.get("status") or "").lower() not in FINISHED_ST:
            continue
        home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
        gk = GROUP_KEYS.get(frozenset((home, away))) if home and away else None
        if not gk:
            continue
        key, hc, _ac = gk
        gh_api, ga_api = m.get("home_score"), m.get("away_score")
        if gh_api is None or ga_api is None:
            continue
        gh, ga = (gh_api, ga_api) if home == hc else (ga_api, gh_api)
        res[key] = "1" if gh > ga else ("x" if gh == ga else "2")
    return res


def ranking(porras, results):
    rows = []
    for p in porras:
        if not p.get("active"):
            continue
        gr = p.get("gr") or {}
        pts = 3 * sum(1 for k, real in results.items() if gr.get(k) == real)
        rows.append([p["id"], display_name(p.get("nombre"), p.get("apellidos")), pts])
    # Cláusula de la porra: Iván Gómez Peral queda por delante en caso de empate a puntos.
    rows.sort(key=lambda x: (-x[2], 0 if normalize(x[1]) == "ivangomezperal" else 1))
    return rows


def rank_line(i, name, pts, is_me):
    """Una fila de clasificación. Todas empiezan por el puesto (nombres alineados);
    el top 3 lleva medalla al final y va en negrita, igual que tu propia fila."""
    medal = {0: " 🥇", 1: " 🥈", 2: " 🥉"}.get(i, "")
    line = "{}º {} — {} pts{}".format(i + 1, name, pts, medal)
    return "<b>{}</b>".format(line) if (i < 3 or is_me) else line


def ranking_block(porras, results, my_pid):
    if my_pid is None:
        return ""
    rk = ranking(porras, results)
    idx = next((i for i, r in enumerate(rk) if r[0] == my_pid), None)
    if idx is None:
        return ""
    n = len(rk)
    start = max(0, idx - 2)
    end = min(n, start + 5)
    start = max(0, end - 5)
    lines = ["<b>CLASIFICACIÓN ACTUAL</b>"]
    for i in range(start, end):
        pid, name, pts = rk[i]
        lines.append(rank_line(i, name, pts, pid == my_pid))
    return "\n".join(lines) + "\n\nVer entera: /clasificacioncompleta"


# --------------------------------------------------------------------------- #
# Mensajes de partido
# --------------------------------------------------------------------------- #
def team_label(code, winner):
    return "<b>{}</b>".format(code) if code == winner else code


def pred_distribution(porras, home, away, exclude_pid=None):
    """(%local, %X, %visitante) de los pronósticos del resto de participantes activos."""
    activos = [p for p in porras if p.get("active") and p["id"] != exclude_pid]
    n = len(activos)
    if not n:
        return (0, 0, 0)
    h = a = 0
    for p in activos:
        pick = user_pick(p.get("gr"), home, away)
        if pick == home:
            h += 1
        elif pick == away:
            a += 1
    x = n - h - a
    return (round(100 * h / n), round(100 * x / n), round(100 * a / n))


def msg_comienzo(home, away, winner, dist=None):
    fh, fa = TEAMS[home][1], TEAMS[away][1]
    l1 = "Comienzo de {} {} - {} {}".format(fh, home, away, fa)
    if dist is None:
        return l1
    tu = fh if winner == home else (fa if winner == away else "X")
    pH, pX, pA = dist
    return ("{}\n\nTu predicción: {}\nLa del resto: {}% {} · {}% X · {}% {}"
            .format(l1, tu, pH, fh, pX, pA, fa))


def pct_aciertos(porras, home, away, real):
    """% de participantes activos que acertó el resultado (1/x/2) de este partido."""
    activos = [p for p in porras if p.get("active")]
    if not activos:
        return 0
    n = sum(1 for p in activos if user_pick(p.get("gr"), home, away) == real)
    return round(100 * n / len(activos))


PHRASES_ESP = [
    "🔥 ¡Qué grande La Roja! A por la siguiente",
    "🇪🇸 ¡VAMOS ESPAÑA! Imparables 🔥",
    "🥳 ¡Otra victoria de La Roja!",
    "💪 ¡A por todas, España!",
    "🇪🇸 ¡Esto huele a campeones!",
    "⚡ ¡Vamooos! La Roja no para",
    "🏆 ¡Camino al título!",
    "🔥 ¡A seguir soñando, España!",
    "🇪🇸 ¡La Roja sigue intratable!",
]


def msg_final(home, away, winner, gh, ga, acierto, ranking_txt="", pct=None, cheer=None):
    fh, fa = TEAMS[home][1], TEAMS[away][1]
    emoji = "✅" if acierto else "❌"
    base = "{} Final: {} {} {}-{} {} {}".format(
        emoji, fh, team_label(home, winner), gh, ga, team_label(away, winner), fa)
    if cheer:
        base += "\n" + cheer
    if pct is not None:
        base += "\nHa acertado el {}% de personas".format(pct)
    return base + ("\n\n━━━━━━━━━━━━━━━━\n\n" + ranking_txt if ranking_txt else "")


# --------------------------------------------------------------------------- #
# Chat: identificación de cada participante ("¿quién eres?")
# --------------------------------------------------------------------------- #
ASK_TEXT = ("\U0001f44b ¡Hola! Soy el bot de la porra del Mundial 2026.\n"
            "Para avisarte en cada partido y mostrarte tu posición, dime: "
            "<b>¿quién eres?</b>\nEscribe tu nombre y apellidos.")

RESET_CMDS = ("/start", "/reset", "/cambiar", "/quien")
RANK_CMDS = ("/clasificacion", "/clasi", "/ranking", "/posicion", "/puesto")
FULLRANK_CMDS = ("/clasificacioncompleta", "/completa", "/todos")
PORRA_CMDS = ("/miporra", "/porra", "/pronosticos")
MUTE_CMDS = ("/silencio", "/mute", "/pausa")
UNMUTE_CMDS = ("/avisos", "/activar", "/voz", "/reanudar")
HELP_CMDS = ("/ayuda", "/help", "/comandos")
COMPARE_CMDS = ("/compararprediccion", "/comparar", "/comparacion", "/compara")
ADMIN_CMDS = ("/usuarios", "/uso", "/stats")  # oculto: solo el admin
BROADCAST_CMDS = ("/aviso", "/anuncio", "/broadcast")  # oculto: solo el admin, manda a todos
WEB_CMDS = ("/web", "/pagina", "/porraweb")
WEB_URL = "https://cesaresteban.github.io/NFQ-WORLD-CUP/"

# Etiqueta canónica de cada comando, para el contador de uso.
CMD_LABEL = {}
for _grp, _lbl in [(RESET_CMDS, "start"), (RANK_CMDS, "clasificacion"),
                   (FULLRANK_CMDS, "clasificacioncompleta"), (PORRA_CMDS, "miporra"),
                   (COMPARE_CMDS, "comparar"), (MUTE_CMDS, "silencio"),
                   (UNMUTE_CMDS, "avisos"), (HELP_CMDS, "ayuda"),
                   (WEB_CMDS, "web"), (ADMIN_CMDS, "usuarios"),
                   (BROADCAST_CMDS, "aviso")]:
    for _c in _grp:
        CMD_LABEL[_c] = _lbl

AYUDA = ("<b>Comandos</b>\n"
         "/clasificacion — tu posición ahora mismo\n"
         "/clasificacioncompleta — la clasificación entera\n"
         "/miporra — tus pronósticos y próximos partidos\n"
         "/compararprediccion — comparar tus pronósticos con otro\n"
         "/silencio — pausar los avisos\n"
         "/avisos — reactivar los avisos\n"
         "/web — abrir la web de la porra\n"
         "/start — identificarte o cambiar de identidad\n"
         "/ayuda — esta ayuda")

# Lista para el menú de comandos de Telegram (setMyCommands).
BOT_COMMANDS = [
    {"command": "clasificacion", "description": "Tu posición en la clasificación"},
    {"command": "clasificacioncompleta", "description": "La clasificación entera (46)"},
    {"command": "miporra", "description": "Tus pronósticos y próximos partidos"},
    {"command": "compararprediccion", "description": "Comparar tus pronósticos con otro"},
    {"command": "silencio", "description": "Pausar los avisos"},
    {"command": "avisos", "description": "Reactivar los avisos"},
    {"command": "web", "description": "Abrir la web de la porra"},
    {"command": "start", "description": "Identificarte / cambiar de identidad"},
    {"command": "ayuda", "description": "Ver los comandos"},
]


def cmd_ranking(token, cid, pid, porras):
    results = group_results(fetch_matches())
    blk = ranking_block(porras, results, pid)
    send(token, cid, blk or "Aún no estás en la clasificación (o no hay resultados todavía).")


def cmd_ranking_full(token, cid, pid, porras):
    rk = ranking(porras, group_results(fetch_matches()))
    if not rk:
        send(token, cid, "Aún no hay clasificación.")
        return
    lines = ["<b>CLASIFICACIÓN COMPLETA</b>", "━━━━━━━━━━━━━━━━"]
    for i, (p_id, name, pts) in enumerate(rk):
        lines.append(rank_line(i, name, pts, p_id == pid))
    send(token, cid, "\n".join(lines))


def normalize_player(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(s.replace(".", " ").replace("-", " ").split())


def espn_get(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    return r.json()


def refresh_scorers(state):
    """Suma los goles por jugador desde los partidos del Mundial (ESPN). Incremental."""
    sc = state.setdefault("scorers", {"events": [], "goals": {}, "ts": None})
    done = set(sc["events"])
    try:
        sb = espn_get(ESPN_SB)
    except Exception as e:
        print("ESPN scoreboard error:", e, file=sys.stderr)
        return 0
    nuevos = 0
    for e in sb.get("events", []):
        if not (e.get("status") or {}).get("type", {}).get("completed"):
            continue
        eid = str(e.get("id"))
        if eid in done:
            continue
        try:
            s = espn_get(ESPN_SUMMARY.format(eid))
            for k in s.get("keyEvents", []):
                tt = (k.get("type") or {}).get("type", "")
                if tt.startswith("goal") or tt == "penalty---scored":
                    parts = k.get("participants") or []
                    nm = parts[0].get("displayName") if parts else None
                    if not nm and parts:
                        nm = (parts[0].get("athlete") or {}).get("displayName")
                    if nm:
                        key = normalize_player(nm)
                        sc["goals"][key] = sc["goals"].get(key, 0) + 1
            sc["events"].append(eid)
            done.add(eid)
            nuevos += 1
        except Exception as e2:
            print("ESPN summary error:", e2, file=sys.stderr)
    sc["ts"] = now_iso()
    return nuevos


def player_goals(goals_map, name):
    """Goles reales de un jugador (0 si no ha marcado). None si no hay datos cargados."""
    if goals_map is None or not name:
        return None
    n = normalize_player(name)
    if n in goals_map:
        return goals_map[n]
    ln = n.split()[-1] if n.split() else n
    for k, g in goals_map.items():
        ks = k.split()
        if ks and ks[-1] == ln:
            return g
    return 0


def _gol_line(etq, name, predicted, goals_map):
    g = player_goals(goals_map, name)
    if g is not None and predicted:
        suf = " ({}/{})".format(g, predicted)
    elif g is not None:
        suf = " ({})".format(g)
    elif predicted:
        suf = " ({})".format(predicted)
    else:
        suf = ""
    return "{}: {}{}".format(etq, name, suf)


def cmd_miporra(token, cid, p, goals_map=None):
    if not p:
        send(token, cid, "No encuentro tu porra.")
        return
    lines = ["<b>Tu porra</b>"]
    if p.get("mvp"):
        lines.append("🏆 MVP: " + p["mvp"])
    if p.get("gol1"):
        lines.append(_gol_line("⚽ Goleador del torneo", p["gol1"], p.get("gol1n"), goals_map))
    if p.get("gol2"):
        lines.append(_gol_line("🇪🇸 Goleador de España", p["gol2"], p.get("gol2n"), goals_map))
    for campo, etq in (("camp", "🥇 Campeón"), ("sub", "🥈 Subcampeón"),
                       ("p3", "🥉 Tercero"), ("p4", "4º")):
        if p.get(campo):
            lines.append("{}: {}".format(etq, p[campo]))

    nxt = next_matches(3)
    if nxt:
        gr = p.get("gr") or {}
        lines.append("\n📅 <b>Próximos partidos</b> (tu pronóstico):")
        for k, h, a in nxt:
            pick = user_pick(gr, h, a)
            when = k.astimezone(ESP_TZ).strftime("%d/%m %H:%M")
            lines.append("{} · {} {} - {} {} → <b>{}</b>".format(
                when, TEAMS[h][1], h, a, TEAMS[a][1], pick or "empate"))
    send(token, cid, "\n".join(lines))


def next_matches(n=3):
    """Los n próximos partidos de grupos sin empezar, ordenados por hora."""
    up = []
    for m in fetch_matches():
        h, a = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if not (h and a and frozenset((h, a)) in GROUP_KEYS):
            continue
        st = (m.get("status") or "").lower()
        k = parse_dt(m.get("event_date"))
        if st in LIVE_ST or st in FINISHED_ST or k is None:
            continue
        up.append((k, h, a))
    up.sort(key=lambda x: x[0])
    return up[:n]


def compare_keyboard(candidates):
    rows = [[{"text": display_name(p.get("nombre"), p.get("apellidos")),
              "callback_data": "cmp:{}".format(p["id"])}] for p in candidates]
    return {"inline_keyboard": rows}


def do_compare(token, cid, my_pid, text, porras, user):
    """Resuelve el nombre escrito y muestra (o pide elegir) con quién comparar."""
    cands = [p for p in find_candidates(text, porras) if p["id"] != my_pid]
    if not cands:
        user["awaiting_compare"] = True
        send(token, cid, "No encuentro a nadie con ese nombre 🤔. Escríbelo otra vez "
                         "(nombre y apellidos).")
    elif len(cands) > 4:
        user["awaiting_compare"] = True
        send(token, cid, "Hay varios con ese nombre. Añade el apellido, por favor.")
    elif len(cands) == 1:
        do_compare_pid(token, cid, my_pid, cands[0]["id"], porras)
    else:
        send(token, cid, "¿Con cuál quieres comparar?", reply_markup=compare_keyboard(cands))


def do_compare_pid(token, cid, my_pid, other_pid, porras):
    by_id = {p["id"]: p for p in porras}
    me, other = by_id.get(my_pid), by_id.get(other_pid)
    if not me or not other:
        send(token, cid, "No he podido cargar esa porra.")
        return
    other_name = display_name(other.get("nombre"), other.get("apellidos"))
    my_gr, their_gr = me.get("gr") or {}, other.get("gr") or {}
    lines = ["<b>TU PREDICCIÓN vs {}</b>".format(other_name.upper()),
             "━━━━━━━━━━━━━━━━"]
    for k, h, a in next_matches(10):
        mp = user_pick(my_gr, h, a) or "X"
        tp = user_pick(their_gr, h, a) or "X"
        row = "{} {}-{} {} → {} / {}".format(TEAMS[h][1], h, a, TEAMS[a][1], mp, tp)
        lines.append("<b>{}</b>".format(row) if mp != tp else row)
    lines.append("\n<i>tú / {}  ·  en negrita, donde discrepáis</i>".format(other_name))
    send(token, cid, "\n".join(lines))


def normalize_words(s):
    return [normalize(w) for w in (s or "").split() if normalize(w)]


def find_candidates(text, porras):
    q = " ".join(normalize_words(text))
    parts = q.split()
    scored = []
    for p in porras:
        if not p.get("active"):
            continue
        full = " ".join(normalize_words(display_name(p.get("nombre"), p.get("apellidos"))))
        if not full:
            continue
        if q and q == full:
            scored.append((0, p))
        elif q and q in full:
            scored.append((1, p))
        elif parts and all(tok in full for tok in parts):
            scored.append((2, p))
        elif parts and full.split() and full.split()[0].startswith(parts[0]):
            scored.append((3, p))
    if not scored:
        return []
    best = min(s[0] for s in scored)
    return [p for s, p in scored if s == best]


def confirm_keyboard(candidates):
    rows = [[{"text": display_name(p.get("nombre"), p.get("apellidos")),
              "callback_data": "id:{}".format(p["id"])}] for p in candidates]
    rows.append([{"text": "Otro", "callback_data": "otro"}])
    return {"inline_keyboard": rows}


def ask(token, chat_id, u):
    u["asked"] = True
    u["stage"] = "awaiting_name"
    u["candidates"] = []
    send(token, chat_id, ASK_TEXT)


def _esp(iso, with_time=False):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso).astimezone(ESP_TZ)
    except Exception:
        return "—"
    return d.strftime("%d/%m %H:%M") if with_time else d.strftime("%d/%m")


def cmd_usuarios(token, cid, state):
    """Resumen de uso (solo admin): quién, cuánta actividad y qué comandos usan."""
    us = [u for u in state["users"].values() if u.get("confirmed")]
    total = sum(u.get("msgs", 0) for u in us)
    hoy = datetime.now(ESP_TZ).strftime("%d/%m")
    activos = sum(1 for u in us if _esp(u.get("last_active")) == hoy)
    lines = ["<b>👥 USUARIOS ({})</b>".format(len(us)),
             "Mensajes: {} · activos hoy: {}".format(total, activos),
             "━━━━━━━━━━━━━━━━"]
    for u in sorted(us, key=lambda x: -x.get("msgs", 0)):
        lines.append("{} · alta {} · {} msg · últ {}".format(
            u.get("name"), _esp(u.get("first_seen")), u.get("msgs", 0),
            _esp(u.get("last_active"), True)))
    agg = {}
    for u in us:
        for k, v in (u.get("cmds") or {}).items():
            agg[k] = agg.get(k, 0) + v
    if agg:
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("Comandos: " + " · ".join(
            "/{} {}".format(k, v) for k, v in sorted(agg.items(), key=lambda x: -x[1])))
    send(token, cid, "\n".join(lines))


def track(u, cmd):
    """Apunta actividad de uso del usuario."""
    u["msgs"] = u.get("msgs", 0) + 1
    ts = now_iso()
    if not u.get("first_seen"):
        u["first_seen"] = ts
    u["last_active"] = ts
    lbl = CMD_LABEL.get(cmd)
    if lbl:
        c = u.setdefault("cmds", {})
        c[lbl] = c.get(lbl, 0) + 1


def process_chat(token, porras, state, updates):
    """Atiende los mensajes entrantes de todos los chats e identifica a cada participante."""
    if not porras or not updates:
        return
    by_id = {p["id"]: p for p in porras}
    users = state["users"]

    for upd in updates:
        state["tg_offset"] = max(state["tg_offset"], upd["update_id"] + 1)

        cq = upd.get("callback_query")
        msg = upd.get("message")
        if cq:
            chat = (cq.get("message") or {}).get("chat") or {}
        elif msg:
            chat = msg.get("chat") or {}
        else:
            continue
        if chat.get("type") != "private":
            continue
        cid = str(chat.get("id"))
        u = users.setdefault(cid, new_user())

        # ── Pulsación de botón ──
        if cq:
            tg(token, "answerCallbackQuery", callback_query_id=cq["id"])
            track(u, "")
            data = cq.get("data", "")
            if data.startswith("cmp:"):
                if u.get("confirmed"):
                    do_compare_pid(token, cid, u["pid"], int(data[4:]), porras)
                continue
            if u.get("confirmed") or u.get("stage") != "awaiting_confirm":
                continue
            if data == "otro":
                u["stage"] = "awaiting_name"
                send(token, cid, "Vale. Escribe otra vez tu nombre (nombre y apellidos).")
            elif data.startswith("id:"):
                p = by_id.get(int(data[3:]))
                if p:
                    u.update(confirmed=True, pid=p["id"], stage="done", candidates=[],
                             name=display_name(p.get("nombre"), p.get("apellidos")))
                    send(token, cid,
                         "✅ Hecho, te he identificado como <b>{}</b>.\n"
                         "Te avisaré al comienzo de cada partido y, al acabar, te pondré "
                         "tu zona de la clasificación.\n\nClick en /ayuda para ver los comandos.".format(u["name"]))
            continue

        # ── Mensaje de texto ──
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        cmd = low.split()[0].split("@")[0]
        track(u, cmd)
        # /usuarios: oculto, solo responde al admin (a los demás les cae como desconocido).
        if cmd in ADMIN_CMDS and cid == ADMIN_CHAT_ID:
            cmd_usuarios(token, cid, state)
            continue
        # /aviso: oculto, solo el admin; manda el texto a TODOS los identificados.
        if cmd in BROADCAST_CMDS and cid == ADMIN_CHAT_ID:
            parts = text.split(None, 1)
            anuncio = parts[1].strip() if len(parts) > 1 else ""
            if not anuncio:
                send(token, cid, "Uso: /aviso TU MENSAJE")
            else:
                esc = anuncio.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                n = 0
                for ucid, uu in state["users"].items():
                    if uu.get("confirmed") and send(token, ucid, "📢 " + esc):
                        n += 1
                send(token, cid, "📢 Enviado a {} personas.".format(n))
            continue
        # /ayuda y /web funcionan siempre, estés identificado o no.
        if cmd in HELP_CMDS:
            send(token, cid, AYUDA)
            continue
        if cmd in WEB_CMDS:
            send(token, cid, "🌐 Web de la porra:\n" + WEB_URL,
                 reply_markup={"inline_keyboard": [[{"text": "Abrir la web", "url": WEB_URL}]]})
            continue
        if u.get("confirmed"):
            if u.get("awaiting_compare") and not text.startswith("/"):
                u["awaiting_compare"] = False
                do_compare(token, cid, u["pid"], text, porras, u)
                continue
            u["awaiting_compare"] = False  # cualquier comando cancela el modo comparar
            if cmd in RESET_CMDS:
                u.update(new_user())
                ask(token, cid, u)
            elif cmd in COMPARE_CMDS:
                u["awaiting_compare"] = True
                send(token, cid, "¿Con quién quieres comparar? Escríbeme su nombre.")
            elif cmd in RANK_CMDS:
                cmd_ranking(token, cid, u["pid"], porras)
            elif cmd in FULLRANK_CMDS:
                cmd_ranking_full(token, cid, u["pid"], porras)
            elif cmd in PORRA_CMDS:
                cmd_miporra(token, cid, by_id.get(u["pid"]) or {},
                            (state.get("scorers") or {}).get("goals"))
            elif cmd in MUTE_CMDS:
                u["muted"] = True
                send(token, cid, "🔕 Avisos en pausa. Reactívalos con /avisos.")
            elif cmd in UNMUTE_CMDS:
                u["muted"] = False
                send(token, cid, "🔔 Avisos reactivados.")
            elif text.startswith("/"):
                send(token, cid, "No conozco ese comando.\n\n" + AYUDA)
            continue
        if cmd in RESET_CMDS or text.startswith("/") or not u.get("asked"):
            ask(token, cid, u)
            continue
        cands = find_candidates(text, porras)
        if not cands:
            send(token, cid, "No te encuentro en la porra \U0001f914. Escríbelo como "
                             "aparece (nombre y apellidos).")
        elif len(cands) > 4:
            send(token, cid, "Hay varios con ese nombre. Añade el apellido, por favor.")
        else:
            u["stage"] = "awaiting_confirm"
            u["candidates"] = [p["id"] for p in cands]
            txt = "Pulsa tu nombre para confirmar:"
            send(token, cid, txt, reply_markup=confirm_keyboard(cands))


# --------------------------------------------------------------------------- #
# Partidos
# --------------------------------------------------------------------------- #
def fetch_matches():
    r = requests.get(SPORTS_URL, headers={"Authorization": "Token " + SPORTS_KEY}, timeout=30)
    if r.status_code != 200:
        print("ERROR API partidos ({}): {}".format(r.status_code, r.text[:200]), file=sys.stderr)
        sys.exit(1)
    return r.json().get("results", [])


def parse_dt(s):
    return datetime.fromisoformat(s) if s else None


def confirmed_users(state, porras_by_pid):
    """[(chat_id, user)] de los usuarios identificados con porra conocida."""
    out = []
    for cid, u in state["users"].items():
        if u.get("confirmed") and not u.get("muted") and u.get("pid") in porras_by_pid:
            out.append((cid, u))
    return out


def check_matches(token, porras, state):
    """Mira los partidos y envía comienzos/finales nuevos a todos los identificados."""
    porras_by_pid = {p["id"]: p for p in porras}
    now = datetime.now(timezone.utc)
    matches = fetch_matches()
    results = group_results(matches)

    relevant = []
    for m in matches:
        home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if home and away and frozenset((home, away)) in GROUP_KEYS:
            relevant.append((m, home, away))

    # Primera ejecución: marcar lo ya jugado como avisado (sin spamear el historial).
    if not state["seeded"]:
        for m, home, away in relevant:
            st = (m.get("status") or "").lower()
            if st in LIVE_ST or st in FINISHED_ST:
                state["comienzo"].append(m["id"])
            if st in FINISHED_ST:
                state["final"].append(m["id"])
        state["seeded"] = True
        persist(state, force=True)
        print("Estado inicializado: {} comienzos y {} finales ya marcados.".format(
            len(state["comienzo"]), len(state["final"])))
        return 0

    comienzo_set = set(state["comienzo"])
    final_set = set(state["final"])
    users = confirmed_users(state, porras_by_pid)
    enviados = 0

    for m, home, away in relevant:
        mid = m["id"]
        st = (m.get("status") or "").lower()
        kickoff = parse_dt(m.get("event_date"))
        ya_empezado = st in LIVE_ST or st in FINISHED_ST
        toca_comienzo = ya_empezado or (kickoff is not None and now >= kickoff)
        if kickoff is not None and (now - kickoff).total_seconds() > COMIENZO_MAX_RETRASO_H * 3600:
            demasiado_tarde = st not in LIVE_ST
        else:
            demasiado_tarde = False

        # ── Comienzo: a cada usuario, su pronóstico ──
        if mid not in comienzo_set and toca_comienzo and not demasiado_tarde:
            for cid, u in users:
                pick = user_pick(porras_by_pid[u["pid"]].get("gr"), home, away)
                dist = pred_distribution(porras, home, away, u["pid"])
                if send(token, cid, msg_comienzo(home, away, pick, dist)):
                    enviados += 1
            comienzo_set.add(mid)
            state["comienzo"].append(mid)
            persist(state, force=True)

        # ── Final: a cada usuario, resultado + acierto + su clasificación ──
        if st in FINISHED_ST and mid not in final_set:
            gh, ga = m.get("home_score"), m.get("away_score")
            if gh is None or ga is None:
                continue
            real = home if gh > ga else (away if ga > gh else None)
            pct = pct_aciertos(porras, home, away, real)
            cheer = PHRASES_ESP[int(mid) % len(PHRASES_ESP)] if real == "ESP" else None
            for cid, u in users:
                pick = user_pick(porras_by_pid[u["pid"]].get("gr"), home, away)
                rk = ranking_block(porras, results, u["pid"])
                if send(token, cid, msg_final(home, away, pick, gh, ga, real == pick, rk, pct, cheer)):
                    enviados += 1
            final_set.add(mid)
            state["final"].append(mid)
            persist(state, force=True)

    return enviados


def main():
    """Una sola pasada (para pruebas o ejecución manual)."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("Falta TELEGRAM_TOKEN.", file=sys.stderr)
        sys.exit(2)
    state = load_state()
    porras = fetch_porras()
    process_chat(token, porras, state, get_updates(token, state["tg_offset"]))
    enviados = check_matches(token, porras, state)
    persist(state, force=True)
    print("Hecho. Usuarios: {}. Mensajes enviados: {}.".format(len(state["users"]), enviados))


def run_loop():
    """Modo 'siempre encendido': escucha el chat en continuo (long-polling) y revisa
    los partidos cada 2 min. Corre ~5h30 y luego el workflow lanza el relevo."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("Falta TELEGRAM_TOKEN.", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    porras = fetch_porras()
    porras_ts = time.monotonic()
    last_match = 0.0
    last_scorers = 0.0
    start = time.monotonic()
    # El proceso vive hasta ~5h50 (cerca del límite de 6h de un job). Así, si el cron de
    # relevo se salta una vez, el bot sigue escuchando en vez de caerse.
    MAX_RUN = int(os.environ.get("LOOP_MINUTES", "350")) * 60
    print("Bot escuchando (long-polling). Usuarios: {}.".format(len(state["users"])))

    while time.monotonic() - start < MAX_RUN:
        try:
            if not porras or time.monotonic() - porras_ts > 600:
                p = fetch_porras()
                if p:
                    porras, porras_ts = p, time.monotonic()

            updates = get_updates(token, state["tg_offset"], timeout=30)
            if updates:
                process_chat(token, porras, state, updates)
                persist(state)

            if time.monotonic() - last_match > 120:
                check_matches(token, porras, state)
                last_match = time.monotonic()

            if time.monotonic() - last_scorers > 1200:  # goleadores cada 20 min (incremental)
                if refresh_scorers(state):
                    persist(state, force=True)
                last_scorers = time.monotonic()
        except Exception as e:  # un fallo puntual de red no debe tumbar el bucle
            print("loop error:", e, file=sys.stderr)
            time.sleep(5)

    persist(state, force=True)
    print("Fin de ciclo: relevo.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        main()
