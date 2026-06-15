# -*- coding: utf-8 -*-
"""
Bot de Telegram — Porra Mundial 2026 de Marcos.

En cada ejecución:
  1. Atiende el chat: si aún no sabe quién eres, te pregunta y lo confirma.
  2. Pide a football-data.org los partidos del Mundial (competición "WC").
  3. Para cada partido de FASE DE GRUPOS que esté en la porra:
       - Al comienzo  -> "Comienzo de 🇪🇸 ESP - KOR 🇰🇷" (tu pronóstico en negrita).
       - Al terminar  -> "✅ Final: 🇪🇸 ESP 1-0 KOR 🇰🇷" + tu zona de la clasificación.
  4. Guarda en state.json qué avisos ya mandó, para no repetir.

La clasificación se calcula igual que la web https://cesaresteban.github.io/NFQ-WORLD-CUP/
(participantes y porras desde su Supabase; 3 puntos por cada 1/X/2 acertado en grupos).

Variables de entorno necesarias:
  TELEGRAM_TOKEN        token del bot de Telegram
  TELEGRAM_CHAT_ID      tu chat_id
  FOOTBALL_DATA_TOKEN   token de https://www.football-data.org (gratis)
Opcionales:
  STATE_FILE            ruta del fichero de estado (por defecto state.json)
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
# Casado de equipos
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


# Pronóstico personal (PDF) por pareja: frozenset -> winner_code | None(empate)
_PRED = {}
# Clave de grupo por pareja (igual que la web): frozenset -> (key, home_code, away_code)
GROUP_KEYS = {}
for _i, (_h, _a, _pick) in enumerate(PORRA):
    _PRED[frozenset((_h, _a))] = _h if _pick == "1" else (_a if _pick == "2" else None)
    _key = "{}_{}".format(GROUP_LETTERS[_i // 6], _i % 6)
    GROUP_KEYS[frozenset((_h, _a))] = (_key, _h, _a)


# --------------------------------------------------------------------------- #
# Estado
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
    st.setdefault("identity", {"confirmed": False, "pid": None, "name": None,
                               "asked": False, "stage": "awaiting_name", "candidates": []})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg(token, method, **payload):
    r = requests.post(TG_API.format(token=token, method=method), json=payload, timeout=30)
    data = r.json()
    if not data.get("ok"):
        print("ERROR Telegram {}: {}".format(method, data), file=sys.stderr)
    return data


def send_telegram(token, chat_id, text, reply_markup=None):
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
    """Resultados de fase de grupos terminados: clave 'A_0' -> '1'/'x'/'2' (local de la web)."""
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
        # Orientar los goles al "local" que usa la web.
        gh, ga = (gh_api, ga_api) if home == hc else (ga_api, gh_api)
        res[key] = "1" if gh > ga else ("x" if gh == ga else "2")
    return res


def ranking(porras, results):
    """Lista de participantes activos ordenada por puntos (estable). Devuelve [(pid, name, pts)]."""
    rows = []
    for p in porras:
        if not p.get("active"):
            continue
        gr = p.get("gr") or {}
        pts = 3 * sum(1 for k, real in results.items() if gr.get(k) == real)
        rows.append([p["id"], display_name(p.get("nombre"), p.get("apellidos")), pts])
    # Orden estable: por -pts manteniendo el orden de registro (created_at asc) en empates.
    rows.sort(key=lambda x: -x[2])
    return rows


def ranking_block(porras, results, my_pid):
    """Bloque de clasificación: ventana de ±2 alrededor del usuario, con su fila en negrita."""
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
# Identidad: "¿Quién eres?"
# --------------------------------------------------------------------------- #
ASK_TEXT = ("\U0001f44b ¡Hola! Soy el bot de la porra del Mundial 2026.\n"
            "Para mostrarte tu posición en la clasificación, díme: "
            "<b>¿quién eres?</b>\nEscribe tu nombre y apellidos.")


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


def normalize_words(s):
    return [normalize(w) for w in (s or "").split() if normalize(w)]


def confirm_keyboard(candidates):
    rows = [[{"text": display_name(p.get("nombre"), p.get("apellidos")),
              "callback_data": "id:{}".format(p["id"])}] for p in candidates]
    rows.append([{"text": "Otro (no soy ninguno)", "callback_data": "otro"}])
    return {"inline_keyboard": rows}


def process_identity(token, chat_id, porras, state):
    ident = state["identity"]
    if ident.get("confirmed"):
        # Ya identificado: vaciamos updates pendientes. Si manda /start o /reset,
        # reiniciamos la identificación.
        updates = get_updates(token, state["tg_offset"])
        restart = False
        for upd in updates:
            state["tg_offset"] = max(state["tg_offset"], upd["update_id"] + 1)
            msg = upd.get("message")
            if msg and (msg.get("from") or {}).get("id") == int(chat_id):
                t = (msg.get("text") or "").strip().lower()
                if t in ("/start", "/reset", "/cambiar", "/quien"):
                    restart = True
        if restart:
            ident.update(confirmed=False, pid=None, name=None, asked=True,
                         stage="awaiting_name", candidates=[])
            send_telegram(token, chat_id, ASK_TEXT)
            return None
        return ident["pid"]

    if not porras:
        return None  # sin participantes no podemos identificar; reintentamos la próxima vez

    by_id = {p["id"]: p for p in porras}
    updates = get_updates(token, state["tg_offset"])
    for upd in updates:
        state["tg_offset"] = max(state["tg_offset"], upd["update_id"] + 1)

        cq = upd.get("callback_query")
        if cq:
            if (cq.get("from") or {}).get("id") != int(chat_id):
                continue
            tg(token, "answerCallbackQuery", callback_query_id=cq["id"])
            data = cq.get("data", "")
            if ident.get("stage") != "awaiting_confirm":
                continue
            if data == "otro":
                ident["stage"] = "awaiting_name"
                send_telegram(token, chat_id, "Vale. Escribe otra vez tu nombre (nombre y apellidos).")
            elif data.startswith("id:"):
                pid = int(data[3:])
                p = by_id.get(pid)
                if p:
                    ident.update(confirmed=True, pid=pid,
                                 name=display_name(p.get("nombre"), p.get("apellidos")),
                                 stage="done", candidates=[])
                    send_telegram(token, chat_id,
                                  "✅ Hecho, te he identificado como <b>{}</b>.\n"
                                  "Te avisaré al comienzo de cada partido y, al acabar, te pondré "
                                  "tu zona de la clasificación.".format(ident["name"]))
            continue

        msg = upd.get("message")
        if not msg or (msg.get("from") or {}).get("id") != int(chat_id):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if text.startswith("/") or not ident.get("asked"):
            ident["asked"] = True
            ident["stage"] = "awaiting_name"
            send_telegram(token, chat_id, ASK_TEXT)
            continue
        # Tratar el texto como un nombre
        cands = find_candidates(text, porras)
        if not cands:
            send_telegram(token, chat_id,
                          "No te encuentro en la porra \U0001f914. Escríbelo como aparece "
                          "(nombre y apellidos).")
        elif len(cands) > 4:
            send_telegram(token, chat_id, "Hay varios con ese nombre. Añade el apellido, por favor.")
        else:
            ident["stage"] = "awaiting_confirm"
            ident["candidates"] = [p["id"] for p in cands]
            if len(cands) == 1:
                txt = "¿Confirmas que eres <b>{}</b>?".format(
                    display_name(cands[0].get("nombre"), cands[0].get("apellidos")))
            else:
                txt = "He encontrado varios. ¿Cuál eres?"
            send_telegram(token, chat_id, txt, reply_markup=confirm_keyboard(cands))

    # Si nunca hemos preguntado (no había /start pendiente), preguntamos ahora.
    if not ident.get("confirmed") and not ident.get("asked"):
        ident["asked"] = True
        ident["stage"] = "awaiting_name"
        send_telegram(token, chat_id, ASK_TEXT)

    return ident["pid"] if ident.get("confirmed") else None


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


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    fd_token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not (token and chat_id):
        print("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    porras = fetch_porras()

    # 1) Chat / identidad (funciona aunque aún no haya token de football-data)
    my_pid = process_identity(token, chat_id, porras, state)
    save_state(state)

    if not fd_token:
        print("Sin FOOTBALL_DATA_TOKEN todavía: solo atiendo el chat/identidad.")
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
        if not home or not away or frozenset((home, away)) not in _PRED:
            continue
        relevant.append((m, home, away, _PRED[frozenset((home, away))]))

    # Primera ejecución: marcar lo ya jugado como avisado (sin spamear el historial).
    if not state["seeded"]:
        for m, home, away, winner in relevant:
            st = m.get("status")
            if st in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED"):
                state["comienzo"].append(m["id"])
            if st == "FINISHED":
                state["final"].append(m["id"])
        state["seeded"] = True
        save_state(state)
        print("Estado inicializado: {} comienzos y {} finales ya marcados (no se envía nada).".format(
            len(state["comienzo"]), len(state["final"])))
        return

    comienzo_set = set(state["comienzo"])
    final_set = set(state["final"])
    enviados = 0

    for m, home, away, winner in relevant:
        mid = m["id"]
        st = m.get("status")
        kickoff = parse_utc(m["utcDate"]) if m.get("utcDate") else None
        ya_empezado = st in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED")
        toca_comienzo = ya_empezado or (kickoff is not None and now >= kickoff)
        if kickoff is not None and (now - kickoff).total_seconds() > COMIENZO_MAX_RETRASO_H * 3600:
            demasiado_tarde = st not in ("IN_PLAY", "PAUSED")
        else:
            demasiado_tarde = False

        if mid not in comienzo_set and toca_comienzo and not demasiado_tarde:
            if send_telegram(token, chat_id, msg_comienzo(home, away, winner)):
                comienzo_set.add(mid)
                state["comienzo"].append(mid)
                enviados += 1
                save_state(state)

        if st == "FINISHED" and mid not in final_set:
            ft = (m.get("score") or {}).get("fullTime") or {}
            gh, ga = ft.get("home"), ft.get("away")
            if gh is None or ga is None:
                continue
            if mid not in comienzo_set:
                if send_telegram(token, chat_id, msg_comienzo(home, away, winner)):
                    comienzo_set.add(mid)
                    state["comienzo"].append(mid)
                    enviados += 1
            if gh > ga:
                real = home
            elif ga > gh:
                real = away
            else:
                real = None
            acierto = (real == winner)
            rk_txt = ranking_block(porras, results, my_pid)
            if send_telegram(token, chat_id, msg_final(home, away, winner, gh, ga, acierto, rk_txt)):
                final_set.add(mid)
                state["final"].append(mid)
                enviados += 1
                save_state(state)

    save_state(state)
    print("Hecho. Mensajes enviados en esta pasada: {}.".format(enviados))


if __name__ == "__main__":
    main()
