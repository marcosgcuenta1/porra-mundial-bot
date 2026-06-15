# -*- coding: utf-8 -*-
"""
Bot de Telegram — Porra Mundial 2026 de Marcos.

En cada ejecución:
  1. Pide a football-data.org los partidos del Mundial (competición "WC").
  2. Para cada partido de FASE DE GRUPOS que esté en la porra:
       - Al comienzo  -> manda "Comienzo de ..." con tu pronóstico (ganador en negrita).
       - Al terminar  -> manda "Final: ... marcador ..." con ✅ si acertaste o ❌ si no.
  3. Guarda en state.json qué avisos ya se mandaron, para no repetir.

Pensado para ejecutarse en bucle (cada ~5 min) desde GitHub Actions, el PC,
o cualquier cron. Es idempotente: ejecutarlo de más no duplica mensajes.

Variables de entorno necesarias:
  TELEGRAM_TOKEN        token del bot de Telegram
  TELEGRAM_CHAT_ID      tu chat_id
  FOOTBALL_DATA_TOKEN   token de https://www.football-data.org (gratis)
Opcionales:
  STATE_FILE            ruta del fichero de estado (por defecto state.json)
"""
import json
import os
import sys
import unicodedata
from datetime import datetime, timezone

import requests

from data import TEAMS, PORRA

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))

# Ventana máxima para mandar el "Comienzo" con retraso (por si el cron estuvo
# caído un rato). Pasado eso, el partido ya estará en "Final" y no tiene sentido.
COMIENZO_MAX_RETRASO_H = 6


# --------------------------------------------------------------------------- #
# Utilidades de casado de equipos
# --------------------------------------------------------------------------- #
def normalize(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


# alias normalizado -> code
_ALIAS_INDEX = {}
for _code, (_name, _flag, _aliases) in TEAMS.items():
    _ALIAS_INDEX[normalize(_code)] = _code
    _ALIAS_INDEX[normalize(_name)] = _code
    for _a in _aliases:
        _ALIAS_INDEX[normalize(_a)] = _code


def match_team(api_team):
    """Devuelve el code interno a partir del dict de equipo de la API."""
    if not api_team:
        return None
    for key in ("tla", "name", "shortName"):
        val = api_team.get(key)
        code = _ALIAS_INDEX.get(normalize(val)) if val else None
        if code:
            return code
    return None


# pronóstico por pareja (sin importar quién es local): frozenset -> winner_code | None(empate)
_PRED = {}
for _h, _a, _pick in PORRA:
    if _pick == "1":
        _w = _h
    elif _pick == "2":
        _w = _a
    else:
        _w = None  # empate
    _PRED[frozenset((_h, _a))] = _w


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
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(token, chat_id, text):
    r = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        print("ERROR Telegram:", data, file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #
# Render de mensajes
# --------------------------------------------------------------------------- #
def team_label(code, winner):
    name, flag, _ = TEAMS[code]
    return f"<b>{name}</b>" if code == winner else name


def msg_comienzo(home, away, winner):
    fh = TEAMS[home][1]
    fa = TEAMS[away][1]
    return f"Comienzo de {fh} {team_label(home, winner)} - {team_label(away, winner)} {fa}"


def msg_final(home, away, winner, gh, ga, acierto):
    fh = TEAMS[home][1]
    fa = TEAMS[away][1]
    emoji = "✅" if acierto else "❌"
    return (
        f"Final: {fh} {team_label(home, winner)} {gh}-{ga} "
        f"{team_label(away, winner)} {fa} {emoji}"
    )


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #
def fetch_matches(fd_token):
    r = requests.get(API_URL, headers={"X-Auth-Token": fd_token}, timeout=30)
    if r.status_code != 200:
        print(f"ERROR football-data ({r.status_code}): {r.text}", file=sys.stderr)
        sys.exit(1)
    return r.json().get("matches", [])


def parse_utc(s):
    # "2026-06-15T18:00:00Z"
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    fd_token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not (token and chat_id and fd_token):
        print("Faltan variables de entorno (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID / "
              "FOOTBALL_DATA_TOKEN).", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    now = datetime.now(timezone.utc)
    matches = fetch_matches(fd_token)

    # Quedarnos solo con partidos de la porra (fase de grupos, pareja conocida).
    relevant = []
    for m in matches:
        if m.get("stage") != "GROUP_STAGE":
            continue
        home = match_team(m.get("homeTeam"))
        away = match_team(m.get("awayTeam"))
        if not home or not away:
            continue
        pair = frozenset((home, away))
        if pair not in _PRED:
            continue
        relevant.append((m, home, away, _PRED[pair]))

    # Primera ejecución: marcar como ya avisados los partidos ya empezados /
    # terminados ANTES de poner el bot en marcha, para no spamear el historial.
    if not state["seeded"]:
        for m, home, away, winner in relevant:
            mid = m["id"]
            status = m.get("status")
            if status in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED"):
                state["comienzo"].append(mid)
            if status == "FINISHED":
                state["final"].append(mid)
        state["seeded"] = True
        save_state(state)
        print(f"Estado inicializado: {len(state['comienzo'])} comienzos y "
              f"{len(state['final'])} finales marcados como ya avisados (no se envía nada).")
        return

    comienzo_set = set(state["comienzo"])
    final_set = set(state["final"])
    enviados = 0

    for m, home, away, winner in relevant:
        mid = m["id"]
        status = m.get("status")
        kickoff = parse_utc(m["utcDate"]) if m.get("utcDate") else None
        ya_empezado = status in ("IN_PLAY", "PAUSED", "FINISHED", "SUSPENDED")
        toca_comienzo = ya_empezado or (kickoff is not None and now >= kickoff)
        # No mandar un "Comienzo" absurdamente tardío.
        if kickoff is not None and (now - kickoff).total_seconds() > COMIENZO_MAX_RETRASO_H * 3600:
            demasiado_tarde = status not in ("IN_PLAY", "PAUSED")
        else:
            demasiado_tarde = False

        # --- Comienzo ---
        if mid not in comienzo_set and toca_comienzo and not demasiado_tarde:
            if send_telegram(token, chat_id, msg_comienzo(home, away, winner)):
                comienzo_set.add(mid)
                state["comienzo"].append(mid)
                enviados += 1
                save_state(state)

        # --- Final ---
        if status == "FINISHED" and mid not in final_set:
            ft = (m.get("score") or {}).get("fullTime") or {}
            gh, ga = ft.get("home"), ft.get("away")
            if gh is None or ga is None:
                continue
            # Por si acaso no se mandó el comienzo, mandarlo antes.
            if mid not in comienzo_set:
                if send_telegram(token, chat_id, msg_comienzo(home, away, winner)):
                    comienzo_set.add(mid)
                    state["comienzo"].append(mid)
                    enviados += 1
            # ¿Acertó? Comparar ganador real con el pronosticado.
            if gh > ga:
                real = home
            elif ga > gh:
                real = away
            else:
                real = None
            acierto = (real == winner)
            if send_telegram(token, chat_id, msg_final(home, away, winner, gh, ga, acierto)):
                final_set.add(mid)
                state["final"].append(mid)
                enviados += 1
                save_state(state)

    save_state(state)
    print(f"Hecho. Mensajes enviados en esta pasada: {enviados}.")


if __name__ == "__main__":
    main()
