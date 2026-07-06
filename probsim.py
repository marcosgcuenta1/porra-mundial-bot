# -*- coding: utf-8 -*-
"""Recalcula probs.json (probabilidades de ganar la porra + análisis personalizados).

Se lanza desde el bot en segundo plano tras cada final de partido (y al detectar que está
desactualizado). Fija los partidos ya jugados del CUADRO REAL (refs W##/L## de la API) y
simula solo lo pendiente, 12.000 veces, con las reglas exactas de la web de César. Los
puntos actuales salen del motor real de la web (web_points); aquí solo se añade el futuro.
Desempate: Iván gana los empates en los que está; el resto reparte.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

import bot

N_SIMS = 12000
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probs.json")

KO_PTS = {"c": (5, 5, 0), "oct": (10, 10, 10), "qf": (15, 15, 15),
          "sf": (20, 20, 20), "p3f": (25, 25, 25), "fin2": (30, 30, 30)}
ROUND_OF = {"round of 32": "c", "round of 16": "oct", "quarterfinals": "qf",
            "quarter-finals": "qf", "semifinals": "sf", "semi-finals": "sf",
            "final": "fin2", "match for 3rd place": "p3f", "3rd place final": "p3f"}

# Ratings CALIBRADOS a las cuotas reales de campeón (FanDuel vía FOX Sports, 1-jul-2026,
# devig proporcional): con ellos la simulación reproduce el mercado con error <0,3 pp
# (FRA 30% · ARG 17% · ESP 11% · ENG 9% · BRA 8% · POR 5% · MAR 4%...).
R = {"FRA": 2029, "ESP": 1987, "ARG": 1977, "ENG": 1956, "POR": 1946, "BRA": 1944,
     "AUT": 1903, "USA": 1900, "MEX": 1898, "BEL": 1886, "MAR": 1885, "NOR": 1885,
     "COL": 1869, "CRO": 1855, "SUI": 1830, "CAN": 1755, "PAR": 1745, "ALG": 1720,
     "EGY": 1705, "GHA": 1690, "AUS": 1690, "SWE": 1750, "SEN": 1780, "ECU": 1780,
     "BIH": 1640, "COD": 1600, "JPN": 1760, "NED": 1920, "GER": 1900, "RSA": 1650,
     "CIV": 1740, "CPV": 1565}

STARS = {"ESP": ("lamine yamal", "pedri"), "FRA": ("kylian mbappe", "ousmane dembele"),
         "ENG": ("harry kane", "jude bellingham"), "ARG": ("lionel messi", "julian alvarez"),
         "BRA": ("vinicius junior", "raphinha"), "POR": ("cristiano ronaldo", "vitinha"),
         "NOR": ("erling haaland", "martin odegaard"), "BEL": ("kevin de bruyne", "jeremy doku"),
         "MAR": ("achraf hakimi", "brahim diaz"), "MEX": ("santiago gimenez", "julian quinones"),
         "USA": ("christian pulisic", "folarin balogun"), "COL": ("luis diaz", "james rodriguez"),
         "CRO": ("luka modric", "josko gvardiol"), "SUI": ("granit xhaka", "breel embolo"),
         "CAN": ("alphonso davies", "jonathan david"), "PAR": ("miguel almiron", "julio enciso"),
         "AUT": ("david alaba", "marcel sabitzer"), "EGY": ("mohamed salah", "omar marmoush")}

GOL1_CAND = [("kylian mbappe", "FRA", 0.95), ("lionel messi", "ARG", 0.65),
             ("erling haaland", "NOR", 0.85), ("harry kane", "ENG", 0.80),
             ("vinicius junior", "BRA", 0.60), ("ousmane dembele", "FRA", 0.55),
             ("lamine yamal", "ESP", 0.50), ("julian alvarez", "ARG", 0.55),
             ("mikel oyarzabal", "ESP", 0.50), ("cristiano ronaldo", "POR", 0.60),
             ("santiago gimenez", "MEX", 0.50), ("julian quinones", "MEX", 0.45),
             ("folarin balogun", "USA", 0.50), ("mohamed salah", "EGY", 0.65),
             ("ismaila sarr", None, 0.0), ("kai havertz", None, 0.0),
             ("deniz undav", None, 0.0), ("ismael saibari", None, 0.0)]
GOL2_CAND = [("mikel oyarzabal", 0.50), ("lamine yamal", 0.40), ("ferran torres", 0.35),
             ("nico williams", 0.30), ("dani olmo", 0.25), ("alvaro morata", 0.25),
             ("pedri", 0.12)]

ES = {"ESP": "España", "FRA": "Francia", "ARG": "Argentina", "ENG": "Inglaterra",
      "BRA": "Brasil", "POR": "Portugal", "NOR": "Noruega", "BEL": "Bélgica",
      "MAR": "Marruecos", "CRO": "Croacia", "COL": "Colombia", "MEX": "México",
      "USA": "EE.UU.", "SUI": "Suiza", "CAN": "Canadá", "PAR": "Paraguay",
      "AUT": "Austria", "ALG": "Argelia", "EGY": "Egipto", "GHA": "Ghana",
      "AUS": "Australia", "CPV": "Cabo Verde"}


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower().strip())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def names_match(a, b):
    na, nb = norm(a).replace(" jr", " junior"), norm(b).replace(" jr", " junior")
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def elo_share(a, b):
    return 1.0 / (1.0 + 10 ** (-(R.get(a, 1700) - R.get(b, 1700)) / 400.0))


def poisson(lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= L:
            return k
        k += 1


def sim_score(a, b, s_share):
    s = min(0.85, max(0.15, s_share))
    ga = min(6, poisson(2.6 * s))
    gb = min(6, poisson(2.6 * (1 - s)))
    if ga > gb:
        w = a
    elif gb > ga:
        w = b
    else:
        w = a if random.random() < 0.5 + (s - 0.5) * 0.4 else b
    return ga, gb, w


def build_bracket(matches, porras):
    """Cuadro real: lista ordenada de nodos con refs W##/L## resueltas dinámicamente.
    R32 = 73+índice de su slot c (mapeo por equipos); oct 89-96, qf 97-100, sf 101-102
    por orden de fecha. Devuelve (nodos, n_finished)."""
    cidof = bot.ko_cid_map(matches, porras)
    nodes, n_fin = [], 0
    by_round = defaultdict(list)
    for m in matches:
        pfx = ROUND_OF.get((m.get("round_name") or "").strip().lower())
        if pfx:
            by_round[pfx].append(m)
    for pfx in by_round:
        by_round[pfx].sort(key=lambda m: (m.get("event_date") or "", str(m.get("id"))))
    num_of = {}
    for m in by_round.get("c", []):
        slot = cidof.get(m.get("id"))
        if slot:
            num_of[m["id"]] = 73 + int(slot[1:])
    for i, m in enumerate(by_round.get("oct", [])):
        num_of[m["id"]] = 89 + i
    for i, m in enumerate(by_round.get("qf", [])):
        num_of[m["id"]] = 97 + i
    for i, m in enumerate(by_round.get("sf", [])):
        num_of[m["id"]] = 101 + i

    def ref_of(name):
        mm = re.match(r"^([WL])(\d+)$", (name or "").strip())
        return (mm.group(1), int(mm.group(2))) if mm else None

    for pfx in ("c", "oct", "qf", "sf", "p3f", "fin2"):
        for m in by_round.get(pfx, []):
            st = (m.get("status") or "").lower()
            fin = st in bot.FINISHED_ST
            w = bot._match_winner(m) if fin else None
            et = m.get("extra_time_score") or {}
            sh = et.get("home") if et.get("home") is not None else m.get("home_score")
            sa = et.get("away") if et.get("away") is not None else m.get("away_score")
            if fin and w:
                n_fin += 1
            nodes.append({
                "num": num_of.get(m["id"]), "pfx": pfx, "slot": cidof.get(m.get("id")),
                "home": bot.match_team(m.get("home_team")), "away": bot.match_team(m.get("away_team")),
                "href": ref_of(m.get("home_team")), "aref": ref_of(m.get("away_team")),
                "fin": fin, "win": w, "sh": sh, "sa": sa,
            })
    return nodes, n_fin


def overlay_proxy_results(nodes, proxy_matches):
    """Sobrescribe en los nodos del cuadro (estructura de bzzoiro) los resultados del PROXY
    de la web (fuente de verdad de marcadores y ganadores). Casa por ronda + equipos."""
    idx = {}
    for m in proxy_matches:
        pfx = ROUND_OF.get((m.get("round_name") or "").strip().lower())
        h, a = bot.match_team(m.get("home_team")), bot.match_team(m.get("away_team"))
        if pfx and h and a:
            idx[(pfx, frozenset((h, a)))] = m
    for nd in nodes:
        if not (nd["home"] and nd["away"]):
            continue
        m = idx.get((nd["pfx"], frozenset((nd["home"], nd["away"]))))
        if m is None:
            continue
        fin = (m.get("status") or "").lower() in bot.FINISHED_ST
        nd["fin"], nd["win"] = fin, (bot._match_winner(m) if fin else None)
        sh, sa = m.get("home_score"), m.get("away_score")
        if bot.match_team(m.get("home_team")) != nd["home"]:
            sh, sa = sa, sh
        nd["sh"], nd["sa"] = sh, sa


def main():
    random.seed()
    porras = bot.fetch_porras()
    matches = bot.fetch_matches()          # proxy de la web: resultados/marcadores de verdad
    struct = bot.fetch_matches_bzzoiro()   # bzzoiro: estructura del cuadro (refs W##/L##)
    CURR = bot.web_points(porras, matches)
    # goles reales: del estado del bot si hay clave; si no, ESPN directo
    goals = {}
    try:
        goals = (bot.load_state().get("scorers") or {}).get("goals") or {}
    except Exception:
        pass
    if not goals:
        st = {"scorers": {"events": [], "goals": {}, "ts": None}}
        bot.refresh_scorers(st)
        goals = st["scorers"]["goals"]

    nodes, _ = build_bracket(struct, porras)
    overlay_proxy_results(nodes, matches)
    # nº de KO acabados según el PROXY (mismo criterio que probs_stale en el bot)
    n_fin = sum(1 for m in matches
                if ROUND_OF.get((m.get("round_name") or "").strip().lower())
                and (m.get("status") or "").lower() in bot.FINISHED_ST and bot._match_winner(m))

    # consenso de la porra en los 16avos pendientes
    crowd = {}
    for nd in nodes:
        if nd["pfx"] == "c" and not nd["fin"] and nd["slot"] and nd["home"] and nd["away"]:
            na = nb = 0
            for p in porras:
                if not p.get("active"):
                    continue
                pr = (p.get("ko") or {}).get(nd["slot"])
                if pr and pr.get("winner") and not pr.get("lateNoScore"):
                    w = bot.match_team(pr["winner"])
                    if w == nd["home"]:
                        na += 1
                    elif w == nd["away"]:
                        nb += 1
            crowd[nd["slot"]] = na / (na + nb) if (na + nb) else 0.5

    # predicciones compiladas
    def wg_og(pr, wcode):
        sh, sa = _int(pr.get("scoreH")), _int(pr.get("scoreA"))
        if sh is None or sa is None:
            return None
        return (sh, sa) if bot.match_team(pr.get("homeTeam")) == wcode else (sa, sh)

    def placeholder_codes(nm):
        n = norm(nm)
        if not n.startswith("ganador "):
            return None
        out = set()
        for t in n[8:].split("/"):
            c = bot.match_team(t.strip())
            if c:
                out.add(c)
        return out

    players = []
    for p in porras:
        if not p.get("active"):
            continue
        ko = p.get("ko") or {}
        cpred, later = {}, defaultdict(list)
        for slot, pr in ko.items():
            pfx = bot.ko_pfx(slot)
            if not pfx or not pr or not pr.get("winner") or pr.get("lateNoScore"):
                continue
            if pfx == "c":
                w = bot.match_team(pr["winner"])
                if w:
                    cpred[slot] = (w, wg_og(pr, w))
                continue
            ph = placeholder_codes(pr["winner"])
            if ph is not None:
                later[pfx].append(("PH", frozenset(ph), None, None))
                continue
            w = bot.match_team(pr["winner"])
            if not w:
                continue
            th, ta = bot.match_team(pr.get("homeTeam")), bot.match_team(pr.get("awayTeam"))
            later[pfx].append(("W", w, wg_og(pr, w),
                               frozenset((th, ta)) if th and ta else None))
        players.append({
            "pid": p["id"], "name": bot.display_name(p.get("nombre"), p.get("apellidos")),
            "curr": CURR.get(p["id"], 0), "c": cpred, "later": dict(later),
            "camp": bot.match_team(p.get("camp")), "sub": bot.match_team(p.get("sub")),
            "p3": bot.match_team(p.get("p3")),
            "mvp": bot.normalize_player(p.get("mvp")), "gol1": bot.normalize_player(p.get("gol1")),
            "gol1n": _int(p.get("gol1n")), "gol2": bot.normalize_player(p.get("gol2")),
            "gol2n": _int(p.get("gol2n")),
            "ivan": norm(bot.display_name(p.get("nombre"), p.get("apellidos"))) == "ivan gomez peral",
        })
    ivan_pid = next((pl["pid"] for pl in players if pl["ivan"]), None)

    wins, top3 = defaultdict(float), defaultdict(float)
    champ_count = Counter()

    for _ in range(N_SIMS):
        wres = {}          # num -> (winner, loser)
        sim_by_round = defaultdict(dict)   # pfx -> winner -> (teams_fs, wg, og)
        c_sim = {}         # slot -> (home, away, ga, gb, winner)
        matches_of = defaultdict(int)
        champ = runner = third = None
        for nd in nodes:
            h = nd["home"] or (wres.get(nd["href"][1], (None, None))[0 if nd["href"][0] == "W" else 1]
                               if nd["href"] else None)
            a = nd["away"] or (wres.get(nd["aref"][1], (None, None))[0 if nd["aref"][0] == "W" else 1]
                               if nd["aref"] else None)
            if nd["fin"] and nd["win"]:
                w, ga, gb = nd["win"], nd["sh"], nd["sa"]
                lo = a if w == h else h
            else:
                if not (h and a):
                    continue
                if nd["pfx"] == "c" and nd["slot"] in crowd:
                    s = 0.5 * elo_share(h, a) + 0.5 * crowd[nd["slot"]]
                elif nd["fin"]:   # acabado en empate sin dato de penaltis: marcador fijo
                    ga, gb = nd["sh"], nd["sa"]
                    s = elo_share(h, a)
                    w = h if random.random() < 0.5 + (s - 0.5) * 0.4 else a
                    lo = a if w == h else h
                    wres[nd["num"]] = (w, lo) if nd["num"] else None
                    sim_by_round[nd["pfx"]][w] = (frozenset((h, a)),
                                                  (ga, gb) if w == h else (gb, ga), None)
                    if nd["pfx"] == "c":
                        c_sim[nd["slot"]] = (h, a, ga, gb, w)
                    continue
                else:
                    s = elo_share(h, a)
                ga, gb, w = sim_score(h, a, s)
                lo = a if w == h else h
                matches_of[h] += 1
                matches_of[a] += 1
                wg = (ga, gb) if w == h else (gb, ga)
                sim_by_round[nd["pfx"]][w] = (frozenset((h, a)), wg, None)
                if nd["pfx"] == "c" and nd["slot"]:
                    c_sim[nd["slot"]] = (h, a, ga, gb, w)
            if nd["num"]:
                wres[nd["num"]] = (w, a if w == h else h)
            if nd["pfx"] == "fin2":
                champ, runner = w, (a if w == h else h)
            if nd["pfx"] == "p3f":
                third = w
        champ_count[champ] += 1

        # premios individuales
        tally = {}
        for nm, team, rate in GOL1_CAND:
            rem = matches_of.get(team, 0) if team else 0
            tally[nm] = goals.get(nm, 0) + (poisson(rate * rem) if rem else 0)
        mx = max(tally.values())
        gol1_win = random.choice([n for n, v in tally.items() if v == mx])
        gol1_n = tally[gol1_win]
        esp_rem = matches_of.get("ESP", 0)
        t2 = {}
        for nm, rate in GOL2_CAND:
            t2[nm] = goals.get(nm, 0) + (poisson(rate * esp_rem) if esp_rem else 0)
        mx2 = max(t2.values())
        gol2_win = random.choice([n for n, v in t2.items() if v == mx2])
        gol2_n = t2[gol2_win]
        rr = random.random()
        s1, s2 = STARS.get(champ, (None, None)), STARS.get(runner, (None, None))
        mvp = s1[0] if rr < 0.60 and s1[0] else (s1[1] if rr < 0.72 and s1[1] else
                                                 (s2[0] if rr < 0.85 and s2[0] else None))

        totals = []
        for pl in players:
            pts = pl["curr"]
            for slot, (w, sc) in pl["c"].items():
                r = c_sim.get(slot)
                if r is None:
                    continue
                A, B, ga, gb, rw = r
                if w == rw:
                    pts += 5
                if sc is not None and w in (A, B):
                    real = (ga, gb) if w == A else (gb, ga)
                    if sc == real:
                        pts += 5
            for pfx, preds in pl["later"].items():
                tp, sp, fp = KO_PTS[pfx]
                table = sim_by_round.get(pfx) or {}
                for kind, w, sc, teams in preds:
                    if kind == "PH":
                        if any(x in table for x in w):
                            pts += tp
                        continue
                    r = table.get(w)
                    if r is None:
                        continue
                    pts += tp
                    if sc is not None and sc == r[1]:
                        pts += sp
                        if teams is not None and teams == r[0]:
                            pts += fp
            if champ and pl["camp"] == champ:
                pts += 50
            if runner and pl["sub"] == runner:
                pts += 30
            if third and pl["p3"] == third:
                pts += 20
            if mvp and pl["mvp"] and names_match(pl["mvp"], mvp):
                pts += 20
            if pl["gol1"] and names_match(pl["gol1"], gol1_win):
                pts += 20
                if pl["gol1n"] == gol1_n:
                    pts += 20
            if pl["gol2"] and names_match(pl["gol2"], gol2_win):
                pts += 15
                if pl["gol2n"] == gol2_n:
                    pts += 20
            totals.append((pl["pid"], pts))
        best = max(t for _, t in totals)
        lead = [pid for pid, t in totals if t == best]
        if ivan_pid in lead:
            wins[ivan_pid] += 1.0
        else:
            for pid in lead:
                wins[pid] += 1.0 / len(lead)
        order = sorted(totals, key=lambda x: (-x[1], 0 if x[0] == ivan_pid else 1, random.random()))
        for pid, _ in order[:3]:
            top3[pid] += 1.0

    # ---------------- salida: lista + porqués + claves ----------------
    names = {pl["pid"]: pl["name"] for pl in players}
    by_prob = sorted(players, key=lambda pl: -wins.get(pl["pid"], 0))
    prob_rank = {pl["pid"]: i + 1 for i, pl in enumerate(by_prob)}
    by_curr = sorted(players, key=lambda pl: (-pl["curr"], 0 if pl["ivan"] else 1))
    curr_rank = {pl["pid"]: i + 1 for i, pl in enumerate(by_curr)}
    camp_share = Counter(pl["camp"] for pl in players)
    mvp_share = Counter(pl["mvp"] for pl in players if pl["mvp"])
    gol1_share = Counter(pl["gol1"] for pl in players if pl["gol1"])
    champ_p = {t: c / N_SIMS for t, c in champ_count.items() if t}

    def pct(x):
        return ("{:.1f}".format(100.0 * x)).replace(".", ",")

    def why(pl):
        pid = pl["pid"]
        pw, p3v = wins.get(pid, 0) / N_SIMS, top3.get(pid, 0) / N_SIMS
        L = ["Vas {}º con {} puntos. Probabilidad de ganar la porra: <b>{}%</b> "
             "({}º más probable) · top 3: {}%.".format(
                 curr_rank[pid], pl["curr"], pct(pw), prob_rank[pid], pct(p3v))]
        c = pl["camp"]
        if c:
            n_c, p_c = camp_share[c], champ_p.get(c, 0)
            if n_c >= 15:
                L.append("Tu campeona es {}, como otros {} — si gana, media porra sube contigo "
                         "y apenas te diferencia.".format(ES.get(c, c), n_c - 1))
            elif n_c > 1:
                L.append("Tu campeona es {} (solo {} la tenéis, {}% en la simulación): si gana, "
                         "adelantas de golpe a la mayoría.".format(ES.get(c, c), n_c, pct(p_c)))
            else:
                L.append("Eres el único con {} campeona ({}%): si sonara la flauta, "
                         "el premio gordo es casi solo tuyo.".format(ES.get(c, c), pct(p_c)))
        m = pl["mvp"]
        if m:
            n_m = mvp_share[m]
            L.append("Tu MVP ({}) casi no lo comparte nadie: si acierta, son 20 puntos que "
                     "casi nadie más suma.".format(m.title()) if n_m <= 2 else
                     "Tu MVP ({}) lo compartís {} — acertarlo te da poco filo.".format(m.title(), n_m))
        g1, n1 = pl["gol1"], pl["gol1n"]
        if g1:
            now_g = 0
            for nm in goals:
                if names_match(nm, g1):
                    now_g = max(now_g, goals[nm])
            n_g = gol1_share[g1]
            if n1 is not None and n1 < now_g:
                fact = "tu cifra de {} goles ya es imposible (lleva {})".format(n1, now_g)
            elif n1 is not None and now_g <= n1 <= now_g + 3:
                fact = "tu cifra de {} goles es alcanzable (lleva {}) — ese +20 extra puede decidir".format(n1, now_g)
            elif n1 is not None:
                fact = "tu cifra de {} goles es difícil (lleva {})".format(n1, now_g)
            else:
                fact = "lleva {}".format(now_g)
            L.append("Pichichi {} como {} más; {}.".format(g1.title(), n_g - 1, fact)
                     if n_g > 1 else
                     "Tu pichichi ({}) es apuesta casi única; {}.".format(g1.title(), fact))
        if pl["ivan"]:
            L.append("Y tu as: la cláusula de desempate — ganas TODOS los empates a puntos.")
        if any(True for s, v in (next((p for p in porras if p["id"] == pid), {}).get("ko") or {}).items()
               if v and v.get("lateNoScore")):
            L.append("Ojo: tus cruces bloqueados de la porra tardía no puntúan, "
                     "vas con lastre respecto al resto.")
        if pw < 0.005:
            L.append("Necesitas una carambola: que tus aciertos diferenciales salgan Y que "
                     "pinchen todos los que van por delante con tus mismas elecciones.")
        return "\n".join(L)

    fav = by_prob[0]
    fav_bits = []
    if fav["ivan"]:
        fav_bits.append("la cláusula de desempate")
    if fav["camp"]:
        fav_bits.append("{} campeona (solo {} la tienen)".format(
            ES.get(fav["camp"], fav["camp"]), camp_share[fav["camp"]]))
    surprise = max(by_prob[:6], key=lambda pl: curr_rank[pl["pid"]] - prob_rank[pl["pid"]])
    top_camp, n_top_camp = camp_share.most_common(1)[0]
    top_g1, n_top_g1 = gol1_share.most_common(1)[0]
    g1_now = max((v for k, v in goals.items() if names_match(k, top_g1)), default=0)
    flags = " · ".join("{} {}".format(bot.TEAMS.get(t, ("", t))[1], pct(pv))
                       for t, pv in sorted(champ_p.items(), key=lambda kv: -kv[1])[:5])
    claves = ("<b>Las claves de la porra</b>\n"
              "• Favorito: {} ({}º en la tabla real) — {}.\n"
              "• La sorpresa: {} (va {}º pero es {}º en probabilidad) — su porra se "
              "diferencia donde otros van en manada.\n"
              "• {} de 46 tenéis a {} campeona: si gana, casi no mueve la clasificación "
              "entre vosotros; deciden cruces, subcampeón, MVP y pichichis.\n"
              "• {} de 46 tenéis a {} de pichichi (lleva {}): el desempate real está en "
              "clavar SU número exacto de goles.\n"
              "• P(campeón): {}").format(
        fav["name"], curr_rank[fav["pid"]], " + ".join(fav_bits) or "su porra diferencial",
        surprise["name"], curr_rank[surprise["pid"]], prob_rank[surprise["pid"]],
        n_top_camp, ES.get(top_camp, top_camp),
        n_top_g1, top_g1.title(), g1_now, flags + "%")

    now_es = datetime.now(timezone.utc).astimezone(bot.ESP_TZ)
    out = {"gen": now_es.strftime("%d/%m %H:%M"),
           "estado": "actualizado {} · tras {} de 22 partidos de eliminatoria".format(
               now_es.strftime("%d/%m %H:%M"), n_fin),
           "n_finished": n_fin, "sims": N_SIMS,
           # los comandos se bloquean desde el inicio del primer cuarto (pierde la gracia)
           "qf_kickoff": min((m.get("event_date") for m in matches
                              if ROUND_OF.get((m.get("round_name") or "").strip().lower()) == "qf"
                              and m.get("event_date")), default=None),
           "claves": claves,
           "lista": [[pl["pid"], pl["name"], pct(wins.get(pl["pid"], 0) / N_SIMS),
                      pct(top3.get(pl["pid"], 0) / N_SIMS)] for pl in by_prob],
           "why": {str(pl["pid"]): why(pl) for pl in players}}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("probs.json regenerado ({} sims, {} KO jugados).".format(N_SIMS, n_fin))

    # en GitHub Actions, commitear para que sobreviva a los relevos
    if os.environ.get("GITHUB_ACTIONS"):
        try:
            subprocess.run(["git", "add", "probs.json"], cwd=HERE, check=False)
            r = subprocess.run(["git", "-c", "user.name=bot",
                                "-c", "user.email=bot@users.noreply.github.com",
                                "commit", "-m", "Probabilidades actualizadas [skip ci]"],
                               cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                for _ in range(3):
                    subprocess.run(["git", "pull", "--rebase", "origin", "master"],
                                   cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False)
                    pr = subprocess.run(["git", "push", "origin", "HEAD:master"],
                                        cwd=HERE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, check=False)
                    if pr.returncode == 0:
                        break
        except Exception as e:
            print("probs commit error:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
