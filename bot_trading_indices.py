"""
BOT DE TRADING - INDICES US (stratégie RSI(2) de Larry Connors)

Contrairement au bot crypto (RSI(14), scalping 30min sur BTC/ETH), celui-ci
trade des indices boursiers américains via des ETF liquides (SPY = S&P 500),
avec une stratégie RE-CHERCHÉE et documentée, pas inventée :

  LA STRATÉGIE RSI(2) DE LARRY CONNORS
  (source : "Short Term Trading Strategies That Work", Connors & Alvarez ;
  des centaines de milliers de trades backtestés sur indices US)

  1. Filtre de tendance : SMA 200 périodes. On n'achète QUE si le prix est
     au-dessus (on achète les creux dans une tendance haussière de fond, pas
     un couteau qui tombe dans une tendance baissière).
  2. Signal d'achat : RSI(2) — période volontairement très courte et donc
     très réactive — descend sous 10 (Connors a trouvé qu'un seuil encore
     plus bas, 5, donne un edge encore meilleur mais génère moins de signaux).
  3. Sortie : quand le prix repasse au-dessus de sa moyenne mobile 5 périodes
     (pas un pourcentage fixe comme sur le bot crypto).

  ÉCART ASSUMÉ PAR RAPPORT À LA RECHERCHE ORIGINALE : Connors n'utilisait
  AUCUN stop-loss dans ses tests (il a démontré que ça dégradait la
  performance sur longue période). On en garde quand même un ici par
  prudence — c'est un choix délibéré qui s'écarte de la méthode originale,
  pas une erreur d'implémentation.

LIMITES TECHNIQUES HONNÊTES :
  - Contrairement à Kraken (crypto, gratuit, sans clé), il n'existe aucune
    API gratuite et sans inscription pour les indices boursiers. Ce bot
    utilise Alpha Vantage (clé gratuite, simple email, sans carte bancaire).
  - Le plan gratuit d'Alpha Vantage est limité (25 requêtes/jour). Ce bot ne
    traite donc qu'UN SEUL indice (SPY) et tourne moins souvent (1x/heure)
    que le bot crypto, pour rester dans ce quota.
  - La bourse US n'est ouverte que ~9h30-16h00 heure de New York, en semaine.
    En dehors de ces horaires, les données ne changent pas — c'est normal,
    pas un bug (contrairement à la crypto qui trade 24h/24).

Mode : paper trading (simulation). Aucun argent réel engagé.
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPITAL_INITIAL = 1000.0
FEE = 0.001

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

RSI_PERIODE = 2                # court et réactif, volontairement (coeur de la méthode Connors)
RSI_SEUIL_ACHAT = 10           # Connors : plus bas (ex: 5) = meilleur edge mais moins de signaux
SMA_TENDANCE_PERIODE = 200     # filtre de tendance de fond
SMA_SORTIE_PERIODE = 5         # règle de sortie
STOP_LOSS_PCT = 0.02           # garde-fou ajouté (absent de la recherche originale de Connors)

ACTIFS = [
    {"nom": "S&P 500 (SPY)", "symbole": "SPY", "journal": "journal_trading_indices.csv"},
]


def get_intraday(symbole, interval="30min"):
    """Récupère les bougies intraday via Alpha Vantage. Renvoie une liste de
    dicts {time, open, high, low, close}, du plus ancien au plus récent."""
    if not ALPHA_VANTAGE_KEY:
        print("Erreur : clé ALPHA_VANTAGE_API_KEY manquante (variable d'environnement).")
        return []
    url = (
        "https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY"
        f"&symbol={symbole}&interval={interval}&outputsize=full&apikey={ALPHA_VANTAGE_KEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
            cle_serie = [k for k in data.keys() if k.startswith("Time Series")]
            if not cle_serie:
                print(f"Erreur API Alpha Vantage pour {symbole} : {data}")
                return []
            serie = data[cle_serie[0]]
            candles = []
            for horodatage in sorted(serie.keys()):
                v = serie[horodatage]
                candles.append({
                    "time": horodatage,
                    "open": float(v["1. open"]),
                    "high": float(v["2. high"]),
                    "low": float(v["3. low"]),
                    "close": float(v["4. close"]),
                })
            return candles
    except Exception as e:
        print(f"Erreur API bougies {symbole} ({e}).")
        return []


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


def compute_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def load_history(chemin_journal):
    if not os.path.exists(chemin_journal):
        return []
    with open(chemin_journal, newline="") as f:
        return list(csv.DictReader(f))


def save_row(chemin_journal, row):
    exists = os.path.exists(chemin_journal)
    with open(chemin_journal, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def traiter_actif(actif):
    nom, symbole, chemin_relatif = actif["nom"], actif["symbole"], actif["journal"]
    chemin_journal = os.path.join(BASE_DIR, chemin_relatif)
    history = load_history(chemin_journal)

    candles = get_intraday(symbole)
    if not candles:
        print(f"[{nom}] Données indisponibles, actif ignoré pour cette exécution.")
        return

    derniere_bougie_connue = history[-1]["derniere_bougie"] if history else ""
    bougie_actuelle = candles[-1]["time"]
    if bougie_actuelle == derniere_bougie_connue:
        print(f"[{nom}] Pas de nouvelle bougie depuis la dernière exécution (marché probablement fermé).")
        return

    closes = [c["close"] for c in candles]
    price = closes[-1]

    rsi = compute_rsi(closes)
    sma_tendance = compute_sma(closes, SMA_TENDANCE_PERIODE)
    sma_sortie = compute_sma(closes, SMA_SORTIE_PERIODE)
    tendance_haussiere = sma_tendance is not None and price > sma_tendance

    position = float(history[-1]["position"]) if history else 0.0
    capital = float(history[-1]["capital"]) if history else CAPITAL_INITIAL
    quantite_detenue = float(history[-1]["quantite_detenue"]) if history else 0.0
    prix_entree = float(history[-1]["prix_entree"]) if history and history[-1].get("prix_entree") else 0.0

    decision = "HOLD"
    pnl_pct = ""

    # --- Stop-loss (garde-fou ajouté, absent de la recherche Connors d'origine) ---
    sortie_forcee = False
    if position == 1 and prix_entree:
        variation = (price - prix_entree) / prix_entree
        if variation <= -STOP_LOSS_PCT:
            sortie_forcee = True

    if sortie_forcee:
        decision = f"VENTE (Stop-loss atteint ({(price-prix_entree)/prix_entree*100:.2f}%))"
        valeur_vente = quantite_detenue * price * (1 - FEE)
        pnl_pct = round((price - prix_entree) / prix_entree * 100, 2)
        capital += valeur_vente
        quantite_detenue = 0.0
        position = 0
        prix_entree = 0.0

    elif rsi is not None and sma_tendance is not None:
        signal_achat = tendance_haussiere and rsi < RSI_SEUIL_ACHAT and position == 0
        signal_vente = position == 1 and sma_sortie is not None and price > sma_sortie

        if signal_achat:
            decision = f"ACHAT (RSI2={rsi:.1f} < {RSI_SEUIL_ACHAT}, tendance haussière)"
            montant_investi = capital
            quantite_detenue = (montant_investi * (1 - FEE)) / price
            capital -= montant_investi
            prix_entree = price
            position = 1
        elif signal_vente:
            decision = f"VENTE (prix repassé au-dessus de la SMA{SMA_SORTIE_PERIODE})"
            valeur_vente = quantite_detenue * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2) if prix_entree else 0
            capital += valeur_vente
            quantite_detenue = 0.0
            position = 0
            prix_entree = 0.0
        elif not tendance_haussiere:
            decision = f"HOLD (tendance de fond baissière, prix sous la SMA{SMA_TENDANCE_PERIODE})"
    else:
        decision = "EN ATTENTE (historique insuffisant pour calculer la SMA200)"

    valeur_totale = capital + quantite_detenue * price

    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "actif": nom,
        "derniere_bougie": bougie_actuelle,
        "prix": round(price, 2),
        "rsi2": round(rsi, 1) if rsi else "",
        "sma200": round(sma_tendance, 2) if sma_tendance else "",
        "sma5": round(sma_sortie, 2) if sma_sortie else "",
        "tendance_haussiere": tendance_haussiere,
        "decision": decision,
        "position": position,
        "capital": round(capital, 2),
        "quantite_detenue": round(quantite_detenue, 6),
        "prix_entree": round(prix_entree, 2) if prix_entree else "",
        "pnl_pct": pnl_pct,
        "valeur_totale": round(valeur_totale, 2),
    }
    save_row(chemin_journal, row)
    print(f"[{nom}] {price} | RSI(2)={rsi} | Tendance haussière: {tendance_haussiere} | Décision: {decision}")


def run_daily():
    for actif in ACTIFS:
        try:
            traiter_actif(actif)
        except Exception as e:
            print(f"Erreur inattendue sur {actif['nom']} : {e}")


def rapport():
    for actif in ACTIFS:
        chemin_journal = os.path.join(BASE_DIR, actif["journal"])
        history = load_history(chemin_journal)
        print("=" * 60)
        print(f"RAPPORT : {actif['nom']}")
        print("=" * 60)
        if not history:
            print("Aucune donnée enregistrée pour l'instant.")
            continue
        fin = float(history[-1]["valeur_totale"])
        perf = (fin / CAPITAL_INITIAL - 1) * 100
        trades = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct") != ""]
        gagnants = [t for t in trades if float(t["pnl_pct"]) > 0]
        print(f"Valeur finale : {fin:.2f}€ ({perf:+.2f}%)")
        print(f"Trades clôturés : {len(trades)} ({len(gagnants)} gagnants)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rapport":
        rapport()
    else:
        run_daily()
