"""
BOT DE TRADING QUOTIDIEN v5 - SCALPING INTRADAY : RSI + bougies japonaises
                              + take-profit/stop-loss serrés + gestion adaptative

Changement de philosophie vs v4 :
  - Ne s'exécute plus une fois par jour, mais toutes les 15 minutes (~96 fois/jour)
  - Utilise des bougies de 15 minutes (pas journalières)
  - Signal principal : RSI(14) sur 15 minutes — survente (<30) = achat,
    surachat (>70) = vente
  - Le MACD est abandonné comme signal principal (trop lent pour du scalping)
  - Les bougies japonaises servent de filtre de confirmation, comme avant
  - Take-profit +1,5% / stop-loss -0,75% (resserrés pour des trades courts)

IMPORTANT (honnêteté) :
  - Beaucoup plus de trades = beaucoup plus de frais cumulés (0,1% par achat
    ET par vente). Un RSI sur 15 minutes réagit énormément au "bruit" de
    marché à court terme, pas seulement à de vrais mouvements. Attends-toi à
    des allers-retours fréquents sans gain net, c'est le compromis du
    day trading / scalping, pas un défaut du code.
  - GitHub Actions ne garantit pas une précision à la minute près pour les
    tâches programmées très fréquentes : une tâche "toutes les 15 minutes"
    peut réellement s'exécuter toutes les 20-30 minutes selon la charge des
    serveurs GitHub à ce moment-là.

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

    candles = get_btc_ohlc(nb_bougies=30)
    motif = detect_pattern(candles) if candles else None
    closes = [c["close"] for c in candles] if candles else []

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
    raison_sortie = ""

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
