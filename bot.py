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

HERE = os.path.dirname(os.path.abspath(__file__))
TG_API = "https://api.telegram.org/bot{token}/{method}"
SB_URL = "https://zavqpnsbsmivsuvwurkd.supabase.co"
SB_KEY = "sb_publishable_gzIrT1J8xG5ZGTIxJHFzfg_BAO-XGPm"
# Misma API de partidos que usa la web de la porra (resultados consistentes con el ranking).
SPORTS_URL = "https://sports.bzzoiro.com/api/v2/events/?league_id=27&season_id=188&limit=200"
SPORTS_KEY = os.environ.get("SPORTS_TOKEN", "")  # va por secret/env, nunca hardcodeado
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
    st.setdefault("seeded_ko", False)
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
            "awaiting_compare": False, "focus_match": None, "last_compared": None,
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
            params={"select": "id,nombre,apellidos,active,gr,ko,mvp,gol1,gol1n,gol2,gol2n,camp,sub,p3,p4",
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


def web_points(porras, matches):
    """Clasificación = el MOTOR REAL de la web de César (su `calcPuntos`), ejecutado en Node
    sobre los mismos datos (porras de Supabase + resultados de la API). El bot NO calcula la
    puntuación: descarga el JS de la web y lee lo que produce. Devuelve {pid: total}.
    Lanza RuntimeError si la web no está accesible o su motor falla (el loop reintenta)."""
    payload = json.dumps({"porras": porras, "fixtures": matches})
    try:
        r = subprocess.run(["node", os.path.join(HERE, "web_engine.js")],
                           input=payload, capture_output=True, text=True,
                           timeout=90, encoding="utf-8")
    except Exception as e:
        raise RuntimeError("No pude lanzar el motor de la web: {}".format(e))
    if r.returncode != 0:
        raise RuntimeError("Motor de la web falló ({}): {}".format(
            r.returncode, (r.stderr or "").strip()[:300]))
    try:
        data = json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError("Salida del motor de la web ilegible: {}".format(e))
    return {row["id"]: row.get("total", 0) for row in data}


def ranking(porras, points_by_id):
    """Filas [pid, nombre, puntos] ordenadas, con los puntos que da la web. El bot solo ordena
    (misma cláusula de desempate de Iván que aplica la web en su renderRanking)."""
    rows = [[p["id"], display_name(p.get("nombre"), p.get("apellidos")),
             points_by_id.get(p["id"], 0)]
            for p in porras if p.get("active")]
    rows.sort(key=lambda x: (-x[2], 0 if normalize(x[1]) == "ivangomezperal" else 1))
    return rows


def rank_line(i, name, pts, is_me):
    """Una fila de clasificación. Todas empiezan por el puesto (nombres alineados);
    el top 3 lleva medalla al final y va en negrita, igual que tu propia fila."""
    medal = {0: " 🥇", 1: " 🥈", 2: " 🥉"}.get(i, "")
    line = "{}º - {} · {}{}".format(i + 1, pts, name, medal)
    if is_me:
        return "<b>{}</b> 👈".format(line)
    return "<b>{}</b>".format(line) if i < 3 else line


def ranking_block(porras, points_by_id, my_pid):
    if my_pid is None or not points_by_id:   # sin puntos (web caída) -> sin bloque
        return ""
    rk = ranking(porras, points_by_id)
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
    return "\n".join(lines) + "\n\nVer entera: /clasificacion"


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
PREDRESTO_CMDS = ("/prediccionesdelresto", "/predicciones", "/resto")
VERSUPORRA_CMDS = ("/versuporra", "/suporra")
PROBGANAR_CMDS = ("/probabilidadganar", "/probabilidades", "/prob")
PORQUE_CMDS = ("/porque", "/porqué")
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
                   (COMPARE_CMDS, "comparar"), (PREDRESTO_CMDS, "prediccionesdelresto"),
                   (VERSUPORRA_CMDS, "versuporra"), (PROBGANAR_CMDS, "probabilidadganar"),
                   (PORQUE_CMDS, "porque"),
                   (MUTE_CMDS, "silencio"),
                   (UNMUTE_CMDS, "avisos"), (HELP_CMDS, "ayuda"),
                   (WEB_CMDS, "web"), (ADMIN_CMDS, "usuarios"),
                   (BROADCAST_CMDS, "aviso")]:
    for _c in _grp:
        CMD_LABEL[_c] = _lbl

AYUDA = ("<b>Comandos</b>\n"
         "/clasificacion — la clasificación entera\n"
         "/miporra — tus pronósticos y próximos partidos\n"
         "/compararprediccion — comparar tus pronósticos con otro\n"
         "/prediccionesdelresto — las predicciones de todos para un partido\n"
         "/probabilidadganar — clasificación estimada (12.000 simulaciones)\n"
         "/silencio — pausar los avisos\n"
         "/avisos — reactivar los avisos\n"
         "/web — abrir la web de la porra\n"
         "/start — identificarte o cambiar de identidad\n"
         "/ayuda — esta ayuda")

# Lista para el menú de comandos de Telegram (setMyCommands).
BOT_COMMANDS = [
    {"command": "clasificacion", "description": "La clasificación entera (46)"},
    {"command": "miporra", "description": "Tus pronósticos y próximos partidos"},
    {"command": "compararprediccion", "description": "Comparar tus pronósticos con otro"},
    {"command": "prediccionesdelresto", "description": "Las predicciones de todos para un partido"},
    {"command": "probabilidadganar", "description": "Clasificación estimada (simulación)"},
    {"command": "silencio", "description": "Pausar los avisos"},
    {"command": "avisos", "description": "Reactivar los avisos"},
    {"command": "web", "description": "Abrir la web de la porra"},
    {"command": "start", "description": "Identificarte / cambiar de identidad"},
    {"command": "ayuda", "description": "Ver los comandos"},
]


RANKING_NO_DISP = ("⚠️ La clasificación no está disponible ahora mismo (la web de la porra "
                   "está dando un error). Inténtalo en un rato.")


def cmd_ranking_full(token, cid, pid, porras):
    matches = fetch_matches()
    try:
        pts = web_points(porras, matches)
    except RuntimeError:
        send(token, cid, RANKING_NO_DISP)
        return
    rk = ranking(porras, pts)
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


def cmd_miporra(token, cid, p, goals_map=None, porras=None, title="Tu porra"):
    if not p:
        send(token, cid, "No encuentro esa porra.")
        return
    lines = ["<b>{}</b>".format(title)]
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

    try:
        ko_list = ko_results_list(fetch_matches())
    except Exception:
        ko_list = None
    brk = bracket_block(p.get("ko"), ko_list)
    if brk:
        lines.append("\n🗺️ <b>Tu bracket</b> (tu pronóstico):")
        lines.extend(brk)
        if ko_list:
            lines.append("\n❌ nada\n🎯 marcador exacto pero no quién pasa"
                         "\n☑️ ganador\n✅ ganador y marcador exacto"
                         "\n👑 ganador, marcador y rival exacto")
    send(token, cid, "\n".join(lines))


def next_matches(n=3, porras=None):
    """Los n próximos partidos sin empezar (grupos o KO): (hora, home, away, slot|None).
    n=None devuelve todos los próximos con equipos ya conocidos."""
    matches = fetch_matches()
    cidof = ko_cid_map(matches, porras)
    now = datetime.now(timezone.utc)
    up = []
    for m in matches:
        h, a = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if not (h and a):
            continue
        slot = cidof.get(m.get("id"))
        if not (slot or frozenset((h, a)) in GROUP_KEYS):
            continue
        st = (m.get("status") or "").lower()
        k = parse_dt(m.get("event_date"))
        if st in LIVE_ST or st in FINISHED_ST or k is None or k < now:
            continue
        up.append((k, h, a, slot))
    up.sort(key=lambda x: x[0])
    return up if n is None else up[:n]


def compare_keyboard(candidates):
    rows = [[{"text": display_name(p.get("nombre"), p.get("apellidos")),
              "callback_data": "cmp:{}".format(p["id"])}] for p in candidates]
    return {"inline_keyboard": rows}


def compare_mode_keyboard():
    """Los dos modos de /comparar: con otra persona o con toda la porra."""
    return {"inline_keyboard": [
        [{"text": "👥 Con otra persona", "callback_data": "cmpmode:otros"}],
        [{"text": "🌍 Con toda la porra", "callback_data": "cmpmode:todos"}],
    ]}


def _upcoming_matches(porras, n):
    """Los n próximos partidos sin empezar: (hora, match, home, away, pfx|None, slot|None)."""
    matches = fetch_matches()
    cidof = ko_cid_map(matches, porras)
    now = datetime.now(timezone.utc)
    out = []
    for m in matches:
        h, a = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if not (h and a):
            continue
        pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
        if not (pfx or frozenset((h, a)) in GROUP_KEYS):
            continue
        st = (m.get("status") or "").lower()
        k = parse_dt(m.get("event_date"))
        if st in LIVE_ST or st in FINISHED_ST or k is None or k < now:
            continue
        out.append((k, m, h, a, pfx, cidof.get(m.get("id")) if pfx == "c" else None))
    out.sort(key=lambda x: x[0])
    return out[:n]


def match_stats_keyboard(porras):
    """Menú con los próximos 10 partidos para pedir sus estadísticas (callback mstat:<id>)."""
    rows = []
    for k, m, h, a, pfx, slot in _upcoming_matches(porras, 10):
        when = k.astimezone(ESP_TZ).strftime("%d/%m %H:%M")
        rows.append([{"text": "{} {} - {} {} · {}".format(TEAMS[h][1], h, a, TEAMS[a][1], when),
                      "callback_data": "mstat:{}".format(m["id"])}])
    return {"inline_keyboard": rows} if rows else None


def match_stats_block(porras, m, home, away, pfx, slot, k, me, points_by_id=None):
    """Estadísticas de la porra para UN partido próximo: tu predicción, reparto, marcadores
    más repetidos (16avos) y pronóstico del líder."""
    active = [p for p in porras if p.get("active")]
    lbl = ROUND_LBL.get(pfx, "Grupos")
    when = " · " + k.astimezone(ESP_TZ).strftime("%d/%m %H:%M") if k else ""
    L = ["<b>{} {} - {} {}{} · {}</b>".format(TEAMS[home][1], home, away, TEAMS[away][1], when, lbl)]
    # Tu predicción
    me_ko, me_gr = (me or {}).get("ko") or {}, (me or {}).get("gr") or {}
    mine = None
    if pfx == "c" and slot and me_ko.get(slot, {}).get("winner"):
        pr = me_ko[slot]
        fh, ch = _ko_flag(pr.get("homeTeam"))
        fa, ca = _ko_flag(pr.get("awayTeam"))
        mine = "{} {} {}".format(fh, _ko_own_score(pr), fa)
    elif pfx and pfx != "c":
        hp, ap = ko_user_pick(me_ko, pfx, home), ko_user_pick(me_ko, pfx, away)
        if hp and ap:
            mine = "pasan ambos"
        elif hp:
            mine = "pasa {} {}".format(TEAMS[home][1], home)
        elif ap:
            mine = "pasa {} {}".format(TEAMS[away][1], away)
    elif not pfx:
        w = user_pick(me_gr, home, away)
        if w:
            mine = "gana {} {}".format(*((TEAMS[home][1], home) if w == home else (TEAMS[away][1], away)))
    if mine:
        L.append("\n<b>Tu predicción:</b>")
        L.append(mine)
    # Reparto: a quién da la porra como clasificado / ganador
    nh = na = 0
    for p in active:
        ko, gr = p.get("ko") or {}, p.get("gr") or {}
        if pfx == "c":
            pr = ko.get(slot) if slot else None
            w = match_team(pr.get("winner")) if pr and pr.get("winner") else None
        elif pfx:
            w = home if ko_user_pick(ko, pfx, home) else (away if ko_user_pick(ko, pfx, away) else None)
        else:
            w = user_pick(gr, home, away)
        if w == home:
            nh += 1
        elif w == away:
            na += 1
    if nh + na:
        L.append("\n<b>Predicción de la porra:</b>")
        L.append("{} {}% ({}) · {} {}% ({})".format(
            TEAMS[home][1], round(100 * nh / (nh + na)), nh,
            TEAMS[away][1], round(100 * na / (nh + na)), na))
    # Marcadores más repetidos (solo 16avos)
    if pfx == "c" and slot:
        counts = {}
        for p in active:
            pr = (p.get("ko") or {}).get(slot)
            if pr and pr.get("scoreH") is not None and pr.get("scoreA") is not None:
                kk = (pr["scoreH"], pr["scoreA"]) if match_team(pr.get("homeTeam")) == home \
                    else (pr["scoreA"], pr["scoreH"])
                counts[kk] = counts.get(kk, 0) + 1
        if counts:
            L.append("\n<b>Marcadores más comunes</b>")
            for (sh, sa), c in sorted(counts.items(), key=lambda x: -x[1])[:3]:
                L.append("{} {}-{} {} → {}".format(TEAMS[home][1], sh, sa, TEAMS[away][1], c))
    # Pronóstico del líder
    if points_by_id:
        rk = ranking(porras, points_by_id)
        if rk:
            lid_id, lid_name, _ = rk[0]
            lko = next((p.get("ko") or {} for p in porras if p["id"] == lid_id), {})
            first = (lid_name or "Líder").split()[0]
            lead = None
            if pfx == "c" and slot and lko.get(slot, {}).get("winner"):
                pr = lko[slot]
                fh, ch = _ko_flag(pr.get("homeTeam"))
                fa, ca = _ko_flag(pr.get("awayTeam"))
                lead = "{} {} {}".format(fh, _ko_own_score(pr), fa)
            elif pfx and pfx != "c":
                if ko_user_pick(lko, pfx, home):
                    lead = "pasa {} {}".format(TEAMS[home][1], home)
                elif ko_user_pick(lko, pfx, away):
                    lead = "pasa {} {}".format(TEAMS[away][1], away)
            if lead:
                L.append("\n<b>Predicción del Líder ({}):</b>".format(first))
                L.append(lead)
    L.append("\nPredicciones del resto: /prediccionesdelresto")
    return "\n".join(L)


def cmd_match_stats(token, cid, match_id, porras, me):
    """Muestra las estadísticas de la porra para un partido próximo (botón del menú)."""
    matches = fetch_matches()
    m = next((x for x in matches if x.get("id") == match_id), None)
    if not m:
        send(token, cid, "Ese partido ya no está disponible.")
        return
    home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
    pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
    slot = ko_cid_map(matches, porras).get(match_id) if pfx == "c" else None
    try:
        pts = web_points(porras, matches)
    except Exception:
        pts = None
    send(token, cid, match_stats_block(porras, m, home, away, pfx, slot,
                                       parse_dt(m.get("event_date")), me, pts))


PRED_LEGEND = "❌ nada · 🎯 marcador · ☑️ ganador · ✅ ganador y marcador · 👑 todo"
_EMOJI_RANK = {"👑": 0, "✅": 1, "🎯": 2, "☑️": 3, "❌": 4, "⏳": 5}


def predicciones_resto(porras, home, away, pfx, slot, played, ko_list=None):
    """Una fila por participante con su predicción para el partido. Si 'played', cada fila
    lleva el emoji de su resultado y debajo la leyenda; si no, van ordenadas por marcador."""
    active = [p for p in porras if p.get("active")]
    ents = []
    for p in active:
        name = display_name(p.get("nombre"), p.get("apellidos"))
        ko = p.get("ko") or {}
        if pfx == "c":
            pr = ko.get(slot) if slot else None
            if not pr or not pr.get("winner"):
                continue
            score = _ko_pred_score(pr, home, away)
            sh, sa = (pr.get("scoreH"), pr.get("scoreA")) if match_team(pr.get("homeTeam")) == home \
                else (pr.get("scoreA"), pr.get("scoreH"))
            if played and ko_list is not None:
                emoji = ko_pick_eval("c", pr, ko_list)[0]
                ents.append(((_EMOJI_RANK.get(emoji, 9), name), "{} {} · {}".format(score, emoji, name)))
            else:
                ents.append(((_int(sh) if _int(sh) is not None else 99,
                              _int(sa) if _int(sa) is not None else 99, name),
                             "{} · {}".format(score, name)))
        elif pfx:
            pick_home, pick_away = ko_user_pick(ko, pfx, home), ko_user_pick(ko, pfx, away)
            pr = pick_home or pick_away
            if not pr:
                continue
            team, fl = (home, TEAMS[home][1]) if pick_home else (away, TEAMS[away][1])
            label = "pasa {} {}".format(fl, team)
            if played and ko_list is not None:
                emoji = ko_pick_eval(pfx, pr, ko_list)[0]
                ents.append(((_EMOJI_RANK.get(emoji, 9), name), "{} {} · {}".format(label, emoji, name)))
            else:
                ents.append(((0 if pick_home else 1, name), "{} · {}".format(label, name)))
    ents.sort(key=lambda e: e[0])
    head = "<b>{} {} - {} {} · {}</b>".format(
        TEAMS[home][1], home, away, TEAMS[away][1], ROUND_LBL.get(pfx, "Grupos"))
    body = "\n".join(t for _, t in ents) if ents else "Nadie ha hecho predicción."
    msg = head + "\n\n" + body
    if played:
        msg += "\n\n<i>{}</i>".format(PRED_LEGEND)
    return msg


def cmd_predicciones_resto(token, cid, u, porras):
    """46 filas con la predicción de cada uno para el partido en foco (el del último aviso o
    comparación). Antes del partido van ordenadas por marcador; después, con el emoji de cada uno."""
    matches = fetch_matches()
    mid = u.get("focus_match")
    m = next((x for x in matches if x.get("id") == mid), None) if mid else None
    if not m:
        up = _upcoming_matches(porras, 1)
        m = up[0][1] if up else None
    if not m:
        send(token, cid, "No hay partido para mostrar ahora mismo.")
        return
    home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
    pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
    slot = ko_cid_map(matches, porras).get(m.get("id")) if pfx == "c" else None
    played = (m.get("status") or "").lower() in FINISHED_ST
    ko_list = ko_results_list(matches) if played else None
    send(token, cid, predicciones_resto(porras, home, away, pfx, slot, played, ko_list))


def load_probs():
    """Lee probs.json (simulación Monte Carlo precalculada con la clasificación estimada)."""
    try:
        with open(os.path.join(HERE, "probs.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("probs.json error:", e, file=sys.stderr)
        return None


def check_scheduled_broadcasts(token, state):
    """Envía los avisos programados en aviso_programado.json cuya hora ya pasó.
    Se marca cada id en el estado (persistido) para no reenviar tras un relevo."""
    try:
        with open(os.path.join(HERE, "aviso_programado.json"), encoding="utf-8") as f:
            avisos = json.load(f)
    except Exception:
        return
    sent = state.setdefault("avisos_enviados", [])
    now = datetime.now(timezone.utc)
    for av in avisos:
        aid = av.get("id")
        if not aid or aid in sent or not av.get("text"):
            continue
        try:
            at = datetime.fromisoformat(av.get("at", ""))
        except Exception:
            continue
        if now < at:
            continue
        n = 0
        for ucid, uu in state["users"].items():
            if uu.get("confirmed") and send(token, ucid, "📢 " + av["text"]):
                n += 1
        sent.append(aid)
        persist(state, force=True)
        print("Aviso programado '{}' enviado a {} personas.".format(aid, n))


_probsim = {"proc": None, "ts": 0.0}


def launch_probsim():
    """Recalcula las probabilidades en segundo plano (probsim.py). No bloquea el loop;
    como mucho un proceso a la vez y con 10 min de margen entre lanzamientos."""
    p = _probsim["proc"]
    if p is not None and p.poll() is None:
        return
    if time.monotonic() - _probsim["ts"] < 600:
        return
    try:
        _probsim["proc"] = subprocess.Popen([sys.executable, os.path.join(HERE, "probsim.py")],
                                            cwd=HERE)
        _probsim["ts"] = time.monotonic()
        print("probsim lanzado en segundo plano.")
    except Exception as e:
        print("probsim launch error:", e, file=sys.stderr)


def probs_stale(matches):
    """True si probs.json no refleja los partidos de KO ya terminados."""
    d = load_probs()
    if not d:
        return True
    n = sum(1 for m in matches
            if KO_MAP.get((m.get("round_name") or "").strip().lower())
            and (m.get("status") or "").lower() in FINISHED_ST and _match_winner(m))
    return n != d.get("n_finished")


def cmd_probganar(token, cid, pid):
    d = load_probs()
    if not d:
        send(token, cid, "Las probabilidades no están disponibles ahora mismo.")
        return
    lines = ["<b>PROBABILIDAD DE GANAR LA PORRA</b>",
             "<i>{} simulaciones · {}</i>".format(
                 "{:,}".format(d.get("sims", 12000)).replace(",", "."),
                 d.get("estado", "")), ""]
    for i, r in enumerate(d["lista"]):
        p, name, pw = r[0], r[1], r[2]
        podio = " (podio {}%)".format(r[3]) if len(r) > 3 else ""
        row = "{}º · {}%{} · {}".format(i + 1, pw, podio, name)
        lines.append("<b>{}</b> 👈".format(row) if p == pid else row)
    lines.append("\nEl porqué de tu posición: /porque")
    send(token, cid, "\n".join(lines))


def cmd_porque(token, cid, pid):
    d = load_probs()
    why = (d or {}).get("why", {}).get(str(pid))
    if not why:
        send(token, cid, "No tengo tu análisis ahora mismo. Prueba /probabilidadganar.")
        return
    send(token, cid, why + "\n\n" + d.get("claves", ""))


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
        user["last_compared"] = cands[0]["id"]
        do_compare_pid(token, cid, my_pid, cands[0]["id"], porras)
    else:
        send(token, cid, "¿Con cuál quieres comparar?", reply_markup=compare_keyboard(cands))


def _ko_pred_score(pred, h, a):
    """Marcador pronosticado orientado al partido real (h-a). En empate, '*' en el lado
    del ganador (pasa por penaltis). '—' si no hay predicción."""
    if not pred or not pred.get("winner"):
        return "—"
    swap = match_team(pred.get("homeTeam")) == a  # bracket al revés que el partido real
    sh, sa = (pred.get("scoreA"), pred.get("scoreH")) if swap else (pred.get("scoreH"), pred.get("scoreA"))
    if sh == sa:
        w = match_team(pred.get("winner"))
        if w == h:
            return "{}*-{}".format(sh, sa)
        if w == a:
            return "{}-{}*".format(sh, sa)
    return "{}-{}".format(sh, sa)


# Rondas del bracket en orden (etiqueta, prefijo de slot, nº de cruces)
KO_ROUNDS = [("16avos", "c", 16), ("Octavos", "oct", 8), ("Cuartos", "qf", 4),
             ("Semifinales", "sf", 2), ("🥉 3er puesto", "p3f", 1), ("🏆 Final", "fin2", 1)]


def _ko_flag(name):
    """(bandera, código de 3 letras) de un equipo por su nombre en español."""
    c = match_team(name)
    return (TEAMS.get(c, (None, ""))[1], c or (name or "?"))


def _ko_own_score(s):
    """Marcador pronosticado tal cual lo guardó el usuario (sin reorientar al partido
    real). En empate, '*' en el lado del ganador (pasa por penaltis)."""
    sh, sa, w = s.get("scoreH"), s.get("scoreA"), match_team(s.get("winner") or "")
    if sh == sa:
        return "{}*-{}".format(sh, sa) if w == match_team(s.get("homeTeam") or "") else "{}-{}*".format(sh, sa)
    return "{}-{}".format(sh, sa)


def ko_pick_eval(pfx, pred, ko_list):
    """(emoji, puntos, jugado) de un acierto, replicando las reglas de la web: ganador (team) +
    marcador exacto (desde la perspectiva del ganador) + full (además el rival exacto). Solo
    para mostrar '(+N)' en /miporra; el ranking oficial sale del motor de la web."""
    if ko_list is None or pfx not in KO_PTS or not pred or not pred.get("winner"):
        return ("", 0, False)
    team = match_team(pred.get("winner"))
    if not team:
        return ("", 0, False)
    outcome, r = ko_team_outcome(pfx, team, ko_list)
    if outcome is None:
        return ("", 0, False)              # su partido de esa ronda aún no se ha jugado
    if outcome == "pending":
        return ("⏳", None, True)           # jugado, esperando el resultado (penaltis)
    tp, sp, fp = KO_PTS[pfx]
    # marcador desde la perspectiva del equipo que el usuario dio por ganador (como la web)
    pw = (pred.get("scoreH"), pred.get("scoreA")) if match_team(pred.get("homeTeam")) == team \
        else (pred.get("scoreA"), pred.get("scoreH"))
    if r.get("home_c") == team:
        rw = (r["sh"], r["sa"])
    elif r.get("away_c") == team:
        rw = (r["sa"], r["sh"])
    else:
        rw = (None, None)
    exact = _int(pw[0]) is not None and _int(pw[0]) == _int(rw[0]) and _int(pw[1]) == _int(rw[1])
    if outcome == "loss":
        # Ganador fallado. En 16avos, si clavaste el marcador (típico empate por penaltis que
        # se va al otro lado) sumas el marcador igual, como la web. En octavos+ no suma nada.
        if pfx == "c" and exact:
            return ("🎯", sp, True)         # acertaste el marcador, no quién pasa
        return ("❌", 0, True)              # nada
    if not exact:
        return ("☑️", tp, True)             # solo ganador
    cruce = {match_team(pred.get("homeTeam")), match_team(pred.get("awayTeam"))}
    if fp > 0 and None not in cruce and cruce == {r["home_c"], r["away_c"]}:
        return ("👑", tp + sp + fp, True)   # ganador + marcador + rival exacto (full)
    return ("✅", tp + sp, True)            # ganador + marcador exacto


def bracket_block(ko, ko_list=None):
    """Pinta el bracket de una porra agrupado por ronda: el CRUCE completo (ambos equipos +
    marcador) en todas las rondas. De octavos en adelante el rival es el que el usuario puso
    (puede no coincidir con la realidad). Si se pasa ko_list, marca ✅/🎯/❌ según si el equipo
    que el usuario dio por clasificado ganó/cayó en su partido real de esa ronda."""
    ko = ko or {}
    lines = []
    for label, pfx, n in KO_ROUNDS:
        slots = [ko.get(pfx + str(i)) for i in range(n)]
        slots = [s for s in slots if s and s.get("winner")]
        if not slots:
            continue
        lines.append("\n<b>{}</b>".format(label))
        for s in slots:
            emoji, pts, played = ko_pick_eval(pfx, s, ko_list)
            fh, ch = _ko_flag(s.get("homeTeam"))
            fa, ca = _ko_flag(s.get("awayTeam"))
            if not played:
                tail = ""
            elif pts is None:                       # pendiente de penaltis
                tail = " · {} pendiente".format(emoji)
            else:
                tail = " · {} (+{} puntos)".format(emoji, pts)
            lines.append("{} {} {} {} {}{}".format(fh, ch, _ko_own_score(s), ca, fa, tail))
    return lines


def _round_winner_codes(ko, pfx, n):
    """Equipos (código) que una porra cree que pasan en la ronda 'pfx', por orden de slot."""
    out = []
    for i in range(n):
        pr = (ko or {}).get(pfx + str(i))
        if pr and pr.get("winner"):
            c = match_team(pr.get("winner"))
            if c and c not in out:
                out.append(c)
    return out


def do_compare_pid(token, cid, my_pid, other_pid, porras):
    by_id = {p["id"]: p for p in porras}
    me, other = by_id.get(my_pid), by_id.get(other_pid)
    if not me or not other:
        send(token, cid, "No he podido cargar esa porra.")
        return
    other_name = display_name(other.get("nombre"), other.get("apellidos"))
    my_gr, their_gr = me.get("gr") or {}, other.get("gr") or {}
    my_ko, their_ko = me.get("ko") or {}, other.get("ko") or {}
    lines = ["<b>TU PREDICCIÓN vs {}</b>".format(other_name.upper()),
             "━━━━━━━━━━━━━━━━"]
    # Próximos partidos con cruce fijo (16avos / grupos): marcador vs marcador.
    for k, h, a, slot in next_matches(None, porras):
        pfx = ko_pfx(slot) if slot else None
        if pfx and pfx != "c":
            continue  # octavos+ se comparan por ronda más abajo (el cruce no coincide)
        if slot:
            mp = _ko_pred_score(my_ko.get(slot), h, a)
            tp = _ko_pred_score(their_ko.get(slot), h, a)
        else:
            mp = user_pick(my_gr, h, a) or "X"
            tp = user_pick(their_gr, h, a) or "X"
        row = "{} {}-{} {} → {} / {}".format(TEAMS[h][1], h, a, TEAMS[a][1], mp, tp)
        lines.append("<b>{}</b>".format(row) if mp != tp else row)

    # De octavos en adelante el cruce no coincide: se comparan los EQUIPOS que cada uno
    # cree que pasan en cada ronda.
    def _flags(codes):
        return " ".join("{} {}".format(TEAMS.get(c, (None, ""))[1], c) for c in codes) or "—"
    for label, pfx, n in KO_ROUNDS:
        if pfx == "c":
            continue
        mine = _round_winner_codes(my_ko, pfx, n)
        theirs = _round_winner_codes(their_ko, pfx, n)
        if not mine and not theirs:
            continue
        coin = [c for c in mine if c in theirs]
        only_me = [c for c in mine if c not in theirs]
        only_th = [c for c in theirs if c not in mine]
        # La Final no es "quién pasa" sino quién gana el título; va sin la coletilla.
        head = "<b>{}</b>".format(label) if pfx == "fin2" else "<b>{}</b> · quién pasa".format(label)
        lines.append("\n" + head)
        if coin:
            lines.append("🤝 ambos: " + _flags(coin))
        if only_me:
            lines.append("🔵 solo tú: " + _flags(only_me))
        if only_th:
            lines.append("🔴 solo {}: {}".format(other_name, _flags(only_th)))
    lines.append("\n<i>tú / {}  ·  en 16avos, en negrita donde discrepáis</i>".format(other_name))
    lines.append("\nVer su porra entera: /versuporra")
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
    """Resumen de uso (solo admin): quién, cuánta actividad y qué comandos usan.
    Excluye al propio admin (Marcos): sus pruebas desvirtuarían las métricas."""
    us = [u for ucid, u in state["users"].items()
          if u.get("confirmed") and ucid != ADMIN_CHAT_ID]
    total = sum(u.get("msgs", 0) for u in us)
    hoy = datetime.now(ESP_TZ).strftime("%d/%m")
    activos = sum(1 for u in us if _esp(u.get("last_active")) == hoy)
    lines = ["<b>👥 USUARIOS ({})</b>".format(len(us)),
             "Mensajes: {} · activos hoy: {}".format(total, activos),
             "━━━━━━━━━━━━━━━━"]
    d = load_probs() or {}
    prob_pos = {row[0]: (i + 1, row[2]) for i, row in enumerate(d.get("lista", []))}
    for u in sorted(us, key=lambda x: -x.get("msgs", 0)):
        pos, pw = prob_pos.get(u.get("pid"), (None, None))
        prob_txt = " · prob {}º ({}%)".format(pos, pw) if pos else ""
        lines.append("{} · alta {} · {} msg · últ {}{}".format(
            u.get("name"), _esp(u.get("first_seen")), u.get("msgs", 0),
            _esp(u.get("last_active"), True), prob_txt))
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
                    u["last_compared"] = int(data[4:])
                    do_compare_pid(token, cid, u["pid"], int(data[4:]), porras)
                continue
            if data == "cmpmode:otros":
                if u.get("confirmed"):
                    u["awaiting_compare"] = True
                    send(token, cid, "¿Con quién quieres comparar? Escríbeme su nombre.")
                continue
            if data == "cmpmode:todos":
                if u.get("confirmed"):
                    kb = match_stats_keyboard(porras)
                    send(token, cid, "Elige un partido para ver sus estadísticas:" if kb
                         else "No hay próximos partidos ahora mismo.", reply_markup=kb)
                continue
            if data.startswith("mstat:"):
                if u.get("confirmed"):
                    u["focus_match"] = int(data[6:])
                    cmd_match_stats(token, cid, int(data[6:]), porras, by_id.get(u["pid"]))
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
                send(token, cid, "¿Con quién te la quieres comparar?",
                     reply_markup=compare_mode_keyboard())
            elif cmd in RANK_CMDS or cmd in FULLRANK_CMDS:
                cmd_ranking_full(token, cid, u["pid"], porras)
            elif cmd in PREDRESTO_CMDS:
                cmd_predicciones_resto(token, cid, u, porras)
            elif cmd in PROBGANAR_CMDS:
                cmd_probganar(token, cid, u["pid"])
            elif cmd in PORQUE_CMDS:
                cmd_porque(token, cid, u["pid"])
            elif cmd in VERSUPORRA_CMDS:
                op = by_id.get(u.get("last_compared"))
                if op:
                    cmd_miporra(token, cid, op, (state.get("scorers") or {}).get("goals"), porras,
                                title="Porra de " + display_name(op.get("nombre"), op.get("apellidos")))
                else:
                    send(token, cid, "Primero compárate con alguien: /comparar")
            elif cmd in PORRA_CMDS:
                cmd_miporra(token, cid, by_id.get(u["pid"]) or {},
                            (state.get("scorers") or {}).get("goals"), porras)
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
        # Un error puntual NO debe tumbar el bucle (lo captura run_loop y reintenta).
        raise RuntimeError("API partidos {}: {}".format(r.status_code, r.text[:120]))
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


# --------------------------------------------------------------------------- #
# Fase eliminatoria
# --------------------------------------------------------------------------- #
KO_MAP = {"round of 32": "c", "round of 16": "oct", "quarterfinals": "qf",
          "quarter-finals": "qf", "semifinals": "sf", "semi-finals": "sf",
          "final": "fin2", "match for 3rd place": "p3f", "3rd place final": "p3f"}
# Puntos por ronda (ganador, marcador exacto, full=además el rival exacto). Mismos valores
# que el KO_PTS de la web de César. Solo se usan para mostrar '(+N)' en /miporra; el ranking
# oficial lo calcula su web.
KO_PTS = {"c": (5, 5, 0), "oct": (10, 10, 10), "qf": (15, 15, 15),
          "sf": (20, 20, 20), "p3f": (25, 25, 25), "fin2": (30, 30, 30)}


R16_PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11), (12, 13), (14, 15)]
QF_PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7)]
SF_PAIRS = [(0, 1), (2, 3)]


_ko_ovr_cache = {"v": {}, "ts": 0.0}


def ko_overrides():
    """Overrides manuales de ganador de KO que César mete en su web (`KO_WINNER_OVERRIDES`), para
    los partidos de penaltis que la API deja sin rellenar. Devuelve {match_id: nombre_equipo}.
    Cacheado 10 min; si no se puede leer, cae a lo cacheado/{} (el bot sigue con solo-API)."""
    now = time.monotonic()
    if _ko_ovr_cache["ts"] and now - _ko_ovr_cache["ts"] < 600:
        return _ko_ovr_cache["v"]
    try:
        txt = requests.get(WEB_URL, timeout=20).text
        blk = re.search(r"KO_WINNER_OVERRIDES\s*=\s*\{([^}]*)\}", txt)
        ovr = {}
        if blk:
            for mm in re.finditer(r"(\d+)\s*:\s*'([^']+)'", blk.group(1)):
                ovr[int(mm.group(1))] = mm.group(2)
        _ko_ovr_cache["v"], _ko_ovr_cache["ts"] = ovr, now
        return ovr
    except Exception as e:
        print("ko_overrides error:", e, file=sys.stderr)
        return _ko_ovr_cache["v"]


def _match_winner(m):
    """Code del ganador de un partido terminado (prórroga/penaltis/override de César), o None."""
    if (m.get("status") or "").lower() not in FINISHED_ST:
        return None
    et = m.get("extra_time_score") or {}
    sh = et.get("home") if et.get("home") is not None else m.get("home_score")
    sa = et.get("away") if et.get("away") is not None else m.get("away_score")
    home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
    if sh is None or sa is None or not home or not away:
        return None
    if sh > sa:
        return home
    if sa > sh:
        return away
    pen = m.get("penalty_shootout") or {}
    if pen:
        return home if (pen.get("home", 0) or 0) > (pen.get("away", 0) or 0) else away
    # Empate sin penaltis en la API: usar el override manual de la web de César si lo hay.
    ovr = match_team(ko_overrides().get(m.get("id")))
    return ovr if ovr in (home, away) else None


def _ref_r32(porras):
    """{0..15: frozenset(codeH, codeA)} de los dieciseisavos, por consenso de las porras."""
    out = {}
    for i in range(16):
        cnt = {}
        for p in porras or []:
            pr = (p.get("ko") or {}).get("c" + str(i))
            if pr:
                ch, ca = match_team(pr.get("homeTeam")), match_team(pr.get("awayTeam"))
                if ch and ca:
                    key = frozenset((ch, ca))
                    cnt[key] = cnt.get(key, 0) + 1
        if cnt:
            out[i] = max(cnt, key=cnt.get)
    return out


def ko_cid_map(matches, porras):
    """match_id -> slot del cuadro, mapeado por EQUIPOS reales (no por fecha)."""
    by_round = {}
    for m in matches:
        pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
        if not pfx:
            continue
        h, a = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if h and a:  # equipos definidos (no plazas tipo W101)
            by_round.setdefault(pfx, []).append((m, frozenset((h, a))))

    cidof, winner = {}, {}

    def assign(pfx, i, teams):
        for m, mt in by_round.get(pfx, []):
            if mt == teams and m["id"] not in cidof:
                cidof[m["id"]] = pfx + str(i)
                w = _match_winner(m)
                if w:
                    winner[pfx + str(i)] = w
                return

    for i, teams in _ref_r32(porras).items():       # dieciseisavos: por equipos
        assign("c", i, teams)
    for i, (a, b) in enumerate(R16_PAIRS):           # octavos -> final: por el árbol
        wa, wb = winner.get("c" + str(a)), winner.get("c" + str(b))
        if wa and wb:
            assign("oct", i, frozenset((wa, wb)))
    for i, (a, b) in enumerate(QF_PAIRS):
        wa, wb = winner.get("oct" + str(a)), winner.get("oct" + str(b))
        if wa and wb:
            assign("qf", i, frozenset((wa, wb)))
    for i, (a, b) in enumerate(SF_PAIRS):
        wa, wb = winner.get("qf" + str(a)), winner.get("qf" + str(b))
        if wa and wb:
            assign("sf", i, frozenset((wa, wb)))
    w0, w1 = winner.get("sf0"), winner.get("sf1")
    if w0 and w1:
        assign("fin2", 0, frozenset((w0, w1)))
    losers = []                                       # 3er puesto: perdedores de semis
    for sf in ("sf0", "sf1"):
        w = winner.get(sf)
        for m, mt in by_round.get("sf", []):
            if cidof.get(m["id"]) == sf and w:
                rest = [t for t in mt if t != w]
                if rest:
                    losers.append(rest[0])
    if len(losers) == 2:
        assign("p3f", 0, frozenset(losers))
    return cidof


def ko_real(matches, cidof):
    """slot -> {winner, sh, sa} de los partidos de KO terminados."""
    res = {}
    for m in matches:
        slot = cidof.get(m.get("id"))
        if not slot or (m.get("status") or "").lower() not in FINISHED_ST:
            continue
        et = m.get("extra_time_score") or {}
        sh = et.get("home") if et.get("home") is not None else m.get("home_score")
        sa = et.get("away") if et.get("away") is not None else m.get("away_score")
        w = _match_winner(m)
        if sh is None or sa is None or not w:
            continue
        res[slot] = {"winner": w, "sh": sh, "sa": sa}
    return res


def ko_pfx(cid):
    for p in ("fin2", "p3f", "sf", "qf", "oct", "c"):
        if cid.startswith(p):
            return p
    return None


ROUND_LBL = {"c": "16avos", "oct": "Octavos", "qf": "Cuartos",
             "sf": "Semifinal", "p3f": "3.er puesto", "fin2": "Final"}


# Mapeo de nombres de la API (inglés) a nuestros nombres (español). PORTADO TAL CUAL de la
# web de César (su TEAM_MAP): el ranking del bot debe salir EXACTAMENTE como el de la web, y
# para eso hay que comparar los equipos igual que ella (mismos nombres + matching difuso).
TEAM_MAP_WEB = {
    "Spain": "España", "France": "Francia", "Brazil": "Brasil", "Germany": "Alemania",
    "Argentina": "Argentina", "Portugal": "Portugal", "Netherlands": "Holanda",
    "England": "Inglaterra", "Belgium": "Bélgica", "Croatia": "Croacia",
    "Morocco": "Marruecos", "Japan": "Japón", "South Korea": "Corea del Sur",
    "Mexico": "México", "United States": "EE.UU.", "USA": "EE.UU.", "Canada": "Canadá",
    "Uruguay": "Uruguay", "Ecuador": "Ecuador", "Colombia": "Colombia",
    "Senegal": "Senegal", "Tunisia": "Túnez", "Sweden": "Suecia",
    "Switzerland": "Suiza", "Denmark": "Dinamarca", "Austria": "Austria",
    "Turkey": "Turquía", "Türkiye": "Turquía", "Australia": "Australia",
    "Saudi Arabia": "Arabia Saudita", "Iraq": "Irak", "Norway": "Noruega",
    "Algeria": "Argelia", "Jordan": "Jordania", "DR Congo": "Congo",
    "Uzbekistan": "Uzbekistán", "Ghana": "Ghana", "Panama": "Panamá",
    "Cape Verde": "Cabo Verde", "Cabo Verde": "Cabo Verde",
    "South Africa": "Sudáfrica", "New Zealand": "N.Zelanda",
    "Scotland": "Escocia", "Haiti": "Haití",
    "Bosnia and Herzegovina": "Bosnia", "Bosnia & Herzegovina": "Bosnia",
    "Qatar": "Qatar", "Curacao": "Curazao", "Curaçao": "Curazao",
    "Ivory Coast": "C.Marfil", "Egypt": "Egipto", "Iran": "Irán",
    "Paraguay": "Paraguay", "Czechia": "Chequia", "Czech Republic": "Chequia",
    "Côte d'Ivoire": "C.Marfil",
}


def _map_team(name):
    """API → nuestro nombre, como mapTeam de la web (si no está, devuelve el nombre tal cual)."""
    return TEAM_MAP_WEB.get(name, name)


def _normstr(s):
    """Port de normStr de la web: minúsculas, sin tildes, espacios colapsados."""
    s = unicodedata.normalize("NFD", (s or "").lower().strip())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def _names_match(a, b):
    """Port de namesMatch de la web: igualdad o subcadena (difuso). Esto hace que un hueco
    sin rellenar tipo 'Ganador X/Y' cuente para el equipo (X o Y) que realmente gana."""
    if not a or not b:
        return False
    na = re.sub(r"\bjr\b", "junior", _normstr(a)).strip()
    nb = re.sub(r"\bjr\b", "junior", _normstr(b)).strip()
    return na == nb or na in nb or nb in na


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def ko_results_list(matches):
    """Lista de partidos de KO terminados, con los nombres en ESPAÑOL (como la web) y también
    el código de 3 letras (para los avisos). Base para puntuar y para los mensajes."""
    out = []
    for m in matches:
        pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
        if not pfx:
            continue
        if (m.get("status") or "").lower() not in FINISHED_ST:
            continue
        home, away = _map_team(m.get("home_team")), _map_team(m.get("away_team"))
        et = m.get("extra_time_score") or {}
        sh = et.get("home") if et.get("home") is not None else m.get("home_score")
        sa = et.get("away") if et.get("away") is not None else m.get("away_score")
        sh, sa = sh or 0, sa or 0
        if sh > sa:
            winner = home
        elif sa > sh:
            winner = away
        else:
            ps = m.get("penalty_shootout") or {}
            if ps:
                winner = home if (ps.get("home", 0) or 0) > (ps.get("away", 0) or 0) else away
            else:
                # Sin penaltis en la API: usar el override manual de César si lo hay; si no,
                # PENDIENTE (winner=None) para que el partido aparezca en /miporra como ⏳.
                wc = match_team(ko_overrides().get(m.get("id")))
                winner = home if wc == match_team(home) else (away if wc == match_team(away) else None)
        out.append({"pfx": pfx, "home": home, "away": away, "winner": winner, "sh": sh, "sa": sa,
                    "home_c": match_team(home), "away_c": match_team(away),
                    "winner_c": match_team(winner) if winner else None,
                    "pending": winner is None})
    return out


def _same_ko_game(r, pred):
    """Port de sameKOGame: el cruce real y el predicho son el mismo (en cualquier orden)."""
    rh, ra, ph, pa = r.get("home"), r.get("away"), pred.get("homeTeam"), pred.get("awayTeam")
    if not (rh and ra and ph and pa):
        return False
    return (_names_match(rh, ph) and _names_match(ra, pa)) \
        or (_names_match(rh, pa) and _names_match(ra, ph))


def _find_real_ko(pfx, pred, ko_list):
    """Port de findRealKOForPrediction: 16avos por cruce (equipos), octavos+ por equipo ganador."""
    if not pred or not pred.get("winner"):
        return None
    if pfx == "c":
        for r in ko_list:
            if r["pfx"] == "c" and _same_ko_game(r, pred):
                return r
        return None
    for r in ko_list:
        if r["pfx"] == pfx and _names_match(r["winner"], pred["winner"]):
            return r
    return None


def ko_team_outcome(pfx, team, ko_list):
    """('win'|'loss'|None, partido) del 'team' (código) en la ronda 'pfx'. Solo para los avisos."""
    for r in (ko_list or []):
        if r["pfx"] != pfx:
            continue
        if r.get("pending") and team in (r.get("home_c"), r.get("away_c")):
            return ("pending", r)   # jugado pero aún no se sabe quién pasó (penaltis)
        if r.get("winner_c") == team:
            return ("win", r)
        if team in (r.get("home_c"), r.get("away_c")):
            return ("loss", r)
    return (None, None)


def ko_user_pick(ko, pfx, team):
    """La predicción de la porra para la ronda 'pfx' cuyo ganador es 'team' (código). Avisos."""
    for slot, pred in (ko or {}).items():
        if ko_pfx(slot) == pfx and pred and pred.get("winner") \
                and match_team(pred.get("winner")) == team:
            return pred
    return None


def ko_pcts(porras, slot, real):
    """(% acertó ganador, % acertó marcador exacto) entre quienes predijeron ese cruce."""
    preds = [pr for p in porras if p.get("active")
             for pr in [(p.get("ko") or {}).get(slot)]
             if pr and pr.get("winner") and match_team(pr["winner"])]
    n = len(preds)
    if not n:
        return (0, 0)
    g = sum(1 for pr in preds if match_team(pr["winner"]) == real["winner"])
    e = sum(1 for pr in preds if match_team(pr["winner"]) == real["winner"]
            and pr.get("scoreH") == real["sh"] and pr.get("scoreA") == real["sa"])
    return (round(100 * g / n), round(100 * e / n))


def ko_pcts_adv(porras, pfx, winner_team, sh, sa):
    """Octavos+: (% que tenía a 'winner_team' como que pasa esa ronda, % con marcador exacto),
    entre los activos que hicieron alguna predicción de esa ronda."""
    voters = []
    for p in porras:
        if not p.get("active"):
            continue
        ko = p.get("ko") or {}
        if any(ko_pfx(s) == pfx and (pr or {}).get("winner") for s, pr in ko.items()):
            voters.append(ko)
    n = len(voters)
    if not n:
        return (0, 0)
    g = e = 0
    for ko in voters:
        pk = ko_user_pick(ko, pfx, winner_team)
        if pk:
            g += 1
            if pk.get("scoreH") == sh and pk.get("scoreA") == sa:
                e += 1
    return (round(100 * g / n), round(100 * e / n))


def _disp(name, flag_first=True):
    c = match_team(name)
    if not c:
        return name  # placeholder tipo "Ganador X/Y"
    return "{} {}".format(TEAMS[c][1], c) if flag_first else "{} {}".format(c, TEAMS[c][1])


def _pred_line(pred):
    sh, sa = pred.get("scoreH"), pred.get("scoreA")
    if sh == sa:  # empate: '*' en el lado del ganador
        if pred.get("winner") == pred.get("homeTeam"):
            sh = "{}*".format(sh)
        elif pred.get("winner") == pred.get("awayTeam"):
            sa = "{}*".format(sa)
    return "Tu predicción: {} {}-{} {}".format(
        _disp(pred.get("homeTeam"), True), sh, sa, _disp(pred.get("awayTeam"), False))


def msg_comienzo_ko(home, away, pred):
    l1 = "Comienzo de {} {} - {} {}".format(TEAMS[home][1], home, away, TEAMS[away][1])
    if pred and pred.get("winner"):
        return l1 + "\n" + _pred_line(pred)
    return l1 + "\nTu predicción: —"


def ko_stats_line(g, e):
    if g == 0:
        return "No ha acertado NADIE"
    if e == 0:
        return "El {}% ha acertado el ganador y nadie el exacto".format(g)
    return "El {}% ha acertado el ganador y el {}% el exacto".format(g, e)


def msg_final_ko(home, away, real, pred, g, e, ranking_txt="", cheer=None):
    sh, sa, rw = real["sh"], real["sa"], real["winner"]
    pw = match_team(pred.get("winner")) if (pred and pred.get("winner")) else None
    acierto = pw is not None and pw == rw
    exacto = acierto and pred.get("scoreH") == sh and pred.get("scoreA") == sa
    pen = " (pen.)" if sh == sa and rw else ""
    out = "{} Final: {} {} {}-{} {} {}{}".format(
        "✅" if acierto else "❌", TEAMS[home][1], home, sh, sa, away, TEAMS[away][1], pen)
    if cheer:
        out += "\n" + cheer
    if exacto:
        out += "\n🎯 ¡Resultado exacto!"
    out += "\n" + (_pred_line(pred) if pred and pred.get("winner") else "Tu predicción: —")
    out += "\n" + ko_stats_line(g, e)
    out += "\nPredicciones del resto: /prediccionesdelresto"
    if ranking_txt:
        out += "\n\n━━━━━━━━━━━━━━━━\n\n" + ranking_txt
    return out


def _ko_team_score(pred, team):
    """Marcador pronosticado con los goles de 'team' primero (X-Y), para mostrarlo."""
    sh, sa = pred.get("scoreH"), pred.get("scoreA")
    if match_team(pred.get("homeTeam")) == team:
        return "{}-{}".format(sh, sa)
    return "{}-{}".format(sa, sh)


_DEPTH_ORDER = ["c", "oct", "qf", "p3f", "sf", "fin2"]


def _ko_depth(ko, team):
    """Hasta qué ronda predijo el usuario que llega 'team' (índice; -1 si nunca)."""
    d = -1
    for i, pfx in enumerate(_DEPTH_ORDER):
        if ko_user_pick(ko, pfx, team):
            d = i
    return d


def _pred_vs_line(home, away, pred, winner):
    """'Tu predicción: 🇫🇷 FRA 0 - 1 ESP 🇪🇸' orientado al partido real, ganando 'winner'
    con el marcador que el usuario le puso a 'winner' en su ronda."""
    if match_team(pred.get("homeTeam")) == winner:
        wg, lg = pred.get("scoreH"), pred.get("scoreA")
    else:
        wg, lg = pred.get("scoreA"), pred.get("scoreH")
    x, y = (wg, lg) if home == winner else (lg, wg)
    if x == y:  # empate: '*' en el ganador (pasa por penaltis)
        xs = "{}*".format(x) if home == winner else "{}".format(x)
        ys = "{}*".format(y) if away == winner else "{}".format(y)
    else:
        xs, ys = str(x), str(y)
    return "Tu predicción: {} {} {} - {} {} {}".format(
        TEAMS[home][1], home, xs, ys, away, TEAMS[away][1])


def msg_comienzo_ko_adv(home, away, lbl, pfx, ko):
    """Comienzo de un partido de octavos+. El cruce real puede no coincidir con la porra:
    se dice cuántos de los dos pusiste como clasificados y tu predicción orientada al partido.
    Si tienes a los dos, gana el que predijiste que llega más lejos."""
    l1 = "Comienzo de {} {} - {} {} · {}".format(
        TEAMS[home][1], home, away, TEAMS[away][1], lbl)
    hp, ap = ko_user_pick(ko, pfx, home), ko_user_pick(ko, pfx, away)
    if hp and ap:
        winner = home if _ko_depth(ko, home) >= _ko_depth(ko, away) else away
        pred = hp if winner == home else ap
        return "{}\nTienes a ambos como clasificados.\n{}".format(
            l1, _pred_vs_line(home, away, pred, winner))
    if hp:
        return "{}\nTienes a {} {} como clasificado.\n{}".format(
            l1, TEAMS[home][1], home, _pred_vs_line(home, away, hp, home))
    if ap:
        return "{}\nTienes a {} {} como clasificado.\n{}".format(
            l1, TEAMS[away][1], away, _pred_vs_line(home, away, ap, away))
    return "{}\nNo tienes a ninguno como clasificado.".format(l1)


def ko_comienzo_extra(porras, home, away, pfx, slot, points_by_id=None):
    """Bloque de estadísticas (igual para todos) para el comienzo de un partido KO: reparto de
    la porra, marcador más repetido (solo 16avos) y pronóstico del líder."""
    active = [p for p in porras if p.get("active")]
    lines = []
    # Reparto: a quién da la porra como clasificado en este partido.
    nh = na = 0
    for p in active:
        ko = p.get("ko") or {}
        if pfx == "c":
            pr = ko.get(slot) if slot else None
            w = match_team(pr.get("winner")) if pr and pr.get("winner") else None
        else:
            w = home if ko_user_pick(ko, pfx, home) else (away if ko_user_pick(ko, pfx, away) else None)
        if w == home:
            nh += 1
        elif w == away:
            na += 1
    if nh + na:
        lines.append("La porra: {}% {} {} · {}% {} {}".format(
            round(100 * nh / (nh + na)), TEAMS[home][1], home,
            round(100 * na / (nh + na)), TEAMS[away][1], away))
    # Marcador más repetido (solo 16avos: el cruce es fijo y los marcadores son comparables).
    if pfx == "c" and slot:
        counts = {}
        for p in active:
            pr = (p.get("ko") or {}).get(slot)
            if pr and pr.get("scoreH") is not None and pr.get("scoreA") is not None:
                k = (pr["scoreH"], pr["scoreA"])
                counts[k] = counts.get(k, 0) + 1
        if counts:
            (sh, sa), n = max(counts.items(), key=lambda kv: kv[1])
            lines.append("Marcador más común: {}-{} ({})".format(sh, sa, n))
    # Pronóstico del líder de la clasificación (necesita el ranking del motor de la web).
    if points_by_id:
        rk = ranking(porras, points_by_id)
        if rk:
            lid_id, lid_name, _ = rk[0]
            lko = next((p.get("ko") or {} for p in porras if p["id"] == lid_id), {})
            first = (lid_name or "Líder").split()[0]
            if pfx == "c":
                pr = lko.get(slot) if slot else None
                if pr and pr.get("winner"):
                    fh, ch = _ko_flag(pr.get("homeTeam"))
                    fa, ca = _ko_flag(pr.get("awayTeam"))
                    lines.append("Predicción del Líder ({}): {} {} {} {} {}".format(
                        first, fh, ch, _ko_own_score(pr), ca, fa))
            else:
                if ko_user_pick(lko, pfx, home):
                    lines.append("Predicción del Líder ({}): pasa {} {}".format(first, TEAMS[home][1], home))
                elif ko_user_pick(lko, pfx, away):
                    lines.append("Predicción del Líder ({}): pasa {} {}".format(first, TEAMS[away][1], away))
    lines.append("\nPredicciones del resto: /prediccionesdelresto")
    return "\n" + "\n".join(lines)


def msg_final_ko_adv(home, away, real, lbl, home_pick, away_pick, g, e,
                     ranking_txt="", cheer=None):
    """Final de un partido de octavos+. Acierto = tenías al ganador como que pasa la ronda."""
    sh, sa, rw = real["sh"], real["sa"], real["winner"]
    win_pick = home_pick if rw == home else (away_pick if rw == away else None)
    lose_pick = away_pick if rw == home else (home_pick if rw == away else None)
    acierto = win_pick is not None
    exacto = acierto and win_pick.get("scoreH") == sh and win_pick.get("scoreA") == sa
    pen = " (pen.)" if sh == sa and rw else ""
    icon = "✅" if acierto else ("❌" if lose_pick else "ℹ️")
    out = "{} {}: {} {} {}-{} {} {}{}".format(
        icon, lbl, TEAMS[home][1], home, sh, sa, away, TEAMS[away][1], pen)
    if cheer:
        out += "\n" + cheer
    if exacto:
        out += "\n🎯 ¡Marcador exacto!"
    if acierto:
        out += "\n✅ Acertaste: pasa {} {}".format(TEAMS[rw][1], rw)
    elif lose_pick:
        loser = away if rw == home else home
        out += "\n❌ Tú tenías a {} {}, eliminado".format(TEAMS[loser][1], loser)
    else:
        out += "\nℹ️ No tenías a ninguno de los dos como clasificado"
    out += "\n" + ko_stats_line(g, e)
    out += "\nPredicciones del resto: /prediccionesdelresto"
    if ranking_txt:
        out += "\n\n━━━━━━━━━━━━━━━━\n\n" + ranking_txt
    return out


def check_matches(token, porras, state):
    """Mira los partidos y envía comienzos/finales nuevos a todos los identificados."""
    porras_by_pid = {p["id"]: p for p in porras}
    now = datetime.now(timezone.utc)
    matches = fetch_matches()

    # La clasificación la calcula el MOTOR DE LA WEB (Node). Se pide una sola vez por pasada
    # y solo si hay que mandar algún final (que es lo que la lleva incrustada).
    _pts_cache = {}

    def _pts():
        if "v" not in _pts_cache:
            try:
                _pts_cache["v"] = web_points(porras, matches)
            except RuntimeError as e:
                # Web caída/rota: se manda el final sin la tabla (mejor que no avisar).
                print("ranking no disponible (web):", e, file=sys.stderr)
                _pts_cache["v"] = None
        return _pts_cache["v"]

    relevant = []
    for m in matches:
        home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if home and away and frozenset((home, away)) in GROUP_KEYS:
            relevant.append((m, home, away))

    # Eliminatoria: slot de los 16avos (para el aviso del cruce fijo).
    cidof = ko_cid_map(matches, porras)
    relevant_ko = []
    for m in matches:
        pfx = KO_MAP.get((m.get("round_name") or "").strip().lower())
        home, away = match_team(m.get("home_team")), match_team(m.get("away_team"))
        if pfx and home and away:
            relevant_ko.append((m, home, away, pfx))

    def _seed(items):
        for it in items:
            m, st = it[0], (it[0].get("status") or "").lower()
            if st in LIVE_ST or st in FINISHED_ST:
                state["comienzo"].append(m["id"])
            if st in FINISHED_ST:
                state["final"].append(m["id"])

    # Primera ejecución: marcar lo ya jugado como avisado (sin spamear el historial).
    if not state["seeded"]:
        _seed(relevant)
        _seed(relevant_ko)
        state["seeded"] = True
        state["seeded_ko"] = True
        persist(state, force=True)
        print("Estado inicializado: {} comienzos y {} finales ya marcados.".format(
            len(state["comienzo"]), len(state["final"])))
        return 0

    # Sembrado puntual de eliminatoria (para el bot que ya estaba sembrado de grupos).
    if not state.get("seeded_ko"):
        _seed(relevant_ko)
        state["seeded_ko"] = True
        persist(state, force=True)
        print("Eliminatoria sembrada: {} partidos de KO marcados.".format(len(relevant_ko)))

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
                rk = ranking_block(porras, _pts(), u["pid"])
                if send(token, cid, msg_final(home, away, pick, gh, ga, real == pick, rk, pct, cheer)):
                    enviados += 1
            final_set.add(mid)
            state["final"].append(mid)
            persist(state, force=True)

    # ── Eliminatoria ──
    # 16avos (cruces fijos): se avisa por el cruce concreto, como hasta ahora.
    # Octavos en adelante: el cruce real puede no coincidir con la porra, así que se avisa
    # por EQUIPO (¿tu clasificado de esta ronda gana su partido?), igual que puntúa la web.
    for m, home, away, pfx in relevant_ko:
        mid = m["id"]
        st = (m.get("status") or "").lower()
        kickoff = parse_dt(m.get("event_date"))
        ya_empezado = st in LIVE_ST or st in FINISHED_ST
        toca_comienzo = ya_empezado or (kickoff is not None and now >= kickoff)
        if kickoff is not None and (now - kickoff).total_seconds() > COMIENZO_MAX_RETRASO_H * 3600:
            demasiado_tarde = st not in LIVE_ST
        else:
            demasiado_tarde = False
        lbl = ROUND_LBL.get(pfx, "")
        slot = cidof.get(mid) if pfx == "c" else None

        if mid not in comienzo_set and toca_comienzo and not demasiado_tarde:
            extra = ko_comienzo_extra(porras, home, away, pfx, slot, _pts())
            for chat, u in users:
                u["focus_match"] = mid
                ko = porras_by_pid[u["pid"]].get("ko") or {}
                if pfx == "c":
                    msg = msg_comienzo_ko(home, away, ko.get(slot) if slot else None)
                else:
                    msg = msg_comienzo_ko_adv(home, away, lbl, pfx, ko)
                if send(token, chat, msg + extra):
                    enviados += 1
            comienzo_set.add(mid)
            state["comienzo"].append(mid)
            persist(state, force=True)

        if st in FINISHED_ST and mid not in final_set:
            w = _match_winner(m)
            if not w:
                continue
            et = m.get("extra_time_score") or {}
            sh = et.get("home") if et.get("home") is not None else m.get("home_score")
            sa = et.get("away") if et.get("away") is not None else m.get("away_score")
            if sh is None or sa is None:
                continue
            real = {"winner": w, "sh": sh, "sa": sa}
            cheer = PHRASES_ESP[int(mid) % len(PHRASES_ESP)] if w == "ESP" else None
            if pfx == "c":
                g, e = ko_pcts(porras, slot, real) if slot else (0, 0)
            else:
                g, e = ko_pcts_adv(porras, pfx, w, sh, sa)
            for chat, u in users:
                u["focus_match"] = mid
                ko = porras_by_pid[u["pid"]].get("ko") or {}
                rk = ranking_block(porras, _pts(), u["pid"])
                if pfx == "c":
                    msg = msg_final_ko(home, away, real, ko.get(slot) if slot else None,
                                       g, e, rk, cheer)
                else:
                    msg = msg_final_ko_adv(home, away, real, lbl,
                                           ko_user_pick(ko, pfx, home),
                                           ko_user_pick(ko, pfx, away), g, e, rk, cheer)
                if send(token, chat, msg):
                    enviados += 1
            final_set.add(mid)
            state["final"].append(mid)
            persist(state, force=True)

    # Probabilidades: recalcular en segundo plano si no reflejan los partidos ya jugados
    # (cubre tanto el final que acaba de enviarse como huecos de cuando el bot estuvo caído).
    try:
        if probs_stale(matches):
            launch_probsim()
    except Exception as e:
        print("probs_stale error:", e, file=sys.stderr)

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

    # Sincronizar el menú de comandos de Telegram con BOT_COMMANDS en cada arranque.
    try:
        tg(token, "setMyCommands", commands=BOT_COMMANDS)
    except Exception as e:
        print("setMyCommands error:", e, file=sys.stderr)

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

            check_scheduled_broadcasts(token, state)

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
