"""
BOT DE SCALPING HAUTE FRÉQUENCE - BANDES DE BOLLINGER (1 minute)

Contrairement aux autres bots (RSI, Connors, ORB) qui limitent volontairement
la fréquence des trades (parfois 1 seul par jour), celui-ci est conçu pour
en prendre BEAUCOUP, aussi souvent que le marché s'y prête.

LA STRATÉGIE (recherchée, pas inventée) : retour à la moyenne sur Bandes de
Bollinger — un des indicateurs de scalping les plus utilisés, car il se
déclenche naturellement très souvent (contrairement au RSI(14) qui peut
rester silencieux pendant des heures).

  1. Bandes de Bollinger (20 périodes, 2 écarts-types) sur bougies 1 minute.
  2. ACHAT : dès que le prix touche ou dépasse la bande basse (le marché est
     statistiquement "trop bas" à très court terme, pari sur un retour à la
     moyenne).
  3. VENTE (signal) : dès que le prix revient à la moyenne mobile centrale
     (objectif du retour à la moyenne atteint).
  4. VENTE (stop-loss / take-profit) : garde-fous serrés en % fixe, pour
     limiter le risque sur un mouvement qui continue de chuter au lieu de
     rebondir.
  5. Aucune limite de nombre de trades par jour (à l'inverse du bot ORB) —
     c'est le but recherché ici : beaucoup de trades.

LIMITE TECHNIQUE HONNÊTE : "plusieurs trades à l'intérieur d'une même
minute" n'est pas réalisable avec notre architecture (aucun service gratuit
ne garantit une exécution plus rapide qu'environ 1 fois par minute). Ce bot
tourne donc au maximum une fois par minute, via un déclencheur externe
(cron-job.org), pas via la programmation interne de GitHub qui est trop
capricieuse à cette fréquence.

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

INTERVALLE_MINUTES = 1
BB_PERIODE = 20
BB_ECART_TYPE = 2.0
TAKE_PROFIT_PCT = 0.004   # +0.4% : objectif serré, cohérent avec du scalping 1 minute
STOP_LOSS_PCT = 0.002     # -0.2%

ACTIFS = [
    {"nom": "BTC/USD", "pair_kraken": "XBTUSD", "journal": "journal_scalping_bollinger_BTCUSD.csv"},
    {"nom": "ETH/USD", "pair_kraken": "ETHUSD", "journal": "journal_scalping_bollinger_ETHUSD.csv"},
]


def get_price(pair_kraken):
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair_kraken}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            result = data["result"]
            pair_key = list(result.keys())[0]
            return float(result[pair_key]["c"][0])
    except Exception as e:
        print(f"Erreur API prix {pair_kraken} ({e}).")
        return None


def get_closes(pair_kraken, nb_bougies=BB_PERIODE + 5, interval=INTERVALLE_MINUTES):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair_kraken}&interval={interval}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
            result = data["result"]
            pair_key = [k for k in result.keys() if k != "last"][0]
            raw = result[pair_key]
            closes = [float(row[4]) for row in raw]
            closes = closes[:-1]  # exclut la bougie en cours, pas encore clôturée
            return closes[-nb_bougies:]
    except Exception as e:
        print(f"Erreur API bougies {pair_kraken} ({e}).")
        return []


def compute_bollinger(closes, periode=BB_PERIODE, ecarts=BB_ECART_TYPE):
    if len(closes) < periode:
        return None, None, None
    fenetre = closes[-periode:]
    moyenne = sum(fenetre) / periode
    variance = sum((c - moyenne) ** 2 for c in fenetre) / periode
    ecart_type = variance ** 0.5
    bande_haute = moyenne + ecarts * ecart_type
    bande_basse = moyenne - ecarts * ecart_type
    return bande_basse, moyenne, bande_haute


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
    nom, pair_kraken, chemin_relatif = actif["nom"], actif["pair_kraken"], actif["journal"]
    chemin_journal = os.path.join(BASE_DIR, chemin_relatif)
    history = load_history(chemin_journal)

    price = get_price(pair_kraken)
    if price is None:
        print(f"[{nom}] Prix indisponible, actif ignoré.")
        return
    closes = get_closes(pair_kraken)
    if not closes:
        print(f"[{nom}] Bougies indisponibles, actif ignoré.")
        return

    bande_basse, moyenne, bande_haute = compute_bollinger(closes)

    position = float(history[-1]["position"]) if history else 0.0
    capital = float(history[-1]["capital"]) if history else CAPITAL_INITIAL
    quantite_detenue = float(history[-1]["quantite_detenue"]) if history else 0.0
    prix_entree = float(history[-1]["prix_entree"]) if history and history[-1].get("prix_entree") else 0.0

    decision = "HOLD"
    pnl_pct = ""

    if bande_basse is None:
        decision = "EN ATTENTE (historique insuffisant pour les Bandes de Bollinger)"
    else:
        # --- Stop-loss / take-profit, prioritaires ---
        sortie_forcee = False
        if position == 1 and prix_entree:
            variation = (price - prix_entree) / prix_entree
            if variation >= TAKE_PROFIT_PCT:
                sortie_forcee = True
                decision = f"VENTE (take-profit +{variation*100:.2f}%)"
            elif variation <= -STOP_LOSS_PCT:
                sortie_forcee = True
                decision = f"VENTE (stop-loss {variation*100:.2f}%)"

        if sortie_forcee:
            valeur_vente = quantite_detenue * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2)
            capital += valeur_vente
            quantite_detenue = 0.0
            position = 0
            prix_entree = 0.0

        elif position == 1 and price >= moyenne:
            decision = "VENTE (retour à la moyenne atteint)"
            valeur_vente = quantite_detenue * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2) if prix_entree else 0
            capital += valeur_vente
            quantite_detenue = 0.0
            position = 0
            prix_entree = 0.0

        elif position == 0 and price <= bande_basse:
            decision = f"ACHAT (bande basse touchée, {bande_basse:.2f})"
            montant_investi = capital
            quantite_detenue = (montant_investi * (1 - FEE)) / price
            capital -= montant_investi
            prix_entree = price
            position = 1

    valeur_totale = capital + quantite_detenue * price

    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "actif": nom,
        "prix": round(price, 2),
        "bande_basse": round(bande_basse, 2) if bande_basse else "",
        "moyenne": round(moyenne, 2) if moyenne else "",
        "bande_haute": round(bande_haute, 2) if bande_haute else "",
        "decision": decision,
        "position": position,
        "capital": round(capital, 2),
        "quantite_detenue": round(quantite_detenue, 8),
        "prix_entree": round(prix_entree, 2) if prix_entree else "",
        "pnl_pct": pnl_pct,
        "valeur_totale": round(valeur_totale, 2),
    }
    save_row(chemin_journal, row)
    print(f"[{nom}] {price} | Bandes: {bande_basse}-{moyenne}-{bande_haute} | Décision: {decision}")


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
        trades = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct") not in ("", None)]
        gagnants = [t for t in trades if float(t["pnl_pct"]) > 0]
        print(f"Valeur finale : {fin:.2f}€ ({perf:+.2f}%)")
        print(f"Trades clôturés : {len(trades)} ({len(gagnants)} gagnants)")
        if trades:
            print(f"Taux de réussite : {len(gagnants)/len(trades)*100:.1f}%")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rapport":
        rapport()
    else:
        run_daily()
