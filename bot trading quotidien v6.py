"""
BOT DE TRADING QUOTIDIEN v6 - SCALPING INTRADAY : RSI + bougies japonaises
                              + SUPPORTS/RÉSISTANCES + take-profit/stop-loss
                              + gestion adaptative

Nouveautés vs v5 :
  - Détecte les niveaux de support (prix où le marché a rebondi vers le haut
    plusieurs fois) et de résistance (prix où il a été recalé vers le bas
    plusieurs fois) sur l'historique de bougies récent
  - Si un signal d'achat RSI apparaît alors que le prix est juste sous une
    résistance connue, le bot retarde l'achat (zone à risque de rejet)
  - Les niveaux détectés sont enregistrés dans le journal pour transparence

IMPORTANT (honnêteté) : la détection de supports/résistances ici est une
heuristique simple (plus hauts/plus bas locaux regroupés par proximité), pas
un algorithme professionnel de trading. Avec peu de bougies d'historique au
départ, les niveaux détectés seront peu fiables ; ils deviennent plus
pertinents à mesure que l'historique s'accumule.

Mode : paper trading (simulation). Aucun argent réel engagé.
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(BASE_DIR, "journal_trading.csv")
CAPITAL_INITIAL = 1000.0
FEE = 0.001

SEUIL_PERTES_REDUCTION = 2
SEUIL_PERTES_MODE_PRUDENT = 3
REDUCTION_TAILLE = 0.5

# --- Paramètres "scalping intraday" ---
INTERVALLE_MINUTES = 15   # bougies de 15 minutes
RSI_PERIODE = 14
RSI_SURVENTE = 30         # signal d'achat sous ce seuil
RSI_SURACHAT = 70         # signal de vente au-dessus de ce seuil
TAKE_PROFIT_PCT = 0.015   # sortie automatique à +1.5%
STOP_LOSS_PCT = 0.0075    # sortie automatique à -0.75%
SEUIL_PROXIMITE_NIVEAU = 0.003  # 0.3% : distance en dessous de laquelle une résistance est jugée "proche"


def get_btc_price():
    """Prix BTC/USD actuel (instantané) via l'API publique Kraken."""
    url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            result = data["result"]
            pair_key = list(result.keys())[0]
            return float(result[pair_key]["c"][0])
    except Exception as e:
        print(f"Erreur API prix ({e}).")
        return None


def get_btc_ohlc(nb_bougies=30, interval=INTERVALLE_MINUTES):
    """Récupère les N dernières bougies intraday (Open/High/Low/Close) via Kraken.
    Renvoie une liste de dicts triés du plus ancien au plus récent.
    La toute dernière bougie renvoyée par Kraken est en cours de formation
    (pas encore clôturée) : on l'exclut pour ne garder que des bougies complètes."""
    url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={interval}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            result = data["result"]
            pair_key = [k for k in result.keys() if k != "last"][0]
            raw = result[pair_key]
            candles = []
            for row in raw:
                candles.append({
                    "time": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                })
            candles = candles[:-1]  # on retire la bougie en cours, pas encore clôturée
            return candles[-nb_bougies:]
    except Exception as e:
        print(f"Erreur API bougies ({e}).")
        return []


def get_fear_greed_index():
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            entry = data["data"][0]
            return int(entry["value"]), entry["value_classification"]
    except Exception as e:
        print(f"Erreur API sentiment ({e}), sentiment ignoré pour aujourd'hui.")
        return None, None


# ---------------- Détection de figures de bougies japonaises ----------------

def detect_pattern(candles):
    """Analyse la ou les 2 dernières bougies complètes et renvoie le nom d'une
    figure reconnue, ou None si aucune figure claire n'est détectée."""
    if len(candles) < 2:
        return None

    prev, last = candles[-2], candles[-1]

    def body(c): return abs(c["close"] - c["open"])
    def range_(c): return c["high"] - c["low"] if c["high"] != c["low"] else 1e-9
    def upper_wick(c): return c["high"] - max(c["open"], c["close"])
    def lower_wick(c): return min(c["open"], c["close"]) - c["low"]
    def is_bullish(c): return c["close"] > c["open"]
    def is_bearish(c): return c["close"] < c["open"]

    b, r = body(last), range_(last)

    if lower_wick(last) >= 2 * b and upper_wick(last) <= 0.3 * b and prev["close"] > last["close"]:
        return "Marteau (retournement haussier potentiel)"

    if upper_wick(last) >= 2 * b and lower_wick(last) <= 0.3 * b and prev["close"] < last["close"]:
        return "Étoile filante (retournement baissier potentiel)"

    if is_bearish(prev) and is_bullish(last) and last["open"] <= prev["close"] and last["close"] >= prev["open"]:
        return "Engulfing haussier"

    if is_bullish(prev) and is_bearish(last) and last["open"] >= prev["close"] and last["close"] <= prev["open"]:
        return "Engulfing baissier"

    if b <= 0.1 * r:
        return "Doji (indécision)"

    return None


# ---------------- Détection de supports / résistances ----------------

def find_support_resistance(candles, window=3, tolerance=0.004, demi_vie_bougies=96):
    """Repère les plus hauts et plus bas locaux, puis regroupe les niveaux
    proches (marge `tolerance`) en donnant plus de poids aux touches récentes.

    Chaque touche a un poids qui diminue de moitié tous les `demi_vie_bougies`
    (par défaut 96 bougies de 15 min = ~24h) : une touche d'il y a 24h compte
    deux fois moins qu'une touche toute récente, une touche d'il y a 48h
    compte 4 fois moins, etc. Le niveau final est la moyenne pondérée par ces
    poids (pas une simple moyenne), donc il est "tiré" vers les prix les plus
    récemment testés. Un niveau doit être touché au moins 3 fois pour être
    retenu (évite de garder un simple pic isolé ou un double rebond peu
    significatif)."""
    n = len(candles)
    if n < window * 2 + 3:
        return [], []

    def poids(index):
        age_bougies = (n - 1) - index
        return 0.5 ** (age_bougies / demi_vie_bougies)

    swing_highs, swing_lows = [], []  # chaque élément : (prix, poids)
    for i in range(window, n - window):
        segment_h = [candles[j]["high"] for j in range(i - window, i + window + 1)]
        segment_l = [candles[j]["low"] for j in range(i - window, i + window + 1)]
        if candles[i]["high"] == max(segment_h):
            swing_highs.append((candles[i]["high"], poids(i)))
        if candles[i]["low"] == min(segment_l):
            swing_lows.append((candles[i]["low"], poids(i)))

    def regrouper(points):
        if not points:
            return []
        points = sorted(points, key=lambda p: p[0])
        groupes = [[points[0]]]
        for p in points[1:]:
            dernier_prix = groupes[-1][-1][0]
            if (p[0] - dernier_prix) / dernier_prix <= tolerance:
                groupes[-1].append(p)
            else:
                groupes.append([p])
        niveaux = []
        for g in groupes:
            if len(g) < 3:
                continue  # niveau touché moins de 3 fois : pas assez significatif
            poids_total = sum(w for _, w in g)
            prix_pondere = sum(prix * w for prix, w in g) / poids_total
            niveaux.append(prix_pondere)
        return niveaux

    return regrouper(swing_lows), regrouper(swing_highs)


def niveaux_proches(price, supports, resistances):
    support_dessous = max([s for s in supports if s < price], default=None)
    resistance_dessus = min([r for r in resistances if r > price], default=None)
    return support_dessous, resistance_dessus


def load_history():
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL, newline="") as f:
        return list(csv.DictReader(f))


def save_row(row, fieldnames):
    exists = os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def compute_rsi(prices, period=RSI_PERIODE):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_consecutive_losses(history):
    ventes = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct")]
    streak = 0
    for r in reversed(ventes):
        if float(r["pnl_pct"]) < 0:
            streak += 1
        else:
            break
    return streak


def run_daily():
    history = load_history()
    prices = [float(r["prix_btc"]) for r in history]

    price = get_btc_price()
    if price is None:
        if prices:
            price = prices[-1]
            print("Prix de secours = dernier prix connu.")
        else:
            print("Aucun prix disponible et aucun historique. Réessaie plus tard.")
            return
    prices.append(price)

    candles = get_btc_ohlc(nb_bougies=480)  # ~5 jours de bougies 15min pour des niveaux plus solides
    motif = detect_pattern(candles) if candles else None
    closes = [c["close"] for c in candles] if candles else []
    supports, resistances = find_support_resistance(candles) if candles else ([], [])

    fg_value, fg_label = get_fear_greed_index()

    rsi = compute_rsi(closes) if closes else None
    rsi_prec = compute_rsi(closes[:-1]) if len(closes) > 1 else None

    position = float(history[-1]["position"]) if history else 0.0
    capital = float(history[-1]["capital"]) if history else CAPITAL_INITIAL
    btc_detenu = float(history[-1]["btc_detenu"]) if history else 0.0
    prix_entree = float(history[-1]["prix_entree"]) if history and history[-1].get("prix_entree") else 0.0

    consecutive_pertes = get_consecutive_losses(history)

    taille_position = 1.0
    mode_prudent = False
    if consecutive_pertes >= SEUIL_PERTES_MODE_PRUDENT:
        mode_prudent = True
        taille_position = REDUCTION_TAILLE
    elif consecutive_pertes >= SEUIL_PERTES_REDUCTION:
        taille_position = REDUCTION_TAILLE

    sentiment_risque = False
    if fg_label in ("Extreme Fear", "Extreme Greed"):
        sentiment_risque = True
        taille_position *= REDUCTION_TAILLE

    decision = "HOLD"
    pnl_pct = ""
    motif_bloque_achat = False
    motif_bloque_resistance = False
    raison_sortie = ""
    support_proche, resistance_proche = niveaux_proches(price, supports, resistances)

    # --- Vérification prioritaire : take-profit / stop-loss ---
    sortie_forcee = False
    if position == 1 and prix_entree:
        variation = (price - prix_entree) / prix_entree
        if variation >= TAKE_PROFIT_PCT:
            sortie_forcee = True
            raison_sortie = f"Take-profit atteint (+{variation*100:.2f}%)"
        elif variation <= -STOP_LOSS_PCT:
            sortie_forcee = True
            raison_sortie = f"Stop-loss atteint ({variation*100:.2f}%)"

    if sortie_forcee:
        decision = f"VENTE ({raison_sortie})"
        valeur_vente = btc_detenu * price * (1 - FEE)
        pnl_pct = round((price - prix_entree) / prix_entree * 100, 2)
        capital += valeur_vente
        btc_detenu = 0.0
        position = 0
        prix_entree = 0.0

    elif rsi is not None and rsi_prec is not None:
        # Signal d'achat : le RSI ÉTAIT en survente et vient d'en ressortir
        # (confirmation du rebond, pas juste un frôlement du seuil)
        signal_achat = rsi_prec < RSI_SURVENTE and rsi >= RSI_SURVENTE
        # Signal de vente : le RSI ÉTAIT en surachat et vient d'en ressortir
        signal_vente = rsi_prec > RSI_SURACHAT and rsi <= RSI_SURACHAT

        if signal_achat and resistance_proche is not None:
            distance_resistance = (resistance_proche - price) / price
            if distance_resistance <= SEUIL_PROXIMITE_NIVEAU:
                signal_achat = False
                motif_bloque_resistance = True

        if signal_achat and motif in ("Étoile filante (retournement baissier potentiel)", "Engulfing baissier"):
            signal_achat = False
            motif_bloque_achat = True

        if signal_achat and position == 0:
            decision = "ACHAT"
            montant_investi = capital * taille_position
            btc_detenu = (montant_investi * (1 - FEE)) / price
            capital -= montant_investi
            prix_entree = price
            position = 1
        elif signal_vente and position == 1:
            decision = "VENTE (signal RSI)"
            valeur_vente = btc_detenu * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2) if prix_entree else 0
            capital += valeur_vente
            btc_detenu = 0.0
            position = 0
            prix_entree = 0.0
        elif motif_bloque_achat:
            decision = "HOLD (achat retardé par figure baissière)"
        elif motif_bloque_resistance:
            decision = f"HOLD (achat retardé, résistance proche à {resistance_proche:.2f})"
    else:
        decision = f"EN ATTENTE (pas encore assez de bougies {INTERVALLE_MINUTES}min pour calculer le RSI)"

    valeur_totale = capital + btc_detenu * price

    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "prix_btc": round(price, 2),
        "rsi": round(rsi, 1) if rsi else "",
        "rsi_precedent": round(rsi_prec, 1) if rsi_prec else "",
        "sentiment_marche": fg_label or "",
        "motif_chandelier": motif or "",
        "support_proche": round(support_proche, 2) if support_proche else "",
        "resistance_proche": round(resistance_proche, 2) if resistance_proche else "",
        "decision": decision,
        "taille_position_appliquee": taille_position,
        "mode_prudent": mode_prudent,
        "pertes_consecutives": consecutive_pertes,
        "position": position,
        "capital": round(capital, 2),
        "btc_detenu": round(btc_detenu, 6),
        "prix_entree": round(prix_entree, 2) if prix_entree else "",
        "pnl_pct": pnl_pct,
        "valeur_totale": round(valeur_totale, 2),
    }
    save_row(row, list(row.keys()))

    print(f"[{row['date']}] BTC: {price:.2f}$ | Sentiment: {fg_label} ({fg_value}) | Motif: {motif} | Décision: {decision}")
    print(f"Support proche: {support_proche} | Résistance proche: {resistance_proche}")
    if mode_prudent:
        print(f"⚠️ Mode prudent activé ({consecutive_pertes} pertes d'affilée).")
    if sentiment_risque:
        print(f"⚠️ Marché nerveux ({fg_label}) : exposition réduite.")
    if motif_bloque_achat:
        print(f"⚠️ Achat retardé : figure baissière détectée ({motif}).")
    print(f"Valeur totale du portefeuille simulé : {valeur_totale:.2f}€")


def rapport():
    history = load_history()
    if not history:
        print("Aucune donnée enregistrée pour l'instant.")
        return
    depart = CAPITAL_INITIAL
    fin = float(history[-1]["valeur_totale"])
    perf = (fin / depart - 1) * 100
    trades = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct") != ""]
    gagnants = [t for t in trades if float(t["pnl_pct"]) > 0]
    perdants = [t for t in trades if float(t["pnl_pct"]) <= 0]
    motifs_detectes = [r["motif_chandelier"] for r in history if r.get("motif_chandelier")]

    print("=" * 60)
    print("RAPPORT DE PERFORMANCE")
    print("=" * 60)
    print(f"Période : {history[0]['date']} -> {history[-1]['date']}")
    print(f"Capital de départ : {depart:.2f}€")
    print(f"Valeur finale : {fin:.2f}€")
    print(f"Performance : {perf:+.2f}%")
    print(f"Trades clôturés : {len(trades)} ({len(gagnants)} gagnants / {len(perdants)} perdants)")
    if trades:
        print(f"Taux de réussite : {len(gagnants)/len(trades)*100:.1f}%")
    print(f"Figures de bougies détectées sur la période : {len(motifs_detectes)}")
    print("Détail complet dans journal_trading.csv")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rapport":
        rapport()
    else:
        run_daily()
