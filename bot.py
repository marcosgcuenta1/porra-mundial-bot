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
import sys
import unicodedata
from datetime import datetime, timezone

import requests

from data import TEAMS, PORRA

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
TG_API = "https://api.telegram.org/bot{token}/{method}"
SB_URL = "https://zavqpnsbsmivsuvwurkd.supabase.co"
SB_KEY = "sb_publishable_gzIrT1J8xG5ZGTIxJHFzfg_BAO-XGPm"
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))

COMIENZO_MAX_RETRASO_H = 6
GROUP_LETTERS = "ABCDEFGHIJKL"


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


def match_team(api_team):
    if not api_team:
        return None
    for key in ("tla", "name", "shortName"):
        val = api_team.get(key)
        code = _ALIAS_INDEX.get(normalize(val)) if val else None
        if code:
            return code
    return None


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


def new_user():
    return {"pid": None, "name": None, "confirmed": False,
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


def get_updates(token, offset):
    r = requests.get(TG_API.format(token=token, method="getUpdates"),
                     params={"offset": offset, "timeout": 0}, timeout=30)
    data = r.json()
    return data.get("result", []) if data.get("ok") else []


# --------------------------------------------------------------------------- #
# Supabase (participantes y porras) + clasificación
# --------------------------------------------------------------------------- #
def fetch_porras():
    try:
        r = requests.get(
            SB_URL + "/rest/v1/porras",
            params={"select": "id,nombre,apellidos,active,gr", "order": "created_at.asc"},
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
        if m.get("stage") != "GROUP_STAGE" or m.get("status") != "FINISHED":
            continue
        home, away = match_team(m.get("homeTeam")), match_team(m.get("awayTeam"))
        gk = GROUP_KEYS.get(frozenset((home, away))) if home and away else None
        if not gk:
            continue
        key, hc, _ac = gk
        ft = (m.get("score") or {}).get("fullTime") or {}
        gh_api, ga_api = ft.get("home"), ft.get("away")
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
    rows.append([{"text": "Otro (no soy ninguno)", "callback_data": "otro"}])
    return {"inline_keyboard": rows}


def ask(token, chat_id, u):
    u["asked"] = True
    u["stage"] = "awaiting_name"
    u["candidates"] = []
    send(token, chat_id, ASK_TEXT)


def process_chat(token, porras, state):
    """Atiende los mensajes entrantes de todos los chats e identifica a cada participante."""
    if not porras:
        return  # sin participantes no podemos identificar; reintentamos la próxima vez
    by_id = {p["id"]: p for p in porras}
    users = state["users"]

    for upd in get_updates(token, state["tg_offset"]):
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
                         "tu zona de la clasificación. (Para cambiar: /start.)".format(u["name"]))
            continue

        # ── Mensaje de texto ──
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        if u.get("confirmed"):
            if low in RESET_CMDS:
                u.update(new_user())
                ask(token, cid, u)
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
            txt = ("¿Confirmas que eres <b>{}</b>?".format(
                       display_name(cands[0].get("nombre"), cands[0].get("apellidos")))
                   if len(cands) == 1 else "He encontrado varios. ¿Cuál eres?")
            send(token, cid, txt, reply_markup=confirm_keyboard(cands))


# --------------------------------------------------------------------------- #
# Partidos
# --------------------------------------------------------------------------- #
def fetch_matches(fd_token):
    r = requests.get(API_URL, headers={"X-Auth-Token": fd_token}, timeout=30)
    if r.status_code != 200:
        print("ERROR football-data ({}): {}".format(r.status_code, r.text), file=sys.stderr)
        sys.exit(1)
    return r.json().get("matches", [])


def parse_utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def confirmed_users(state, porras_by_pid):
    """[(chat_id, user)] de los usuarios identificados con porra conocida."""
    out = []
    for cid, u in state["users"].items():
        if u.get("confirmed") and u.get("pid") in porras_by_pid:
            out.append((cid, u))
    return out


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    fd_token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        print("Falta TELEGRAM_TOKEN.", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    porras = fetch_porras()
    porras_by_pid = {p["id"]: p for p in porras}

    # 1) Atender el chat (identificaciones). Funciona sin token de football-data.
    process_chat(token, porras, state)
    save_state(state)

    if not fd_token:
        print("Sin FOOTBALL_DATA_TOKEN: solo atiendo el chat. Usuarios: {}.".format(
            len(state["users"])))
        return

    # 2) Partidos
    now = datetime.now(timezone.utc)
    matches = fetch_matches(fd_token)
    results = group_results(matches)

    relevant = []
    for m in matches:
        if m.get("stage") != "GROUP_STAGE":
            continue
        home, away = match_team(m.get("homeTeam")), match_team(m.get("awayTeam"))
        if home and away and frozenset((home, away)) in GROUP_KEYS:
            relevant.append((m, home, away))

    # Primera ejecución: marcar lo ya jugado como avisado (sin spamear el historial).
    if not state["seeded"]:
        for m, home, away in relevant:
            st = m.get("status")
            if st in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED"):
                state["comienzo"].append(m["id"])
            if st == "FINISHED":
                state["final"].append(m["id"])
        state["seeded"] = True
        save_state(state)
        print("Estado inicializado: {} comienzos y {} finales ya marcados.".format(
            len(state["comienzo"]), len(state["final"])))
        return

    comienzo_set = set(state["comienzo"])
    final_set = set(state["final"])
    users = confirmed_users(state, porras_by_pid)
    enviados = 0

    for m, home, away in relevant:
        mid = m["id"]
        st = m.get("status")
        kickoff = parse_utc(m["utcDate"]) if m.get("utcDate") else None
        ya_empezado = st in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED")
        toca_comienzo = ya_empezado or (kickoff is not None and now >= kickoff)
        if kickoff is not None and (now - kickoff).total_seconds() > COMIENZO_MAX_RETRASO_H * 3600:
            demasiado_tarde = st not in ("IN_PLAY", "PAUSED")
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
            save_state(state)

        # ── Final: a cada usuario, resultado + acierto + su clasificación ──
        if st == "FINISHED" and mid not in final_set:
            ft = (m.get("score") or {}).get("fullTime") or {}
            gh, ga = ft.get("home"), ft.get("away")
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
            save_state(state)

    save_state(state)
    print("Hecho. Usuarios: {}. Mensajes enviados: {}.".format(len(users), enviados))


if __name__ == "__main__":
    main()
