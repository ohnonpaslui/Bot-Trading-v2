"""
BOT DE TRADING QUOTIDIEN v2 - MACD + gestion adaptative du risque + sentiment de marché

Nouveautés vs v1 :
  - Suit chaque trade fermé (gagnant/perdant) et son PnL
  - Réduit automatiquement la taille des positions après des pertes consécutives
  - Exige une confirmation RSI supplémentaire après 3 pertes d'affilée (mode prudent)
  - Remonte progressivement en confiance après un trade gagnant
  - Consulte l'indice Fear & Greed du marché crypto (API gratuite, sans clé) et
    réduit l'exposition en cas de "Extreme Fear" / "Extreme Greed" (marché nerveux,
    souvent lié à des annonces macro/économiques)

IMPORTANT : ceci reste un bot à règles fixes (pas un modèle qui "comprend" ses
erreurs comme un humain). Ce qu'il fait de réel : de la gestion de risque
adaptative, une pratique standard chez les traders systématiques.

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

# --- Paramètres de gestion adaptative du risque ---
SEUIL_PERTES_REDUCTION = 2       # après 2 pertes d'affilée -> taille réduite
SEUIL_PERTES_MODE_PRUDENT = 3    # après 3 pertes d'affilée -> confirmation RSI exigée
REDUCTION_TAILLE = 0.5           # taille de position divisée par 2
RESTAURATION_APRES_GAIN = 1.0    # retour à la taille normale après un gain


def get_btc_price():
    """Prix BTC/USD actuel via l'API publique Kraken (gratuite, sans clé)."""
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


def get_fear_greed_index():
    """Indice Fear & Greed du marché crypto. API gratuite, sans clé (alternative.me)."""
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            entry = data["data"][0]
            return int(entry["value"]), entry["value_classification"]
    except Exception as e:
        print(f"Erreur API sentiment ({e}), sentiment ignoré pour aujourd'hui.")
        return None, None


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


def ema(values, span):
    k = 2 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return None, None
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    return macd_line[-1], signal_line[-1]


def compute_rsi(prices, period=14):
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
    """Compte les pertes d'affilée à partir des dernières VENTE enregistrées."""
    ventes = [r for r in history if r["decision"] == "VENTE" and r.get("pnl_pct")]
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

    fg_value, fg_label = get_fear_greed_index()

    macd, macd_signal = compute_macd(prices)
    rsi = compute_rsi(prices)

    position = float(history[-1]["position"]) if history else 0.0
    capital = float(history[-1]["capital"]) if history else CAPITAL_INITIAL
    btc_detenu = float(history[-1]["btc_detenu"]) if history else 0.0
    prix_entree = float(history[-1]["prix_entree"]) if history and history[-1].get("prix_entree") else 0.0

    consecutive_pertes = get_consecutive_losses(history)

    # --- Gestion adaptative de la taille de position ---
    taille_position = 1.0
    mode_prudent = False
    if consecutive_pertes >= SEUIL_PERTES_MODE_PRUDENT:
        mode_prudent = True
        taille_position = REDUCTION_TAILLE
    elif consecutive_pertes >= SEUIL_PERTES_REDUCTION:
        taille_position = REDUCTION_TAILLE

    # --- Ajustement selon le sentiment de marché (proxy "annonces économiques") ---
    sentiment_risque = False
    if fg_label in ("Extreme Fear", "Extreme Greed"):
        sentiment_risque = True
        taille_position *= REDUCTION_TAILLE

    decision = "HOLD"
    pnl_pct = ""

    if macd is not None:
        signal_achat = macd > macd_signal
        signal_vente = macd < macd_signal

        # En mode prudent, on exige une confirmation RSI (pas de sur-achat) avant d'acheter
        if mode_prudent and signal_achat and rsi is not None and rsi > 65:
            signal_achat = False  # trop tard dans le mouvement, on n'entre pas

        if signal_achat and position == 0:
            decision = "ACHAT"
            montant_investi = capital * taille_position
            btc_detenu = (montant_investi * (1 - FEE)) / price
            capital -= montant_investi
            prix_entree = price
            position = 1
        elif signal_vente and position == 1:
            decision = "VENTE"
            valeur_vente = btc_detenu * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2) if prix_entree else 0
            capital += valeur_vente
            btc_detenu = 0.0
            position = 0
            prix_entree = 0.0
    else:
        decision = "EN ATTENTE (historique insuffisant, ~35 jours nécessaires pour le MACD)"

    valeur_totale = capital + btc_detenu * price

    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "prix_btc": round(price, 2),
        "macd": round(macd, 2) if macd else "",
        "signal": round(macd_signal, 2) if macd_signal else "",
        "rsi": round(rsi, 1) if rsi else "",
        "sentiment_marche": fg_label or "",
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

    print(f"[{row['date']}] BTC: {price:.2f}$ | Sentiment: {fg_label} ({fg_value}) | Décision: {decision}")
    if mode_prudent:
        print(f"⚠️ Mode prudent activé ({consecutive_pertes} pertes d'affilée) : taille de position réduite, confirmation RSI exigée.")
    if sentiment_risque:
        print(f"⚠️ Marché nerveux ({fg_label}) : exposition réduite par précaution.")
    print(f"Valeur totale du portefeuille simulé : {valeur_totale:.2f}€")


def rapport():
    history = load_history()
    if not history:
        print("Aucune donnée enregistrée pour l'instant.")
        return
    depart = CAPITAL_INITIAL
    fin = float(history[-1]["valeur_totale"])
    perf = (fin / depart - 1) * 100
    trades = [r for r in history if r["decision"] == "VENTE" and r.get("pnl_pct") != ""]
    gagnants = [t for t in trades if float(t["pnl_pct"]) > 0]
    perdants = [t for t in trades if float(t["pnl_pct"]) <= 0]

    print("=" * 60)
    print("RAPPORT DE PERFORMANCE")
    print("=" * 60)
    print(f"Période : {history[0]['date']} -> {history[-1]['date']}")
    print(f"Capital de départ : {depart:.2f}€")
    print(f"Valeur finale : {fin:.2f}€")
    print(f"Performance : {perf:+.2f}%")
    print(f"Trades clôturés : {len(trades)} ({len(gagnants)} gagnants / {len(perdants)} perdants)")
    if trades:
        taux_reussite = len(gagnants) / len(trades) * 100
        print(f"Taux de réussite : {taux_reussite:.1f}%")
    print(f"Nombre de fois où le mode prudent s'est activé : {sum(1 for r in history if r.get('mode_prudent') == 'True')}")
    print("Détail complet dans journal_trading.csv")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rapport":
        rapport()
    else:
        run_daily()
