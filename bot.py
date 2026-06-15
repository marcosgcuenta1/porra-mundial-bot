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
from datetime import datetime, timezone

import requests

from data import TEAMS, PORRA

TG_API = "https://api.telegram.org/bot{token}/{method}"
SB_URL = "https://zavqpnsbsmivsuvwurkd.supabase.co"
SB_KEY = "sb_publishable_gzIrT1J8xG5ZGTIxJHFzfg_BAO-XGPm"
# Misma API de partidos que usa la web de la porra (resultados consistentes con el ranking).
SPORTS_URL = "https://sports.bzzoiro.com/api/v2/events/?league_id=27&season_id=188&limit=200"
SPORTS_KEY = os.environ.get("SPORTS_TOKEN", "65282d7cc77d80d27171566864fc427e7a6f1266")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))

COMIENZO_MAX_RETRASO_H = 6
GROUP_LETTERS = "ABCDEFGHIJKL"
LIVE_ST = {"inprogress", "1h", "ht", "2h", "et", "bt", "p", "break", "live", "penalties"}
FINISHED_ST = {"finished", "ft", "aet", "ap", "after_extra_time", "after_penalties"}


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
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("comienzo", [])
    st.setdefault("final", [])
    st.setdefault("seeded", False)
    st.setdefault("tg_offset", 0)
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


_last_commit = [0.0]


def commit_state():
    """En GitHub Actions, guarda state.json en el repo para que el siguiente relevo lo herede."""
    try:
        subprocess.run(["git", "add", "state.json"], check=False)
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
            "asked": False, "stage": "awaiting_name", "candidates": []}


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
    rows.sort(key=lambda x: -x[2])
    return rows


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
    lines = ["<b>Clasificación actual</b>"]
    for i in range(start, end):
        pid, name, pts = rk[i]
        line = "{}º {} — {} pts".format(i + 1, name, pts)
        lines.append("<b>{}</b>".format(line) if pid == my_pid else line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mensajes de partido
# --------------------------------------------------------------------------- #
def team_label(code, winner):
    return "<b>{}</b>".format(code) if code == winner else code


def msg_comienzo(home, away, winner):
    fh, fa = TEAMS[home][1], TEAMS[away][1]
    return "Comienzo de {} {} - {} {}".format(fh, team_label(home, winner),
                                              team_label(away, winner), fa)


def msg_final(home, away, winner, gh, ga, acierto, ranking_txt=""):
    fh, fa = TEAMS[home][1], TEAMS[away][1]
    emoji = "✅" if acierto else "❌"
    base = "{} Final: {} {} {}-{} {} {}".format(
        emoji, fh, team_label(home, winner), gh, ga, team_label(away, winner), fa)
    return base + ("\n\n" + ranking_txt if ranking_txt else "")


# --------------------------------------------------------------------------- #
# Chat: identificación de cada participante ("¿quién eres?")
# --------------------------------------------------------------------------- #
ASK_TEXT = ("\U0001f44b ¡Hola! Soy el bot de la porra del Mundial 2026.\n"
            "Para avisarte en cada partido y mostrarte tu posición, dime: "
            "<b>¿quién eres?</b>\nEscribe tu nombre y apellidos.")

RESET_CMDS = ("/start", "/reset", "/cambiar", "/quien")
RANK_CMDS = ("/clasificacion", "/clasi", "/ranking", "/posicion", "/puesto")
PORRA_CMDS = ("/miporra", "/porra", "/pronosticos")
MUTE_CMDS = ("/silencio", "/mute", "/pausa")
UNMUTE_CMDS = ("/avisos", "/activar", "/voz", "/reanudar")
HELP_CMDS = ("/ayuda", "/help", "/comandos")

AYUDA = ("<b>Comandos</b>\n"
         "/clasificacion — tu posición ahora mismo\n"
         "/miporra — tus pronósticos especiales (MVP, goleadores…)\n"
         "/silencio — pausar los avisos de partidos\n"
         "/avisos — reactivar los avisos\n"
         "/start — cambiar de identidad\n"
         "/ayuda — esta ayuda")

# Lista para el menú de comandos de Telegram (setMyCommands).
BOT_COMMANDS = [
    {"command": "clasificacion", "description": "Tu posición en la clasificación"},
    {"command": "miporra", "description": "Tus pronósticos especiales"},
    {"command": "silencio", "description": "Pausar los avisos"},
    {"command": "avisos", "description": "Reactivar los avisos"},
    {"command": "start", "description": "Identificarte / cambiar de identidad"},
    {"command": "ayuda", "description": "Ver los comandos"},
]


def cmd_ranking(token, cid, pid, porras):
    results = group_results(fetch_matches())
    blk = ranking_block(porras, results, pid)
    send(token, cid, blk or "Aún no estás en la clasificación (o no hay resultados todavía).")


def cmd_miporra(token, cid, p):
    if not p:
        send(token, cid, "No encuentro tu porra.")
        return
    lines = ["<b>Tu porra</b>"]
    if p.get("mvp"):
        lines.append("🏆 MVP: " + p["mvp"])
    if p.get("gol1"):
        lines.append("⚽ Goleador del torneo: " + p["gol1"] +
                     (" ({})".format(p["gol1n"]) if p.get("gol1n") else ""))
    if p.get("gol2"):
        lines.append("🇪🇸 Goleador de España: " + p["gol2"] +
                     (" ({})".format(p["gol2n"]) if p.get("gol2n") else ""))
    for campo, etq in (("camp", "🥇 Campeón"), ("sub", "🥈 Subcampeón"),
                       ("p3", "🥉 Tercero"), ("p4", "4º")):
        if p.get(campo):
            lines.append("{}: {}".format(etq, p[campo]))
    lines.append("\nTu pronóstico de cada partido te lo recuerdo al empezar.")
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
            if u.get("confirmed") or u.get("stage") != "awaiting_confirm":
                continue
            data = cq.get("data", "")
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
                         "tu zona de la clasificación.\n\nEscribe /ayuda para ver los comandos.".format(u["name"]))
            continue

        # ── Mensaje de texto ──
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        if u.get("confirmed"):
            cmd = low.split()[0].split("@")[0]
            if cmd in RESET_CMDS:
                u.update(new_user())
                ask(token, cid, u)
            elif cmd in RANK_CMDS:
                cmd_ranking(token, cid, u["pid"], porras)
            elif cmd in PORRA_CMDS:
                cmd_miporra(token, cid, by_id.get(u["pid"]) or {})
            elif cmd in MUTE_CMDS:
                u["muted"] = True
                send(token, cid, "🔕 Avisos en pausa. Reactívalos con /avisos.")
            elif cmd in UNMUTE_CMDS:
                u["muted"] = False
                send(token, cid, "🔔 Avisos reactivados.")
            elif cmd in HELP_CMDS:
                send(token, cid, AYUDA)
            elif text.startswith("/"):
                send(token, cid, "No conozco ese comando.\n\n" + AYUDA)
            continue
        if low in RESET_CMDS or text.startswith("/") or not u.get("asked"):
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
                if send(token, cid, msg_comienzo(home, away, pick)):
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
            for cid, u in users:
                pick = user_pick(porras_by_pid[u["pid"]].get("gr"), home, away)
                rk = ranking_block(porras, results, u["pid"])
                if send(token, cid, msg_final(home, away, pick, gh, ga, real == pick, rk)):
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
    start = time.monotonic()
    MAX_RUN = 50 * 60  # el workflow lo refresca cada 30 min; esto es el tope de seguridad
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
