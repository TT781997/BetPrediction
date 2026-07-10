# -*- coding: utf-8 -*-
"""
QUANT DESK — Streamlit · FotMob live · Poisson/Bayes · EV+ · SQLite · Telegram

Instalar e correr:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...   # opcional
    export ODDS_API_KEY=...                              # opcional (the-odds-api.com)
    export FOOTBALL_DATA_KEY=...                         # opcional (football-data.org, grátis)
    streamlit run app.py

As quatro chaves também podem ser coladas na barra lateral da app (⚙️ Configuração),
que as guarda no quant_desk.db — dispensa variáveis de ambiente. Sem chaves a app
funciona em modo degradado: alertas só na BD, odds via odds.json.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import time
import datetime as dt

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    import asyncio
    import httpx
except ImportError:
    httpx = None

try:
    from rapidfuzz import fuzz, process as fuzzproc   # fork mantido do fuzzywuzzy (MIT, mais rápido)
except ImportError:
    fuzz = fuzzproc = None

CFG = {
    "w": 90.0, "pet": 0.50, "ev_min": 0.05, "cooldown": 300, "nmax": 10,
    "db": "quant_desk.db",
    "baseline": {"home": 1.40, "away": 1.15},     # fallback quando nenhuma fonte devolve dados
    "calib_min_n": 30, "calib_clamp": (0.85, 1.15),
}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
      "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"}

FACT = [math.factorial(k) for k in range(CFG["nmax"] + 1)]

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_DEFAULTS = {
    "agente1": "mistralai/mistral-large-2-instruct",
    "agente2": "cohere/command-r-plus-08-2024",
    "agente3": "qwen/qwen2-72b-instruct",
    "agente4": "nvidia/nemotron-4-340b-instruct",
    "agente5": "meta/llama-3.1-405b-instruct",
}

CALIB_ATIVA_DEFAULT = """1) +0.20 lambda/equipa em pre-eliminatorias de julho; Under em Q1 so com odd >= fair x1.10.
2) Premio clean-sheet (+10% P CS) so para equipas em ritmo competitivo (epoca/torneio a decorrer).
3) Choque-de-mercado: desvio >= 6 p.p. modelo-mercado -> mover 50% em direcao ao mercado.
4) -15% xG bruto em knockout apenas se o adversario nao for defesa erratica (>=1 erro-que-gera-remate/jogo anula o corte)."""


def montar_dossier(nome, liga, hora, lam, fontes_lam, odds_pre, notas, calib):
    linhas = [f"JOGO: {nome} | COMPETIÇÃO: {liga} | HORA UTC: {hora}", ""]
    if lam:
        linhas.append(f"λ IMPLÍCITOS DO MERCADO (Poisson invertido do consenso devigado): "
                      f"casa {lam[0]} | fora {lam[1]} (origem: {fontes_lam})")
    if odds_pre:
        linhas.append("ODDS AGREGADAS (the-odds-api; melhor/mediana): " +
                      "; ".join(f"{k} {v['best']:.2f}/{v['med']:.2f}" for k, v in odds_pre.items()))
    else:
        linhas.append("ODDS AGREGADAS: indisponíveis nesta execução.")
    faltam = "FootyStats, TotalCorner, Betdiary, Forebet, KickForm, xGscore"
    if "understat" not in (fontes_lam or ""):
        faltam += ", Understat"
    linhas.append(f"FONTES INDISPONÍVEIS NESTA EXECUÇÃO (tratar como lacuna; usar média da liga): {faltam}")
    linhas.append(f"NOTAS DO UTILIZADOR (lesões/onzes/contexto): {(notas or '').strip() or 'nenhumas'}")
    linhas.append(f"CALIBRAÇÃO ATIVA (obrigatória na modelação): {(calib or '').strip()}")
    return "\n".join(linhas)


ESPN_LEAGUES = ["fifa.world", "uefa.champions", "uefa.europa", "uefa.europa.conf",
                "eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "por.1", "ned.1", "tur.1",
                "sco.1", "bel.1", "usa.1", "bra.1", "arg.1", "nor.1", "swe.1", "jpn.1"]

MARKET_LABELS = {
    "home": "Vitória Casa", "draw": "Empate", "away": "Vitória Fora",
    "qh": "Casa qualifica-se", "qa": "Fora qualifica-se",
    "over25": "Over 2.5", "under25": "Under 2.5",
    "btts_yes": "BTTS Sim", "btts_no": "BTTS Não",
    "wtn_home": "Casa vence a zero", "wtn_away": "Fora vence a zero",
}
MARKET_GROUP = {"home": "1x2", "draw": "1x2", "away": "1x2", "qh": "qualif", "qa": "qualif",
                "over25": "totais", "under25": "totais", "btts_yes": "btts", "btts_no": "btts",
                "wtn_home": "wtn", "wtn_away": "wtn"}


# ════════════════════════════════ BASE DE DADOS ════════════════════════════════

class Database:
    def __init__(self, path: str = CFG["db"]):
        self.path = path
        with self._c() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS predictions(
                id INTEGER PRIMARY KEY, data TEXT, match_id TEXT, jogo TEXT, liga TEXT,
                prior_lambda_home REAL, prior_lambda_away REAL,
                p_home REAL, p_draw REAL, p_away REAL, p_over25 REAL, p_btts REAL,
                odds_sugeridas TEXT, fontes TEXT, criado TEXT);
            CREATE TABLE IF NOT EXISTS live_alerts(
                id INTEGER PRIMARY KEY, data TEXT, match_id TEXT, match_url TEXT, jogo TEXT,
                minuto INTEGER, placar TEXT, xg_live TEXT, mercado TEXT,
                prob_modelo REAL, odd_live REAL, odd_justa REAL, ev REAL,
                resultado_final TEXT, pnl REAL, settled INTEGER DEFAULT 0, criado TEXT);
            CREATE TABLE IF NOT EXISTS calibration(
                grupo TEXT PRIMARY KEY, fator REAL, n INTEGER, atualizado TEXT);
            CREATE TABLE IF NOT EXISTS config(
                chave TEXT PRIMARY KEY, valor TEXT);
            CREATE TABLE IF NOT EXISTS agent_predictions(
                id INTEGER PRIMARY KEY, data TEXT, jogo TEXT, odds_pt TEXT,
                fair_json TEXT, ev_json TEXT, sintese TEXT,
                resultado_real TEXT, settled INTEGER DEFAULT 0, criado TEXT);
            """)

    def _c(self):
        return sqlite3.connect(self.path)

    def save_predictions(self, rows: list[dict]):
        with self._c() as c:
            for r in rows:
                c.execute("""INSERT INTO predictions(data,match_id,jogo,liga,prior_lambda_home,
                    prior_lambda_away,p_home,p_draw,p_away,p_over25,p_btts,odds_sugeridas,fontes,criado)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["data"], r["match_id"], r["jogo"], r["liga"], r["lh"], r["la"],
                     r["p_home"], r["p_draw"], r["p_away"], r["p_over25"], r["p_btts"],
                     r.get("odds_sugeridas", ""), r.get("fontes", ""), dt.datetime.now().isoformat()))

    def insert_alert(self, r: dict):
        with self._c() as c:
            c.execute("""INSERT INTO live_alerts(data,match_id,match_url,jogo,minuto,placar,xg_live,
                mercado,prob_modelo,odd_live,odd_justa,ev,settled,criado)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (r["data"], r["match_id"], r["match_url"], r["jogo"], r["minuto"], r["placar"],
                 r["xg_live"], r["mercado"], r["prob"], r["odd_live"], r["odd_justa"], r["ev"],
                 dt.datetime.now().isoformat()))

    def alerts_df(self) -> pd.DataFrame:
        with self._c() as c:
            return pd.read_sql("SELECT * FROM live_alerts ORDER BY id DESC", c)

    def open_alerts(self) -> list[tuple]:
        with self._c() as c:
            return c.execute("SELECT id, match_url, mercado FROM live_alerts WHERE settled=0").fetchall()

    def settle(self, alert_id: int, resultado: str, pnl):
        with self._c() as c:
            c.execute("UPDATE live_alerts SET resultado_final=?, pnl=?, settled=1 WHERE id=?",
                      (resultado, pnl, alert_id))

    def calibration(self) -> dict:
        with self._c() as c:
            return {g: f for g, f, *_ in c.execute("SELECT grupo,fator,n FROM calibration")}

    def save_agent_pred(self, jogo: str, odds_pt: str, fair_json: str, ev_json: str, sintese: str):
        with self._c() as c:
            c.execute("INSERT INTO agent_predictions(data,jogo,odds_pt,fair_json,ev_json,sintese,"
                      "settled,criado) VALUES(?,?,?,?,?,?,0,?)",
                      (str(dt.date.today()), jogo, odds_pt, fair_json, ev_json, sintese,
                       dt.datetime.now().isoformat()))

    def agent_preds_abertas(self) -> list[tuple]:
        with self._c() as c:
            return c.execute("SELECT id, data, jogo, sintese FROM agent_predictions "
                             "WHERE settled=0 ORDER BY id DESC LIMIT 10").fetchall()

    def settle_agent_pred(self, pid: int, resultado: str):
        with self._c() as c:
            c.execute("UPDATE agent_predictions SET resultado_real=?, settled=1 WHERE id=?",
                      (resultado, pid))

    def config_all(self) -> dict:
        with self._c() as c:
            return dict(c.execute("SELECT chave, valor FROM config").fetchall())

    def set_config(self, valores: dict):
        with self._c() as c:
            for k, v in valores.items():
                c.execute("INSERT INTO config(chave,valor) VALUES(?,?) "
                          "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (k, v or ""))

    def set_calibration(self, grupo: str, fator: float, n: int):
        with self._c() as c:
            c.execute("INSERT INTO calibration(grupo,fator,n,atualizado) VALUES(?,?,?,?) "
                      "ON CONFLICT(grupo) DO UPDATE SET fator=excluded.fator, n=excluded.n, "
                      "atualizado=excluded.atualizado",
                      (grupo, fator, n, dt.datetime.now().isoformat()))


# ════════════════════════════════ MOTOR MATEMÁTICO ════════════════════════════════

class MathEngine:
    """Lambda revelado (Bayes) -> lambda restante (game state) -> Poisson -> mercados."""

    def __init__(self, calib: dict | None = None):
        self.calib = calib or {}

    @staticmethod
    def lambda_revelado(prior: float, xg_obs: float, minuto: int, w: float = CFG["w"]) -> float:
        return (w * prior + 90.0 * xg_obs) / (w + minuto)

    @staticmethod
    def mult_estado(meus: int, adv: int) -> float:
        return 1.20 if meus < adv else 0.85 if meus > adv else 1.0

    @staticmethod
    def lambda_restante(rev: float, minuto: int, mult: float) -> float:
        return rev * ((90.0 - minuto) / 90.0) * mult

    @staticmethod
    def _pois(l: float, k: int) -> float:
        return math.exp(-l) * l ** k / FACT[k]

    def market_probs(self, rem_h: float, rem_a: float, gh: int = 0, ga: int = 0,
                     pet: float = CFG["pet"]) -> dict:
        n = CFG["nmax"]
        p = {"home": 0.0, "draw": 0.0, "away": 0.0, "over25": 0.0,
             "btts_yes": 0.0, "wtn_home": 0.0, "wtn_away": 0.0}
        tot = 0.0
        for i in range(n + 1):
            pi = self._pois(rem_h, i)
            for j in range(n + 1):
                pij = pi * self._pois(rem_a, j)
                tot += pij
                fh, fa = gh + i, ga + j
                if fh > fa:
                    p["home"] += pij
                    if fa == 0:
                        p["wtn_home"] += pij
                elif fh == fa:
                    p["draw"] += pij
                else:
                    p["away"] += pij
                    if fh == 0:
                        p["wtn_away"] += pij
                if fh + fa > 2.5:
                    p["over25"] += pij
                if fh > 0 and fa > 0:
                    p["btts_yes"] += pij
        for k in p:
            p[k] /= tot
        p["under25"] = 1.0 - p["over25"]
        p["btts_no"] = 1.0 - p["btts_yes"]
        p["qh"] = p["home"] + p["draw"] * pet
        p["qa"] = p["away"] + p["draw"] * (1.0 - pet)
        return self._apply_calibration(p)

    def _apply_calibration(self, p: dict) -> dict:
        """Auto-aprendizagem: fator por grupo vindo do histórico (limitado a ±15%)."""
        if not self.calib:
            return p
        q = dict(p)
        for k in q:
            f = self.calib.get(MARKET_GROUP.get(k, ""), 1.0)
            q[k] = min(0.99, max(1e-4, q[k] * f))
        s = q["home"] + q["draw"] + q["away"]
        for k in ("home", "draw", "away"):
            q[k] /= s
        q["under25"] = 1.0 - q["over25"]
        q["btts_no"] = 1.0 - q["btts_yes"]
        return q

    @staticmethod
    def fair(p: float):
        return round(1.0 / p, 2) if p > 1e-4 else None

    @staticmethod
    def ev(p: float, odd: float) -> float:
        return p * odd - 1.0

    @staticmethod
    def implied_lambdas(p1: float, p2: float, p_over25: float | None = None) -> tuple[float, float]:
        """Inverte o Poisson: total via Over2.5 (bisseccao), supremacia via P(1)-P(2)."""
        def p_ge3(lt):
            return 1 - math.exp(-lt) * (1 + lt + lt * lt / 2)
        if p_over25 and 0.02 < p_over25 < 0.98:
            lo, hi = 0.4, 6.0
            for _ in range(50):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if p_ge3(mid) < p_over25 else (lo, mid)
            lt = (lo + hi) / 2
        else:
            lt = 2.6
        e0 = MathEngine()
        lim = min(2.4, lt - 0.1)
        lo, hi = -lim, lim
        for _ in range(50):
            s = (lo + hi) / 2
            lh, la = max(0.05, (lt + s) / 2), max(0.05, (lt - s) / 2)
            pr = e0.market_probs(lh, la)
            if (pr["home"] - pr["away"]) < (p1 - p2):
                lo = s
            else:
                hi = s
        s = (lo + hi) / 2
        return round(max(0.05, (lt + s) / 2), 2), round(max(0.05, (lt - s) / 2), 2)


class BiasCalibrator:
    """Le o historico liquidado e recalcula fatores por grupo de mercado.
    fator = frequencia_real / probabilidade_media_do_modelo, com clamp e n minimo."""

    @staticmethod
    def recompute(db: Database) -> dict:
        df = db.alerts_df()
        df = df[(df.settled == 1) & df.resultado_final.isin(["ganhou", "perdeu"])]
        out = {}
        if df.empty:
            return out
        df["grupo"] = df.mercado.map(MARKET_GROUP)
        lo, hi = CFG["calib_clamp"]
        for g, sub in df.groupby("grupo"):
            n = len(sub)
            if n < CFG["calib_min_n"]:
                continue
            realized = (sub.resultado_final == "ganhou").mean()
            expected = sub.prob_modelo.mean()
            if expected <= 0:
                continue
            f = max(lo, min(hi, realized / expected))
            db.set_calibration(g, round(f, 4), n)
            out[g] = f
        return out


# ════════════════════════════════ SCRAPER (registo de fontes) ════════════════════════════════

class DataScraper:
    """FotMob (fixtures + live) e Understat implementados; restantes fontes sao stubs
    com fallback — o consenso usa o que responder e nunca quebra a app."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    # ---------- utilitarios ----------
    def _get(self, url: str) -> str:
        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        return r.text

    def _next_data(self, url: str) -> dict:
        """Exemplo funcional de parsing da tag __NEXT_DATA__ (FotMob) com BS4 + json.loads."""
        tag = BeautifulSoup(self._get(url), "html.parser").find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            raise RuntimeError("__NEXT_DATA__ nao encontrado em " + url)
        return json.loads(tag.string)

    @staticmethod
    def walk(o):
        if isinstance(o, dict):
            yield o
            for v in o.values():
                yield from DataScraper.walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from DataScraper.walk(v)

    # ---------- Fixtures do dia (a lista do FotMob é hidratada no cliente via API
    # assinada — não é server-rendered; usa-se football-data.org e/ou ESPN) ----------
    def _football_data(self, d: dt.date, key: str) -> list[dict]:
        r = self.s.get("https://api.football-data.org/v4/matches", timeout=20,
                       headers={"X-Auth-Token": key},
                       params={"dateFrom": d.isoformat(),
                               "dateTo": (d + dt.timedelta(days=1)).isoformat()})
        r.raise_for_status()
        out = []
        for m in r.json().get("matches", []):
            if not str(m.get("utcDate", "")).startswith(d.isoformat()):
                continue
            st_ = m.get("status", "")
            out.append({"match_id": str(m.get("id")),
                        "liga": (m.get("competition") or {}).get("name", "?"),
                        "home": (m.get("homeTeam") or {}).get("name", "?"),
                        "away": (m.get("awayTeam") or {}).get("name", "?"),
                        "hora_utc": str(m.get("utcDate", ""))[11:16],
                        "started": st_ in ("IN_PLAY", "PAUSED", "FINISHED"),
                        "finished": st_ == "FINISHED", "url": ""})
        return out

    def _espn(self, d: dt.date, ligas: list[str] | None = None) -> list[dict]:
        jogos, vistos = [], set()
        for lg in ["all"] + list(ligas or ESPN_LEAGUES):
            try:
                r = self.s.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/scoreboard",
                               params={"dates": d.strftime("%Y%m%d"), "limit": 400}, timeout=20)
                r.raise_for_status()
                data = r.json()
            except Exception:
                continue
            topo = data.get("leagues") or []
            liga_por_id = {}
            for l in topo:
                nome_l = l.get("name") or l.get("shortName") or ""
                for ident in {str(l.get("id", "")),
                              str(l.get("uid", "")).split("l:")[-1].split("~")[0]}:
                    if ident and nome_l:
                        liga_por_id[ident] = nome_l
            liga_topo = topo[0].get("name") if (len(topo) == 1 and lg != "all") else None
            for ev in data.get("events", []):
                try:
                    comp = ev["competitions"][0]
                    casa = next(c for c in comp["competitors"] if c.get("homeAway") == "home")
                    fora = next(c for c in comp["competitors"] if c.get("homeAway") == "away")
                    estado = (ev.get("status") or {}).get("type", {}) or {}
                    mid = str(ev.get("id"))
                    if mid in vistos:
                        continue
                    vistos.add(mid)
                    lg_ev = ev.get("league")
                    liga = (lg_ev.get("name") if isinstance(lg_ev, dict) else
                            lg_ev if isinstance(lg_ev, str) else None)
                    if not liga:
                        uid = str(ev.get("uid", ""))
                        if "l:" in uid:
                            liga = liga_por_id.get(uid.split("l:")[-1].split("~")[0])
                    liga = liga or liga_topo or ("?" if lg == "all" else lg)
                    jogos.append({"match_id": mid, "liga": liga,
                                  "home": casa["team"]["displayName"],
                                  "away": fora["team"]["displayName"],
                                  "hora_utc": str(ev.get("date", ""))[11:16],
                                  "started": estado.get("state") in ("in", "post"),
                                  "finished": estado.get("state") == "post", "url": ""})
                except Exception:
                    continue
            if lg == "all" and jogos:
                break
        return jogos

    @staticmethod
    def _mesmo_jogo(a: dict, b: dict) -> bool:
        if a["hora_utc"] != b["hora_utc"] or not fuzzproc:
            return False
        if fuzz.token_set_ratio(a["liga"], b["liga"]) < 70:
            return False
        return max(fuzz.token_set_ratio(a["home"], b["home"]),
                   fuzz.token_set_ratio(a["away"], b["away"])) >= 88

    def fixtures_today(self, date: dt.date | None = None, ligas: list[str] | None = None,
                       fd_key: str | None = None) -> tuple[list[dict], str]:
        """Funde football-data.org (oficial, ~12 competições no free tier) com a ESPN
        (cobertura larga) e dedupica — nenhuma fonte curto-circuita a outra."""
        d = date or dt.date.today()
        key = (fd_key or "").strip() or os.getenv("FOOTBALL_DATA_KEY")
        fontes, jogos = [], []
        if key:
            try:
                jogos = self._football_data(d, key)
                if jogos:
                    fontes.append(f"football-data.org ({len(jogos)})")
            except Exception:
                pass
        try:
            extra = self._espn(d, ligas)
        except Exception:
            extra = []
        novos = [j for j in extra if not any(self._mesmo_jogo(j, x) for x in jogos)]
        if novos:
            fontes.append(f"ESPN ({len(novos)})")
        jogos += novos
        if not jogos:
            raise RuntimeError("nenhuma fonte de fixtures respondeu "
                               "(verifica FOOTBALL_DATA_KEY, a lista de ligas ESPN e a rede)")
        jogos.sort(key=lambda j: (j.get("hora_utc") or "99:99"))
        return jogos, " + ".join(fontes)

    # ---------- FotMob: live ----------
    def fotmob_live(self, url: str):
        data = self._next_data(url.split("#")[0])
        header = next((d for d in self.walk(data)
                       if isinstance(d.get("teams"), list) and len(d["teams"]) == 2
                       and "status" in d and all(isinstance(t, dict) and "score" in t for t in d["teams"])), None)
        if not header:
            raise RuntimeError("cabecalho do jogo nao encontrado")
        names = [header["teams"][0].get("name", "Casa"), header["teams"][1].get("name", "Fora")]
        score = [int(header["teams"][0].get("score") or 0), int(header["teams"][1].get("score") or 0)]
        st_ = header.get("status", {})
        if st_.get("finished"):
            minuto = 90
        elif not st_.get("started"):
            minuto = 0
        else:
            short = str((st_.get("liveTime") or {}).get("short", ""))
            m = re.search(r"(\d+)", short)
            minuto = min(90, int(m.group(1))) if m else (45 if "HT" in short.upper() else 1)
        xg = None
        for d in self.walk(data):
            if str(d.get("key", "")).lower() == "expected_goals" or \
               "expected goals" in str(d.get("title", "")).lower() or \
               "golos esperados" in str(d.get("title", "")).lower():
                s = d.get("stats")
                if isinstance(s, list) and len(s) == 2:
                    try:
                        xg = [float(str(s[0]).replace(",", ".")), float(str(s[1]).replace(",", "."))]
                        break
                    except (TypeError, ValueError):
                        pass
        return names, score, minuto, xg

    # ---------- Understat (so ligas de clubes big-5 + RUS) ----------
    def understat_team(self, team: str, league: str = "EPL", season: int | None = None) -> dict | None:
        season = season or (dt.date.today().year - (0 if dt.date.today().month >= 8 else 1))
        try:
            html = self._get(f"https://understat.com/league/{league}/{season}")
            m = re.search(r"teamsData\s*=\s*JSON\.parse\('(.*?)'\)", html)
            if not m:
                return None
            data = json.loads(m.group(1).encode("utf-8").decode("unicode_escape"))
            nomes = {v["title"]: v for v in data.values()}
            if fuzzproc:
                hit = fuzzproc.extractOne(team, list(nomes), scorer=fuzz.token_set_ratio, score_cutoff=75)
                if not hit:
                    return None
                hist = nomes[hit[0]]["history"][-5:]
            else:
                if team not in nomes:
                    return None
                hist = nomes[team]["history"][-5:]
            if not hist:
                return None
            return {"xg5": sum(float(h["xG"]) for h in hist) / len(hist),
                    "xga5": sum(float(h["xGA"]) for h in hist) / len(hist)}
        except Exception:
            return None

    # ---------- soccerdata / FBref (opcional, pesado) ----------
    def fbref_team(self, team: str, league: str = "Big 5 European Leagues Combined") -> dict | None:
        try:
            import soccerdata as sd
            fb = sd.FBref(leagues=league, seasons=dt.date.today().year - 1)
            stats = fb.read_team_season_stats(stat_type="shooting")
            idx = stats.index.get_level_values("team")
            alvo = fuzzproc.extractOne(team, list(idx), scorer=fuzz.token_set_ratio, score_cutoff=80) if fuzzproc else None
            if not alvo:
                return None
            row = stats.xs(alvo[0], level="team").iloc[0]
            jogos = float(row.get(("Standard", "Gls"), 0)) / max(1e-6, float(row.get(("Expected", "xG"), 1)))
            return {"finishing_eff": jogos}
        except Exception:
            return None

    # ---------- stubs documentados (fontes com Cloudflare/ToS restritivos) ----------
    def footystats(self, *a):  # BTTS/cantos — API oficial paga; scraping bloqueado por CF
        return None

    def forebet(self, *a):     # Poisson nativo/clima — anti-bot agressivo
        return None

    def betdiary(self, *a):    # dropping odds — sem HTML server-rendered estavel
        return None

    def totalcorner(self, *a):
        return None

    def xgscore(self, *a):
        return None

    def kickform(self, *a):
        return None

    def consenso_tipsters(self, *a):  # PredictZ/Betensured — apenas validacao, nao implementado
        return None

    # ---------- consenso do prior ----------
    def build_prior(self, home: str, away: str, liga: str, overrides: dict | None = None) -> dict:
        """Media ponderada das fontes que responderem; fallback = baseline da competicao."""
        if overrides and overrides.get("lh") and overrides.get("la"):
            return {"lh": overrides["lh"], "la": overrides["la"], "fontes": "manual"}
        fontes, pesos_lh, pesos_la = [], [], []
        u_map = {"Premier League": "EPL", "English Premier League": "EPL",
                 "LaLiga": "La_liga", "Spanish LaLiga": "La_liga",
                 "Serie A": "Serie_A", "Italian Serie A": "Serie_A",
                 "Bundesliga": "Bundesliga", "German Bundesliga": "Bundesliga",
                 "Ligue 1": "Ligue_1", "French Ligue 1": "Ligue_1"}
        lg_u = None
        if fuzzproc:
            hit = fuzzproc.extractOne(liga, list(u_map), scorer=fuzz.token_set_ratio, score_cutoff=80)
            lg_u = u_map[hit[0]] if hit else None
        else:
            lg_u = u_map.get(liga)
        if lg_u:
            uh, ua = self.understat_team(home, lg_u), self.understat_team(away, lg_u)
            if uh and ua:
                pesos_lh.append(((uh["xg5"] + ua["xga5"]) / 2 * 1.05, 0.6))   # +5% casa
                pesos_la.append(((ua["xg5"] + uh["xga5"]) / 2 * 0.95, 0.6))
                fontes.append("understat")
        base = CFG["baseline"]
        pesos_lh.append((base["home"], 0.4 if fontes else 1.0))
        pesos_la.append((base["away"], 0.4 if fontes else 1.0))
        fontes.append("baseline")
        lh = sum(v * w for v, w in pesos_lh) / sum(w for _, w in pesos_lh)
        la = sum(v * w for v, w in pesos_la) / sum(w for _, w in pesos_la)
        return {"lh": round(lh, 2), "la": round(la, 2), "fontes": "+".join(fontes)}


# ════════════════════════════════ ODDS & ALERTAS ════════════════════════════════

class OddsProvider:
    def __init__(self, api_key: str | None, sport: str, odds_file: str = "odds.json",
                 cache: dict | None = None):
        self.key, self.sport, self.file = api_key, sport, odds_file
        self.cache = cache if cache is not None else {}
        self.s = requests.Session()
        self.s.headers.update(UA)

    def get(self, home: str, away: str) -> dict:
        if not self.key:
            try:
                with open(self.file) as f:
                    return {k: float(v) for k, v in json.load(f).items() if float(v) > 1.0}
            except Exception:
                return {}
        try:
            r = self.s.get(f"https://api.the-odds-api.com/v4/sports/{self.sport}/odds", timeout=15,
                           params={"apiKey": self.key, "regions": "eu", "markets": "h2h,totals",
                                   "oddsFormat": "decimal"})
            r.raise_for_status()
            eventos = r.json()
        except Exception:
            return {}
        alvo = None
        if fuzzproc and eventos:
            nomes = [f'{e.get("home_team","")} vs {e.get("away_team","")}' for e in eventos]
            hit = fuzzproc.extractOne(f"{home} vs {away}", nomes, scorer=fuzz.token_set_ratio, score_cutoff=70)
            if hit:
                alvo = eventos[hit[2]]
        if not alvo:
            return {}
        best = {}

        def keep(k, v):
            if v and v > 1.0 and v > best.get(k, 0):
                best[k] = v
        for bk in alvo.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for out in mk.get("outcomes", []):
                    nm, price = out.get("name", ""), out.get("price")
                    if mk["key"] == "h2h":
                        if nm == alvo["home_team"]:
                            keep("home", price)
                        elif nm == alvo["away_team"]:
                            keep("away", price)
                        elif nm == "Draw":
                            keep("draw", price)
                    elif mk["key"] == "totals" and out.get("point") == 2.5:
                        keep("over25" if nm == "Over" else "under25", price)
        return best

    # ---------- pre-jogo: consenso devigado + melhor preco ----------
    def _sports(self) -> list[dict]:
        if self.cache.get("sports") is None:
            try:
                r = self.s.get("https://api.the-odds-api.com/v4/sports/",
                               params={"apiKey": self.key}, timeout=15)
                r.raise_for_status()
                self.cache["sports"] = [s for s in r.json()
                                        if s.get("group") == "Soccer" and s.get("active")]
            except Exception:
                self.cache["sports"] = []
        return self.cache["sports"]

    def sport_key_for(self, liga: str):
        if not liga or liga in ("all", "?"):
            return None
        sports = self._sports()
        if not (sports and fuzzproc):
            return None
        hit = fuzzproc.extractOne(liga, [s["title"] for s in sports],
                                  scorer=fuzz.token_set_ratio, score_cutoff=70)
        return sports[hit[2]]["key"] if hit else None

    def _league_events(self, sport_key: str) -> list:
        eventos = self.cache.setdefault("events", {})
        if sport_key not in eventos:
            try:
                r = self.s.get(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds", timeout=20,
                               params={"apiKey": self.key, "regions": "eu",
                                       "markets": "h2h,totals", "oddsFormat": "decimal"})
                r.raise_for_status()
                eventos[sport_key] = r.json()
            except Exception:
                eventos[sport_key] = []
        return eventos[sport_key]

    def prematch(self, liga: str, home: str, away: str) -> dict:
        """{mercado: {'best': x, 'med': y}} para h2h e totais 2.5. {} sem key/sem match."""
        if not (self.key and fuzzproc):
            return {}
        sk = self.sport_key_for(liga)
        if not sk:
            return {}
        eventos = self._league_events(sk)
        if not eventos:
            return {}
        nomes = [f'{e.get("home_team","")} vs {e.get("away_team","")}' for e in eventos]
        hit = fuzzproc.extractOne(f"{home} vs {away}", nomes,
                                  scorer=fuzz.token_set_ratio, score_cutoff=70)
        if not hit:
            return {}
        alvo = eventos[hit[2]]
        precos: dict[str, list] = {}
        for bk in alvo.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for out in mk.get("outcomes", []):
                    nm, price = out.get("name", ""), out.get("price")
                    if not price or price <= 1.0:
                        continue
                    k = None
                    if mk["key"] == "h2h":
                        k = ("home" if nm == alvo["home_team"] else
                             "away" if nm == alvo["away_team"] else
                             "draw" if nm == "Draw" else None)
                    elif mk["key"] == "totals" and out.get("point") == 2.5:
                        k = "over25" if nm == "Over" else "under25"
                    if k:
                        precos.setdefault(k, []).append(price)
        return {k: {"best": max(v), "med": statistics.median(v)} for k, v in precos.items()}


SUFIXO_ANTI_ALUCINACAO = ("\n\nREGRA DURA: se uma fonte não constar do DOSSIER fornecido, "
                          "declara-a como INDISPONÍVEL e nunca inventes números para ela.")

AGENT_SYSTEMS = {
    "agente1": """Atua como Analista de Dados Desportivos de Elite, 100% objetivo.
Processa soccerdata/FotMob/FootyStats/Understat: xG/xGA/xPTS (5 jogos), PPDA, passes progressivos, set-pieces, xG tempo real, BTTS, Cantos, conversion rate elite.
Declara lacunas explicitamente e usa média da liga.
Saída OBRIGATÓRIA:
## Métricas Processadas
- lista bullet
## Lacunas de Dados
## Resumo Frio (3 frases máx.)
Não prevejas.""",
    "agente2": """Atua como Analista Tático de Elite, 100% frio. Cruza Agente 1 com FotMob (line-ups/lesões), TotalCorner (momentum), relatórios jogadores. Avalia impacto tático real, motivação e ameaças (set-pieces/contra-ataque).
Saída OBRIGATÓRIA:
## Impacto Tático
## Fatores Decisivos
Ignora hype.""",
    "agente3": """Estatístico Matemático. Modela Poisson + Elo (Forebet/KickForm).
OBRIGATÓRIO aplicar calibrações:
- Knockout: +25% finishing elite, -15% xG bruto
- Variância: +20% se |xG diff| <1.0
- Cartões: +15% variância Poisson
- Over bias se xG total >2.8-3.0 + ameaças
- Baseline knockout: +0.15-0.25 xG/equipa
Calcula Fair Odds ajustadas.
Saída OBRIGATÓRIA:
## Probabilidades Reais (Fair Odds)
1: % | X: % | 2: %
Over 2.5: % | Under: %
## Calibrações Aplicadas
No FIM, inclui SEMPRE um bloco de código json exatamente neste formato:
```json
{"p1": 0.00, "px": 0.00, "p2": 0.00, "over25": 0.00}
```""",
    "agente4": """Gestor de Risco. Cruza Fair Odds com Betdiary (dropping >10%), xGscore, consenso (validação). Deteta EV+ ou "No Bet".
Saída OBRIGATÓRIA:
## Comparação Odds
## Value Bets / EV+
## Decisão Final""",
    "agente5": """Juiz Principal com auto-correção rigorosa.
FASE 0 OBRIGATÓRIA (template exato):
- Jogo: [ ] | Real: [ ] | Previsão anterior: [ ]
- Diagnóstico Técnico: [ ]
- Lição + Ajuste Quantitativo: [ ]
Resumo Calibração Ativa no fim.
Depois SÍNTESE FINAL com relatórios 1-4 + Fase 0.
Saída OBRIGATÓRIA:
## FASE 0 - Auditoria
[template + resumo calibração]
## SÍNTESE FINAL
Previsão: ... | EV+: % | Aposta: [ ] ou "No Bet" | Confiança: N/10 | Justificativa: [2 frases]
NOTA: a secção VERIFICAÇÃO DETERMINÍSTICA do prompt foi calculada em Python e prevalece sobre qualquer EV que os relatórios afirmem.""",
}


class AgentCouncil:
    """Agentes 1-4 em paralelo (NVIDIA NIM) -> verificação determinística de EV -> Agente 5."""

    def __init__(self, api_key: str, modelos: dict):
        self.key = (api_key or "").strip()
        self.modelos = modelos

    async def _um(self, client, modelo: str, system: str, user: str) -> str:
        r = await client.post(NIM_URL, json={
            "model": modelo, "temperature": 0.25, "max_tokens": 800,
            "messages": [{"role": "system", "content": system + SUFIXO_ANTI_ALUCINACAO},
                         {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _paralelo(self, dossier: str) -> list[str]:
        async with httpx.AsyncClient(timeout=90,
                                     headers={"Authorization": f"Bearer {self.key}"}) as c:
            tarefas = [self._um(c, self.modelos[f"agente{i}"], AGENT_SYSTEMS[f"agente{i}"], dossier)
                       for i in range(1, 5)]
            res = await asyncio.gather(*tarefas, return_exceptions=True)
        return [r if isinstance(r, str) else f"AGENTE {i+1} INDISPONÍVEL: {r}"
                for i, r in enumerate(res)]

    @staticmethod
    def extrair_probs(texto_agente3: str) -> dict | None:
        m = re.search(r'\{[^{}]*"p1"[\s\S]*?\}', texto_agente3)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            p = {k: float(d[k]) for k in ("p1", "px", "p2", "over25")}
            if p["p1"] > 1:  # veio em percentagem
                p = {k: v / 100 for k, v in p.items()}
            s = p["p1"] + p["px"] + p["p2"]
            if not 0.9 < s < 1.1:
                return None
            for k in ("p1", "px", "p2"):
                p[k] /= s
            return p
        except Exception:
            return None

    @staticmethod
    def verificacao_ev(probs: dict | None, odds: dict) -> tuple[str, dict]:
        if not probs:
            return ("Agente 3 não devolveu JSON válido — EV NÃO verificável; "
                    "por regra, sem verificação = No Bet."), {}
        pares = [("1", probs["p1"], odds.get("h")), ("X", probs["px"], odds.get("d")),
                 ("2", probs["p2"], odds.get("a")), ("Over 2.5", probs["over25"], odds.get("o25")),
                 ("Under 2.5", 1 - probs["over25"], odds.get("u25"))]
        linhas, evj = [], {}
        for lab, p, odd in pares:
            if odd and odd > 1 and p > 1e-4:
                ev = p * odd - 1
                evj[lab] = round(ev, 4)
                linhas.append(f"{lab}: fair {1/p:.2f} vs mercado {odd:.2f} -> EV {ev*100:+.1f}%"
                              + ("  [EV+]" if ev >= 0.05 else ""))
        txt = ("=== VERIFICAÇÃO DETERMINÍSTICA (Python; prevalece sobre os relatórios) ===\n"
               + "\n".join(linhas)
               + "\nRegra: EV+ exige mercado > fair (p*odd-1 >= +5%).")
        return txt, evj

    def julgar(self, data_hoje: str, fase0: str, jogo: str, odds_txt: str,
               rels: list[str], verif: str) -> str:
        user = (f"DATA: {data_hoje}\n=== FASE 0 (ONTEM) ===\n{fase0 or 'Sem previsões anteriores registadas.'}\n"
                f"=== JOGO HOJE ===\nJogo: {jogo} | Odds PT: {odds_txt}\n"
                + "".join(f"\n--- RELATÓRIO {i+1} ---\n{r}\n" for i, r in enumerate(rels))
                + f"\n{verif}\n\nExecuta FASE 0 (template) + SÍNTESE FINAL (formato acima).")
        async def _run():
            async with httpx.AsyncClient(timeout=120,
                                         headers={"Authorization": f"Bearer {self.key}"}) as c:
                return await self._um(c, self.modelos["agente5"], AGENT_SYSTEMS["agente5"], user)
        return asyncio.run(_run())

    def correr_analistas(self, dossier: str) -> list[str]:
        return asyncio.run(self._paralelo(dossier))


class Notifier:
    def __init__(self, token: str | None = None, chat: str | None = None):
        self.token = (token or "").strip() or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat = (chat or "").strip() or os.getenv("TELEGRAM_CHAT_ID")

    def send(self, txt: str) -> bool:
        if not (self.token and self.chat):
            return False
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": txt}, timeout=10)
            return True
        except Exception:
            return False


def formato_alerta(jogo, minuto, placar, xg, mercado, odd, fairo, ev, metrica) -> str:
    return ("🚨 ALERTA VALUE BET QUANT (LIVE) 🚨\n"
            f"- Jogo: {jogo} | Minuto: {minuto}'\n"
            f"- Placar Atual: {placar}\n"
            f"- xG Real (Live FotMob): {xg}\n"
            f"- Mercado Sugerido: {mercado}\n"
            f"- Odd Live (Mercado): @{odd:.2f} | Odd Justa (Modelo): @{fairo:.2f}\n"
            f"- Margem (EV): {ev*100:+.1f}%\n"
            f"- Métrica Chave: {metrica}")


def settle_market(mk: str, fh: int, fa: int):
    """Liquidacao aos 90'. Qualificacao com empate fica manual (prolongamento)."""
    regras = {"home": fh > fa, "draw": fh == fa, "away": fa > fh,
              "over25": fh + fa > 2.5, "under25": fh + fa < 2.5,
              "btts_yes": fh > 0 and fa > 0, "btts_no": not (fh > 0 and fa > 0),
              "wtn_home": fh > fa and fa == 0, "wtn_away": fa > fh and fh == 0,
              "qh": True if fh > fa else (False if fa > fh else None),
              "qa": True if fa > fh else (False if fh > fa else None)}
    return regras.get(mk)


# ════════════════════════════════ UI STREAMLIT ════════════════════════════════

def setup_ui():
    import streamlit as st
    try:
        from streamlit_autorefresh import st_autorefresh
    except ImportError:
        st_autorefresh = None

    st.set_page_config(page_title="Quant Desk", layout="wide")
    st.title("Mesa de Modelo — Quant Desk")
    st.caption("Poisson · Dixon-Coles-lite · λ revelado bayesiano · EV+. Estimativas de modelo, "
               "não certezas: joga só com dinheiro de entretenimento.")

    db = Database()
    scraper = DataScraper()  # nunca guardar instâncias em session_state: sobrevivem a hot-reloads
    engine = MathEngine(calib=db.calibration())

    cfg_db = db.config_all()
    with st.sidebar:
        st.header("⚙️ Configuração")
        st.caption("Prioridade: campo abaixo > variável de ambiente. Guardadas em texto simples "
                   "no quant_desk.db local — mantém o ficheiro privado (.gitignore).")
        k_odds = st.text_input("ODDS_API_KEY", type="password",
                               value=os.getenv("ODDS_API_KEY", "") or cfg_db.get("odds_api_key", ""),
                               help="the-odds-api.com — odds pré-jogo/live, priors implícitos e EV.")
        k_fd = st.text_input("FOOTBALL_DATA_KEY", type="password",
                             value=os.getenv("FOOTBALL_DATA_KEY", "") or cfg_db.get("football_data_key", ""),
                             help="football-data.org — fixtures oficiais (fonte primária do Radar).")
        k_tg_tok = st.text_input("TELEGRAM_BOT_TOKEN", type="password",
                                 value=os.getenv("TELEGRAM_BOT_TOKEN", "") or cfg_db.get("tg_token", ""),
                                 help="Token do bot (@BotFather).")
        k_nim = st.text_input("NVIDIA_API_KEY", type="password",
                              value=os.getenv("NVIDIA_API_KEY", "") or cfg_db.get("nvidia_key", ""),
                              help="build.nvidia.com — alimenta o Conselho de Agentes (aba 🤖).")
        k_tg_chat = st.text_input("TELEGRAM_CHAT_ID",
                                  value=os.getenv("TELEGRAM_CHAT_ID", "") or cfg_db.get("tg_chat", ""),
                                  help="ID do chat/canal que recebe os alertas.")
        cb1, cb2 = st.columns(2)
        if cb1.button("Guardar chaves"):
            db.set_config({"odds_api_key": k_odds, "football_data_key": k_fd,
                           "tg_token": k_tg_tok, "tg_chat": k_tg_chat, "nvidia_key": k_nim})
            st.success("Guardadas.")
        if cb2.button("Testar Telegram"):
            ok = Notifier(k_tg_tok, k_tg_chat).send("✅ Quant Desk ligado.")
            if ok:
                st.success("Enviado — vê o Telegram.")
            else:
                st.error("Falhou: verifica token e chat_id.")

    notifier = Notifier(k_tg_tok, k_tg_chat)

    st.session_state.setdefault("odds_cache", {})
    if st.session_state.get("odds_key") != k_odds:
        st.session_state.odds_cache.clear()
        st.session_state.odds_key = k_odds
    oddsp = OddsProvider(k_odds.strip() or None, "soccer_fifa_world_cup",
                         cache=st.session_state.odds_cache)

    tab_radar, tab_live, tab_alertas, tab_hist, tab_conselho = st.tabs(
        ["📡 Radar Pré-Jogo", "🎯 Live Quant Desk", "🚨 Alertas EV+", "📚 Histórico & Auditoria",
         "🤖 Conselho de Agentes"])

    # ---------- TAB 1: RADAR ----------
    with tab_radar:
        c1, c2 = st.columns([1, 3])
        data_sel = c1.date_input("Dia", dt.date.today(),
                                 help="Dia dos jogos a carregar. O pipeline pré-jogo corre uma vez "
                                      "por clique (não em loop) e guarda os λ em session_state.")
        ligas_txt = c2.text_input("Ligas ESPN (fallback)", ",".join(ESPN_LEAGUES),
                                  help="Códigos ESPN separados por vírgula (ex: uefa.champions, por.1, "
                                       "bra.1). O scraper tenta primeiro o código 'all' (tudo do dia); "
                                       "se a ESPN não o servir, itera esta lista. Acrescenta aqui "
                                       "pré-eliminatórias/ligas que faltem.")
        if c1.button("Correr pipeline do dia", type="primary") or "radar" not in st.session_state:
            try:
                ligas = [x.strip() for x in ligas_txt.split(",") if x.strip()]
                jogos, fonte_fix = scraper.fixtures_today(data_sel, ligas, fd_key=k_fd)
                st.session_state.radar_fonte = fonte_fix
            except Exception as e:
                st.error(f"Fixtures falharam: {e}")
                jogos = []
            linhas = []
            for j in jogos:
                odds_pre = oddsp.prematch(j["liga"], j["home"], j["away"])
                if all(k in odds_pre for k in ("home", "draw", "away")):
                    inv = {k: 1.0 / odds_pre[k]["med"] for k in ("home", "draw", "away")}
                    s = sum(inv.values())
                    p1, p2 = inv["home"] / s, inv["away"] / s          # devig proporcional
                    po = None
                    if "over25" in odds_pre and "under25" in odds_pre:
                        io, iu = 1.0 / odds_pre["over25"]["med"], 1.0 / odds_pre["under25"]["med"]
                        po = io / (io + iu)
                    lh, la = MathEngine.implied_lambdas(p1, p2, po)
                    pri = {"lh": lh, "la": la, "fontes": "odds-implied"}
                else:
                    pri = scraper.build_prior(j["home"], j["away"], j["liga"])
                p = engine.market_probs(pri["lh"], pri["la"])
                melhor = None
                for mk in ("home", "draw", "away", "over25", "under25"):
                    o = odds_pre.get(mk, {}).get("best")
                    if o and p[mk] > 1e-4:
                        e_ = engine.ev(p[mk], o)
                        if melhor is None or e_ > melhor[2]:
                            melhor = (mk, o, e_)
                linhas.append({**j, **pri,
                               "p_home": p["home"], "p_draw": p["draw"], "p_away": p["away"],
                               "p_over25": p["over25"], "p_btts": p["btts_yes"],
                               "ev_mk": melhor[0] if melhor else None,
                               "ev_odd": melhor[1] if melhor else None,
                               "ev_val": melhor[2] if melhor else None,
                               "melhor_ev": (f"{MARKET_LABELS[melhor[0]]} @{melhor[1]:.2f} "
                                             f"({melhor[2]*100:+.1f}%)") if melhor else "—"})
            st.session_state.radar = linhas
            if not oddsp.key:
                st.info("Sem ODDS_API_KEY: priors caem para Understat/baseline e não há coluna de EV. "
                        "Chave grátis em the-odds-api.com transforma o Radar num screener de valor.")
        linhas = st.session_state.get("radar", [])
        if linhas:
            st.caption(f"Fixtures via {st.session_state.get('radar_fonte','?')} · {len(linhas)} jogos · "
                       "para live, cola o URL do jogo no FotMob na aba seguinte.")
            cA, cB = st.columns([1, 2])
            ocultar = cA.checkbox("Ocultar jogos já terminados", value=True)
            ordem = cB.radio("Ordenar por", ["Hora", "Melhor EV"], horizontal=True,
                             help="'Melhor EV' põe no topo o jogo com o mercado de maior valor — "
                                  "a leitura direta de 'qual a aposta'.")
            df = pd.DataFrame(linhas)
            if ocultar and "finished" in df.columns:
                df = df[~df["finished"].fillna(False)]
            if ordem == "Melhor EV" and "ev_val" in df.columns:
                df = df.sort_values("ev_val", ascending=False, na_position="last")
            vis = df[["liga", "home", "away", "hora_utc", "lh", "la",
                      "p_home", "p_draw", "p_away", "p_over25", "p_btts",
                      "melhor_ev", "fontes"]].copy()
            for col in ["p_home", "p_draw", "p_away", "p_over25", "p_btts"]:
                vis[col] = (vis[col] * 100).round(1)
            vis.columns = ["Liga", "Casa", "Fora", "UTC", "λC", "λF",
                           "P1 %", "PX %", "P2 %", "Over2.5 %", "BTTS %", "Melhor EV", "Fontes"]
            cc = {
                "UTC": st.column_config.TextColumn("UTC", help="Hora de início em UTC "
                    "(Lisboa = UTC+1 no verão, Alemanha = UTC+2)."),
                "λC": st.column_config.NumberColumn("λC", help="Golos esperados da equipa da CASA "
                    "nos 90' (prior do modelo). Ver coluna Fontes para a origem."),
                "λF": st.column_config.NumberColumn("λF", help="Golos esperados da equipa de FORA "
                    "nos 90' (prior do modelo)."),
                "P1 %": st.column_config.NumberColumn("P1 %", help="Probabilidade de vitória da casa "
                    "aos 90', via Poisson com os λ desta linha."),
                "PX %": st.column_config.NumberColumn("PX %", help="Probabilidade de empate aos 90'."),
                "P2 %": st.column_config.NumberColumn("P2 %", help="Probabilidade de vitória de fora "
                    "aos 90'."),
                "Over2.5 %": st.column_config.NumberColumn("Over2.5 %", help="Probabilidade de 3 ou "
                    "mais golos no jogo."),
                "BTTS %": st.column_config.NumberColumn("BTTS %", help="Probabilidade de ambas as "
                    "equipas marcarem."),
                "Melhor EV": st.column_config.TextColumn("Melhor EV", help="Mercado com maior valor: "
                    "MELHOR preço entre casas vs probabilidade do CONSENSO devigado (mediana). "
                    "EV positivo = há uma casa atrasada face ao mercado (sinal tipo closing-line-"
                    "value), não 'o modelo bate o mercado'."),
                "Fontes": st.column_config.TextColumn("Fontes", help="Origem dos λ: odds-implied = "
                    "invertidos do consenso de odds (o melhor); understat = xG recente (clubes big-5 "
                    "em época); baseline = média genérica — pouco informativo, ajusta à mão na aba "
                    "Live."),
            }
            st.dataframe(vis, use_container_width=True, height=520, column_config=cc)
            vals = df[df.ev_val.notna() & (df.ev_val >= CFG["ev_min"])].sort_values(
                "ev_val", ascending=False) if "ev_val" in df else pd.DataFrame()
            st.subheader("🔥 Value pré-jogo (melhor preço vs consenso devigado)")
            if not vals.empty:
                vv = vals[["liga", "home", "away", "hora_utc", "melhor_ev", "lh", "la"]].copy()
                vv.columns = ["Liga", "Casa", "Fora", "UTC", "Mercado / EV", "λC", "λF"]
                st.dataframe(vv, use_container_width=True)
                st.caption("EV = melhor preço entre casas vs probabilidade do consenso devigado "
                           "(mediana). Sinal do tipo closing-line-value: uma casa atrasada face ao "
                           "mercado. Confirma a odd antes de fechar — pré-jogo mexe ao minuto.")
            else:
                st.caption(f"Nenhum mercado com EV ≥ {CFG['ev_min']:.0%} nas odds recolhidas.")
            with st.expander("🔧 Diagnóstico de fontes"):
                sem_odds = sorted({l["liga"] for l in linhas if l.get("fontes") == "baseline"})
                st.write({"fixtures": st.session_state.get("radar_fonte", "?"),
                          "jogos com λ odds-implied": sum(1 for l in linhas
                                                          if l.get("fontes") == "odds-implied"),
                          "ligas em baseline (sem odds mapeadas)": sem_odds,
                          "sports ativos na the-odds-api": (len(oddsp._sports())
                                                            if oddsp.key else "sem chave")})
            if c2.button("Guardar previsões na BD"):
                db.save_predictions([{"data": str(data_sel), "match_id": l["match_id"],
                                      "jogo": f'{l["home"]} vs {l["away"]}', "liga": l["liga"],
                                      "lh": l["lh"], "la": l["la"], "p_home": l["p_home"],
                                      "p_draw": l["p_draw"], "p_away": l["p_away"],
                                      "p_over25": l["p_over25"], "p_btts": l["p_btts"],
                                      "fontes": l["fontes"]} for l in linhas])
                st.success(f"{len(linhas)} previsões guardadas.")
        else:
            st.info("Sem jogos carregados para o dia escolhido.")

    # ---------- TAB 2: LIVE ----------
    with tab_live:
        linhas = st.session_state.get("radar", [])
        opcoes = {f'{l["home"]} vs {l["away"]} ({l["liga"]})': l for l in linhas}
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        escolha = c1.selectbox("Jogo", ["— URL manual —"] + list(opcoes),
                               help="Escolhe um jogo do Radar para pré-carregar os λ calculados; "
                                    "'— URL manual —' deixa-te preencher tudo à mão.")
        jogo_sel = opcoes.get(escolha)
        url = c1.text_input("URL FotMob", jogo_sel["url"] if jogo_sel else "",
                            help="Cola o link da página do JOGO no FotMob — essas páginas são "
                                 "server-rendered; a lista do dia não é, por isso vem do "
                                 "football-data/ESPN sem URL.")
        lh0 = jogo_sel["lh"] if jogo_sel else CFG["baseline"]["home"]
        la0 = jogo_sel["la"] if jogo_sel else CFG["baseline"]["away"]
        prior_h = c2.number_input("λ casa (prior)", 0.1, 5.0, float(lh0), 0.05,
                                  help="Golos esperados da equipa da casa nos 90', estimados ANTES do "
                                       "jogo. É o prior bayesiano — o input central do modelo. O Radar "
                                       "sugere um valor, mas ajusta-o com o que o pipeline não vê: "
                                       "lesões, onze inicial, motivação.")
        prior_a = c2.number_input("λ fora (prior)", 0.1, 5.0, float(la0), 0.05,
                                  help="Igual ao λ casa, para a equipa de fora. Modelo é tão bom "
                                       "quanto estes dois números: lixo à entrada, EV+ fantasma à saída.")
        pet = c3.number_input("p(casa) prolong.", 0.0, 1.0, CFG["pet"], 0.01,
                              help="Probabilidade de a CASA se apurar se o jogo for a prolongamento/"
                                   "penáltis. O Poisson só modela até aos 90'; isto fecha a conta dos "
                                   "mercados 'qualifica-se'. 0.50 = moeda ao ar.")
        w = c3.number_input("Peso do prior W", 10.0, 180.0, CFG["w"], 5.0,
                            help="Quantos 'minutos de evidência' vale o teu prior na atualização "
                                 "bayesiana: λ_rev = (W·prior + 90·xG)/(W+min). Com W=90, aos 45' o "
                                 "live pesa 1/3. Baixa para reagir mais depressa ao xG; sobe para "
                                 "ignorar ruído de início de jogo.")
        side = c4.selectbox("Equipa vigiada", ["fora", "casa"],
                            help="A equipa cujo λ revelado é comparado com a linha de morte — "
                                 "normalmente a que ameaça a tua tese (apostaste 'casa vence a zero' "
                                 "→ vigia a fora).")
        kill = c4.number_input("Linha de morte λ*", 0.0, 5.0, 0.0, 0.05,
                               help="λ revelado a partir do qual consideras a tese morta e ponderas "
                                    "hedge/cash-out. É a tua regra de saída definida a frio, antes do "
                                    "jogo. 0 = automático (prior da vigiada + 0.10).")
        ev_min = c5.number_input("EV mínimo", 0.01, 0.30, CFG["ev_min"], 0.01,
                                 help="Margem mínima (prob.×odd − 1) para registar alerta na BD e "
                                      "enviar Telegram. 0.05 = gatilho de 5%. Sobe para menos ruído; "
                                      "lembra-te que o erro do modelo é muitas vezes maior que 5%.")
        monitor = c6.toggle("Monitorizar (60 s)", value=False,
                            help="Liga o autorefresh de 60 s (streamlit-autorefresh) enquanto o jogo "
                                 "decorre. Desliga no fim para poupar pedidos ao FotMob.")
        enviar_tg = c6.toggle("Enviar Telegram", value=bool(notifier.token),
                              help="Envia cada alerta EV+ para o bot definido em TELEGRAM_BOT_TOKEN/"
                                   "TELEGRAM_CHAT_ID. Sem env vars, fica só o registo na BD.")

        if monitor and st_autorefresh:
            st_autorefresh(interval=60_000, key="live_tick")
        elif monitor:
            st.warning("Instala `streamlit-autorefresh` para o loop de 60 s; usa o botão para atualizar.")
        st.button("Atualizar agora")

        if url:
            try:
                names, score, minuto, xg = scraper.fotmob_live(url)
                if xg is None:
                    st.info("xG live ainda indisponível — a usar 0.00.")
                    xg = [0.0, 0.0]
                rev_h = engine.lambda_revelado(prior_h, xg[0], minuto, w)
                rev_a = engine.lambda_revelado(prior_a, xg[1], minuto, w)
                rem_h = engine.lambda_restante(rev_h, minuto, engine.mult_estado(score[0], score[1]))
                rem_a = engine.lambda_restante(rev_a, minuto, engine.mult_estado(score[1], score[0]))
                probs = engine.market_probs(rem_h, rem_a, score[0], score[1], pet)

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Minuto", f"{minuto}'")
                m2.metric("Placar", f"{score[0]} - {score[1]}")
                m3.metric(f"xG {names[0]}", f"{xg[0]:.2f}")
                m4.metric(f"xG {names[1]}", f"{xg[1]:.2f}")
                m5.metric("λ restante Σ", f"{rem_h + rem_a:.2f}")

                i = 1 if side == "fora" else 0
                prior_w, rev_w = (prior_a, rev_a) if i == 1 else (prior_h, rev_h)
                kill_eff = kill if kill > 0 else prior_w + 0.10
                zona = ("🔴 TESE EVAPORADA" if rev_w >= kill_eff else
                        "🟡 ZONA AMARELA" if rev_w >= kill_eff - 0.05 else "🟢 DENTRO DO GUIÃO")
                st.progress(min(1.0, rev_w / kill_eff),
                            text=f"λ revelado {names[i]}: {rev_w:.3f} / morte {kill_eff:.2f} — {zona}")

                odds = oddsp.get(names[0], names[1])
                rows, alertas = [], []
                for mk, label in MARKET_LABELS.items():
                    p_ = probs[mk]
                    odd = odds.get(mk)
                    ev_ = engine.ev(p_, odd) if odd and p_ > 1e-4 else None
                    rows.append({"Mercado": label, "P %": round(p_ * 100, 1),
                                 "Fair": engine.fair(p_), "Odd": odd,
                                 "EV %": round(ev_ * 100, 1) if ev_ is not None else None})
                    if ev_ is not None and ev_ >= ev_min and p_ <= 0.97:
                        alertas.append((mk, label, p_, odd, ev_))
                st.dataframe(pd.DataFrame(rows), use_container_width=True, height=430)

                st.session_state.setdefault("cool", {})
                for mk, label, p_, odd, ev_ in alertas:
                    chave = f"{url}|{mk}"
                    ts, ev_ant = st.session_state.cool.get(chave, (0, -1))
                    if time.time() - ts < CFG["cooldown"] and ev_ < ev_ant + 0.02:
                        continue
                    st.session_state.cool[chave] = (time.time(), ev_)
                    metrica = (f"λ revelado de {names[0]} em {rev_h:.2f}" if mk in ("home", "qh", "wtn_home")
                               else f"λ revelado de {names[1]} em {rev_a:.2f}" if mk in ("away", "qa", "wtn_away")
                               else f"λ revelado combinado em {rev_h + rev_a:.2f}")
                    txt = formato_alerta(f"{names[0]} vs {names[1]}", minuto, f"{score[0]} - {score[1]}",
                                         f"{xg[0]:.2f} - {xg[1]:.2f}", label, odd, 1.0 / p_, ev_,
                                         metrica + ", suportando a entrada.")
                    db.insert_alert({"data": str(dt.date.today()),
                                     "match_id": jogo_sel["match_id"] if jogo_sel else url,
                                     "match_url": url, "jogo": f"{names[0]} vs {names[1]}",
                                     "minuto": minuto, "placar": f"{score[0]}-{score[1]}",
                                     "xg_live": f"{xg[0]:.2f}-{xg[1]:.2f}", "mercado": mk,
                                     "prob": p_, "odd_live": odd, "odd_justa": 1.0 / p_, "ev": ev_})
                    st.toast(f"EV+ {label} {ev_*100:+.1f}%")
                    if enviar_tg:
                        notifier.send(txt)
                if not odds:
                    st.caption("Sem odds live (define ODDS_API_KEY ou cria odds.json) — tabela mostra só P e Fair.")
            except Exception as e:
                st.error(f"Live falhou (fallback: tenta outra vez no próximo ciclo): {e}")

    # ---------- TAB 3: ALERTAS ----------
    with tab_alertas:
        df = db.alerts_df()
        if df.empty:
            st.info("Sem alertas registados.")
        else:
            st.dataframe(df, use_container_width=True, height=560)

    # ---------- TAB 4: HISTÓRICO ----------
    with tab_hist:
        c1, c2 = st.columns(2)
        if c1.button("Liquidar alertas pendentes (resultados finais)"):
            n_ok = 0
            for aid, murl, mk in db.open_alerts():
                try:
                    _, score, minuto, _ = scraper.fotmob_live(murl)
                    if minuto < 90:
                        continue
                    res = settle_market(mk, score[0], score[1])
                    if res is None:
                        db.settle(aid, "manual (prolongamento)", None)
                    else:
                        row = db.alerts_df().set_index("id").loc[aid]
                        db.settle(aid, "ganhou" if res else "perdeu",
                                  round(row.odd_live - 1, 3) if res else -1.0)
                    n_ok += 1
                except Exception:
                    continue
            st.success(f"{n_ok} alertas liquidados.")
        if c2.button("Recalcular calibração (auto-aprendizagem)"):
            fatores = BiasCalibrator.recompute(db)
            st.write(fatores if fatores else f"Amostra insuficiente (mínimo {CFG['calib_min_n']} por grupo).")

        df = db.alerts_df()
        liq = df[df.settled == 1] if not df.empty else df
        if not liq.empty:
            liq2 = liq[liq.resultado_final.isin(["ganhou", "perdeu"])]
            k1, k2, k3 = st.columns(3)
            k1.metric("Alertas liquidados", len(liq2))
            k2.metric("Hit rate", f"{(liq2.resultado_final=='ganhou').mean()*100:.1f}%" if len(liq2) else "—")
            k3.metric("ROI", f"{liq2.pnl.mean()*100:+.1f}%" if len(liq2) else "—")
            liq2 = liq2.assign(grupo=liq2.mercado.map(MARKET_GROUP))
            if len(liq2):
                st.dataframe(liq2.groupby("grupo").agg(n=("pnl", "size"), roi=("pnl", "mean"),
                                                       hit=("resultado_final", lambda s: (s == "ganhou").mean()))
                             .round(3), use_container_width=True)
        cal = db.calibration()
        st.caption("Fatores de calibração ativos (frequência real / prob. média do modelo, "
                   f"clamp ±15%, n ≥ {CFG['calib_min_n']}): " + (str(cal) if cal else "nenhum — histórico curto."))

    # ---------- TAB 5: CONSELHO DE AGENTES ----------
    with tab_conselho:
        if httpx is None:
            st.error("Instala httpx (`pip install httpx`) e recarrega.")
        elif not k_nim.strip():
            st.info("Cola a NVIDIA_API_KEY na barra lateral (build.nvidia.com) para ativar o conselho.")
        else:
            st.caption("Pipeline: Agentes 1-4 em paralelo (NIM) → verificação determinística de EV "
                       "em Python → Agente 5 (juiz) com FASE 0. O EV calculado prevalece sempre "
                       "sobre o que os modelos afirmarem.")
            with st.expander("Modelos NIM (editáveis — o catálogo muda de nomes)"):
                modelos = {}
                for i in range(1, 6):
                    modelos[f"agente{i}"] = st.text_input(
                        f"Agente {i}", cfg_db.get(f"nim_agente{i}", NIM_DEFAULTS[f"agente{i}"]),
                        key=f"nim_m{i}")
                cA, cB = st.columns(2)
                if cA.button("Guardar modelos"):
                    db.set_config({f"nim_agente{i}": modelos[f"agente{i}"] for i in range(1, 6)})
                    st.success("Modelos guardados.")
                if cB.button("Testar ligação NIM"):
                    try:
                        c5 = AgentCouncil(k_nim, modelos)
                        async def _ping():
                            async with httpx.AsyncClient(timeout=30,
                                    headers={"Authorization": f"Bearer {c5.key}"}) as cli:
                                return await c5._um(cli, modelos["agente5"], "Responde só: ok", "ping")
                        st.success("NIM OK: " + asyncio.run(_ping())[:40])
                    except Exception as e:
                        st.error(f"Falhou: {e}")

            st.subheader("FASE 0 — previsões por liquidar")
            abertas = db.agent_preds_abertas()
            fase0_partes = []
            if not abertas:
                st.caption("Sem previsões anteriores registadas.")
            for pid, pdata, pjogo, psint in abertas:
                cJ, cR, cL = st.columns([3, 2, 1])
                cJ.write(f"**{pjogo}** ({pdata})")
                res = cR.text_input("Resultado real (ex: 2-1)", key=f"res_{pid}",
                                    label_visibility="collapsed", placeholder="Resultado real ex: 2-1")
                if res.strip():
                    resumo = (psint or "").split("SÍNTESE FINAL")[-1].strip()[:400]
                    fase0_partes.append(f"- Jogo: {pjogo} | Real: {res.strip()} | "
                                        f"Previsão anterior: {resumo}")
                if cL.button("Liquidar", key=f"liq_{pid}") and res.strip():
                    db.settle_agent_pred(pid, res.strip())
                    st.rerun()
            fase0_txt = "\n".join(fase0_partes)

            st.subheader("Jogo de hoje")
            linhas_r = st.session_state.get("radar", [])
            ops = {f'{l["home"]} vs {l["away"]} ({l["liga"]})': l for l in linhas_r}
            c1, c2 = st.columns([2, 3])
            esc = c1.selectbox("Do Radar", ["— manual —"] + list(ops), key="cons_sel")
            jsel = ops.get(esc)
            jogo_nome = c1.text_input("Jogo", jsel and f'{jsel["home"]} vs {jsel["away"]}' or "",
                                      key="cons_jogo")
            o1, o2, o3, o4, o5 = st.columns(5)
            oh = o1.number_input("Odd 1 (PT)", 1.0, 100.0, 1.0, 0.01, key="c_oh")
            od = o2.number_input("Odd X", 1.0, 100.0, 1.0, 0.01, key="c_od")
            oa = o3.number_input("Odd 2", 1.0, 100.0, 1.0, 0.01, key="c_oa")
            oo = o4.number_input("Over 2.5", 1.0, 100.0, 1.0, 0.01, key="c_oo")
            ou = o5.number_input("Under 2.5", 1.0, 100.0, 1.0, 0.01, key="c_ou")
            notas = st.text_area("Notas (lesões, onzes, contexto — cola aqui o que vires no FotMob)",
                                 height=90, key="cons_notas")
            calib_txt = st.text_area("Calibração ativa (editável; vai no dossier e para o juiz)",
                                     cfg_db.get("calib_ativa", CALIB_ATIVA_DEFAULT), height=110,
                                     key="cons_calib")

            if st.button("Correr Conselho (1-4 → verificação → Juiz)", type="primary",
                         disabled=not jogo_nome.strip()):
                try:
                    lam = (jsel["lh"], jsel["la"]) if jsel else None
                    odds_pre = (oddsp.prematch(jsel["liga"], jsel["home"], jsel["away"])
                                if (jsel and oddsp.key) else {})
                    dossier = montar_dossier(jogo_nome, jsel["liga"] if jsel else "?",
                                             jsel["hora_utc"] if jsel else "?", lam,
                                             jsel["fontes"] if jsel else "", odds_pre,
                                             notas, calib_txt)
                    db.set_config({"calib_ativa": calib_txt})
                    conselho = AgentCouncil(k_nim, modelos)
                    with st.spinner("Agentes 1-4 em paralelo…"):
                        rels = conselho.correr_analistas(dossier)
                    probs = AgentCouncil.extrair_probs(rels[2])
                    odds_in = {"h": oh if oh > 1 else None, "d": od if od > 1 else None,
                               "a": oa if oa > 1 else None, "o25": oo if oo > 1 else None,
                               "u25": ou if ou > 1 else None}
                    verif, evj = AgentCouncil.verificacao_ev(probs, odds_in)
                    odds_txt = " | ".join(f"{k} @{v:.2f}" for k, v in odds_in.items() if v)
                    with st.spinner("Juiz (Agente 5)…"):
                        sint = conselho.julgar(str(dt.date.today()), fase0_txt, jogo_nome,
                                               odds_txt or "não fornecidas", rels, verif)
                    db.save_agent_pred(jogo_nome, odds_txt, json.dumps(probs or {}),
                                       json.dumps(evj), sint)
                    st.session_state["cons_out"] = {"rels": rels, "verif": verif,
                                                    "sint": sint, "dossier": dossier}
                except Exception as e:
                    st.error(f"Conselho falhou: {e}")

            out = st.session_state.get("cons_out")
            if out:
                with st.expander("📄 Dossier enviado"):
                    st.code(out["dossier"])
                for i, r in enumerate(out["rels"], 1):
                    with st.expander(f"Relatório Agente {i}"):
                        st.markdown(r)
                st.code(out["verif"])
                st.markdown("### 🧑‍⚖️ Síntese do Juiz (Agente 5)")
                st.markdown(out["sint"])


if __name__ == "__main__":
    setup_ui()
