"""
BOT DE TRADING v7 - MULTI-ACTIFS + améliorations "vues par un trader"

Nouveautés vs v6 :
  1. FILTRE DE TENDANCE DE FOND : une moyenne mobile longue (50 périodes) sert
     de contexte. Un signal d'achat n'est accepté que si le prix est au-dessus
     de cette moyenne (pas d'achat "à contre-tendance" sur un marché baissier
     de fond).
  2. TAKE-PROFIT / STOP-LOSS DYNAMIQUES (ATR) : au lieu de seuils fixes
     (+1.5%/-0.75%), les seuils s'adaptent à la volatilité réelle du marché
     au moment de l'entrée (ATR = Average True Range). Marché calme -> seuils
     serrés. Marché agité -> seuils plus larges. Repli sur les seuils fixes
     si l'ATR n'est pas calculable (historique insuffisant).
  3. FILTRE DE VOLUME : un signal d'achat n'est confirmé que si le volume de
     la bougie est significativement supérieur à la moyenne récente
     (mouvement soutenu par de l'activité réelle, pas un sursaut isolé).
  4. PAUSE APRÈS STOP-LOSS : après une sortie en stop-loss, le bot s'interdit
     toute nouvelle entrée pendant quelques bougies (évite l'enchaînement de
     pertes en marché agité/"choppy").
  5. MULTI-ACTIFS : le bot traite maintenant une liste d'actifs (BTC/USD,
     ETH/USD, BTC/EUR, ...) au lieu d'un seul. Chaque actif a son propre
     portefeuille virtuel indépendant et son propre fichier de journal.
  6. SIGNAL DE CASSURE (BREAKOUT) : troisième signal d'achat, en complément
     du retournement RSI et du croisement de moyennes mobiles. Se déclenche
     quand le prix vient de dépasser une résistance connue, confirmé par le
     volume. Ajouté car sur un marché qui reste durablement en tendance forte
     (RSI collé en zone de surachat sans jamais repasser par un retournement),
     les deux autres signaux peuvent rester inactifs pendant très longtemps.

LIMITE HONNÊTE : ceci reste basé sur l'API PUBLIQUE de Kraken, qui ne propose
que des cryptomonnaies et des paires crypto/devises (BTC/EUR, ETH/USD...).
Un vrai forex (EUR/USD, GBP/JPY, paires sans aucune crypto) nécessiterait une
tout autre source de données (courtier forex dédié), pas incluse ici.

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

SEUIL_PERTES_REDUCTION = 2
SEUIL_PERTES_MODE_PRUDENT = 3
REDUCTION_TAILLE = 0.5

INTERVALLE_MINUTES = 30
RSI_PERIODE = 14
RSI_SURVENTE = 35
RSI_SURACHAT = 65
SEUIL_PROXIMITE_NIVEAU = 0.003

# --- Nouveaux paramètres "vus par un trader" ---
SMA_LONGUE_PERIODE = 50
TAKE_PROFIT_PCT_DEFAUT = 0.015
STOP_LOSS_PCT_DEFAUT = 0.0075
ATR_PERIODE = 14
ATR_MULT_TP = 2.0
ATR_MULT_SL = 1.0
VOLUME_PERIODE = 20
VOLUME_MULTIPLICATEUR = 1.2
COOLDOWN_BOUGIES_APRES_STOP = 2

# --- Liste des actifs traités. Ajoute/retire des lignes ici pour changer les marchés. ---
ACTIFS = [
    {"nom": "BTC/USD", "pair_kraken": "XBTUSD", "journal": "journal_trading.csv"},
    {"nom": "ETH/USD", "pair_kraken": "ETHUSD", "journal": "journal_trading_ETHUSD.csv"},
    {"nom": "BTC/EUR", "pair_kraken": "XBTEUR", "journal": "journal_trading_XBTEUR.csv"},
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


def get_ohlc(pair_kraken, nb_bougies=480, interval=INTERVALLE_MINUTES):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair_kraken}&interval={interval}"
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
                    "volume": float(row[6]),
                })
            candles = candles[:-1]
            return candles[-nb_bougies:]
    except Exception as e:
        print(f"Erreur API bougies {pair_kraken} ({e}).")
        return []


def get_fear_greed_index():
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
            entry = data["data"][0]
            return int(entry["value"]), entry["value_classification"]
    except Exception as e:
        print(f"Erreur API sentiment ({e}).")
        return None, None


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


def signal_tendance(closes, court=5, long=20):
    if len(closes) < long + 1:
        return False, False
    sma_court_prec = compute_sma(closes[:-1], court)
    sma_long_prec = compute_sma(closes[:-1], long)
    sma_court = compute_sma(closes, court)
    sma_long = compute_sma(closes, long)
    if None in (sma_court_prec, sma_long_prec, sma_court, sma_long):
        return False, False
    achat = sma_court_prec <= sma_long_prec and sma_court > sma_long
    vente = sma_court_prec >= sma_long_prec and sma_court < sma_long
    return achat, vente


def compute_atr(candles, period=ATR_PERIODE):
    """Average True Range : mesure la volatilité récente. True Range d'une
    bougie = le plus grand écart parmi (high-low), (high-clôture précédente),
    (low-clôture précédente). L'ATR est la moyenne de ces écarts sur `period`
    bougies."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(len(candles) - period, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        close_prec = candles[i - 1]["close"]
        tr = max(high - low, abs(high - close_prec), abs(low - close_prec))
        true_ranges.append(tr)
    return sum(true_ranges) / len(true_ranges)


def volume_confirme(candles, period=VOLUME_PERIODE, multiplicateur=VOLUME_MULTIPLICATEUR):
    if len(candles) < period + 2:
        return True
    volumes_precedents = [c["volume"] for c in candles[-(period + 1):-1]]
    moyenne = sum(volumes_precedents) / len(volumes_precedents)
    if moyenne == 0:
        return True
    return candles[-1]["volume"] >= multiplicateur * moyenne


def detect_pattern(candles):
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


def find_support_resistance(candles, window=3, tolerance=0.004, demi_vie_bougies=96):
    n = len(candles)
    if n < window * 2 + 3:
        return [], []

    def poids(index):
        age_bougies = (n - 1) - index
        return 0.5 ** (age_bougies / demi_vie_bougies)

    swing_highs, swing_lows = [], []
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
                continue
            poids_total = sum(w for _, w in g)
            prix_pondere = sum(prix * w for prix, w in g) / poids_total
            niveaux.append(prix_pondere)
        return niveaux

    return regrouper(swing_lows), regrouper(swing_highs)


def niveaux_proches(price, supports, resistances):
    support_dessous = max([s for s in supports if s < price], default=None)
    resistance_dessus = min([r for r in resistances if r > price], default=None)
    return support_dessous, resistance_dessus


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


def get_consecutive_losses(history):
    ventes = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct")]
    streak = 0
    for r in reversed(ventes):
        if float(r["pnl_pct"]) < 0:
            streak += 1
        else:
            break
    return streak


def bougies_depuis_stop_loss(history):
    for i in range(len(history) - 1, -1, -1):
        if "Stop-loss" in history[i].get("decision", ""):
            bougies_ecoulees = (len(history) - 1) - i
            restant = COOLDOWN_BOUGIES_APRES_STOP - bougies_ecoulees
            return max(restant, 0)
        if history[i].get("decision", "").startswith("ACHAT"):
            return 0
    return 0


def traiter_actif(actif, fg_value, fg_label):
    nom, pair_kraken, chemin_relatif = actif["nom"], actif["pair_kraken"], actif["journal"]
    chemin_journal = os.path.join(BASE_DIR, chemin_relatif)

    history = load_history(chemin_journal)

    price = get_price(pair_kraken)
    if price is None:
        print(f"[{nom}] Prix indisponible, actif ignoré pour cette exécution.")
        return

    candles = get_ohlc(pair_kraken, nb_bougies=480)
    if not candles:
        print(f"[{nom}] Bougies indisponibles, actif ignoré pour cette exécution.")
        return

    closes = [c["close"] for c in candles]
    motif = detect_pattern(candles)
    supports, resistances = find_support_resistance(candles)
    atr = compute_atr(candles)
    sma_longue = compute_sma(closes, SMA_LONGUE_PERIODE)
    vol_ok = volume_confirme(candles)

    rsi = compute_rsi(closes)
    rsi_prec = compute_rsi(closes[:-1]) if len(closes) > 1 else None

    position = float(history[-1]["position"]) if history else 0.0
    capital = float(history[-1]["capital"]) if history else CAPITAL_INITIAL
    quantite_detenue = float(history[-1]["quantite_detenue"]) if history else 0.0
    prix_entree = float(history[-1]["prix_entree"]) if history and history[-1].get("prix_entree") else 0.0
    atr_entree = float(history[-1]["atr_entree"]) if history and history[-1].get("atr_entree") else 0.0

    consecutive_pertes = get_consecutive_losses(history)
    cooldown_restant = bougies_depuis_stop_loss(history)

    taille_position = 1.0
    mode_prudent = False
    if consecutive_pertes >= SEUIL_PERTES_MODE_PRUDENT:
        mode_prudent = True
        taille_position = REDUCTION_TAILLE
    elif consecutive_pertes >= SEUIL_PERTES_REDUCTION:
        taille_position = REDUCTION_TAILLE

    if fg_label in ("Extreme Fear", "Extreme Greed"):
        taille_position *= REDUCTION_TAILLE

    decision = "HOLD"
    pnl_pct = ""
    support_proche, resistance_proche = niveaux_proches(price, supports, resistances)
    tendance_haussiere = sma_longue is not None and price > sma_longue

    sortie_forcee = False
    raison_sortie = ""
    if position == 1 and prix_entree:
        variation_absolue = price - prix_entree
        if atr_entree:
            seuil_tp = ATR_MULT_TP * atr_entree
            seuil_sl = ATR_MULT_SL * atr_entree
        else:
            seuil_tp = TAKE_PROFIT_PCT_DEFAUT * prix_entree
            seuil_sl = STOP_LOSS_PCT_DEFAUT * prix_entree
        if variation_absolue >= seuil_tp:
            sortie_forcee = True
            raison_sortie = f"Take-profit atteint (+{variation_absolue/prix_entree*100:.2f}%)"
        elif variation_absolue <= -seuil_sl:
            sortie_forcee = True
            raison_sortie = f"Stop-loss atteint ({variation_absolue/prix_entree*100:.2f}%)"

    if sortie_forcee:
        decision = f"VENTE ({raison_sortie})"
        valeur_vente = quantite_detenue * price * (1 - FEE)
        pnl_pct = round((price - prix_entree) / prix_entree * 100, 2)
        capital += valeur_vente
        quantite_detenue = 0.0
        position = 0
        prix_entree = 0.0
        atr_entree = 0.0

    elif rsi is not None and rsi_prec is not None:
        signal_achat_rsi = rsi_prec < RSI_SURVENTE and rsi >= RSI_SURVENTE
        signal_vente_rsi = rsi_prec > RSI_SURACHAT and rsi <= RSI_SURACHAT
        signal_achat_tendance, signal_vente_tendance = signal_tendance(closes)

        # Signal 3 (cassure/breakout) : le prix vient de dépasser une résistance
        # connue, confirmé par le volume. Complète les 2 autres signaux, qui
        # peinent à se déclencher sur un marché en tendance forte (le RSI reste
        # bloqué en zone extrême, sans repasser par les seuils de retournement).
        prix_precedent = closes[-2] if len(closes) > 1 else None
        signal_achat_breakout = (
            resistance_proche is not None
            and prix_precedent is not None
            and prix_precedent <= resistance_proche
            and price > resistance_proche
        )

        signal_achat = signal_achat_rsi or signal_achat_tendance or signal_achat_breakout
        signal_vente = signal_vente_rsi or signal_vente_tendance
        if signal_achat_rsi:
            origine_achat = "retournement RSI"
        elif signal_achat_tendance:
            origine_achat = "tendance haussière"
        elif signal_achat_breakout:
            origine_achat = f"cassure de résistance à {resistance_proche:.2f}"
        else:
            origine_achat = ""
        origine_vente = "retournement RSI" if signal_vente_rsi else ("tendance baissière" if signal_vente_tendance else "")

        blocage = None

        if signal_achat and sma_longue is not None and not tendance_haussiere:
            signal_achat = False
            blocage = f"tendance de fond baissière (prix sous la SMA{SMA_LONGUE_PERIODE})"

        if signal_achat and not vol_ok:
            signal_achat = False
            blocage = "volume insuffisant pour confirmer le signal"

        if signal_achat and cooldown_restant > 0:
            signal_achat = False
            blocage = f"pause après stop-loss ({cooldown_restant} exécution(s) restante(s))"

        # Le filtre "résistance proche" ne s'applique qu'aux signaux RSI/tendance
        # (risque de rejet en approchant une résistance par en dessous) — il ne
        # doit PAS bloquer un signal de cassure, puisque dépasser cette même
        # résistance est justement sa condition de déclenchement.
        if signal_achat and not signal_achat_breakout and resistance_proche is not None:
            if (resistance_proche - price) / price <= SEUIL_PROXIMITE_NIVEAU:
                signal_achat = False
                blocage = f"résistance proche à {resistance_proche:.2f}"
        if signal_achat and motif in ("Étoile filante (retournement baissier potentiel)", "Engulfing baissier"):
            signal_achat = False
            blocage = f"figure baissière détectée ({motif})"

        if signal_achat and position == 0:
            decision = f"ACHAT ({origine_achat})"
            montant_investi = capital * taille_position
            quantite_detenue = (montant_investi * (1 - FEE)) / price
            capital -= montant_investi
            prix_entree = price
            atr_entree = atr if atr else 0.0
            position = 1
        elif signal_vente and position == 1:
            decision = f"VENTE ({origine_vente})"
            valeur_vente = quantite_detenue * price * (1 - FEE)
            pnl_pct = round((price - prix_entree) / prix_entree * 100, 2) if prix_entree else 0
            capital += valeur_vente
            quantite_detenue = 0.0
            position = 0
            prix_entree = 0.0
            atr_entree = 0.0
        elif blocage:
            decision = f"HOLD (achat retardé : {blocage})"
    else:
        decision = f"EN ATTENTE (historique insuffisant pour {nom})"

    valeur_totale = capital + quantite_detenue * price

    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "actif": nom,
        "prix_btc": round(price, 5),
        "rsi": round(rsi, 1) if rsi else "",
        "rsi_precedent": round(rsi_prec, 1) if rsi_prec else "",
        "sma_longue": round(sma_longue, 2) if sma_longue else "",
        "tendance_haussiere": tendance_haussiere,
        "atr": round(atr, 4) if atr else "",
        "volume_confirme": vol_ok,
        "sentiment_marche": fg_label or "",
        "motif_chandelier": motif or "",
        "support_proche": round(support_proche, 2) if support_proche else "",
        "resistance_proche": round(resistance_proche, 2) if resistance_proche else "",
        "decision": decision,
        "taille_position_appliquee": taille_position,
        "mode_prudent": mode_prudent,
        "pertes_consecutives": consecutive_pertes,
        "cooldown_restant": cooldown_restant,
        "position": position,
        "capital": round(capital, 2),
        "quantite_detenue": round(quantite_detenue, 8),
        "prix_entree": round(prix_entree, 5) if prix_entree else "",
        "atr_entree": round(atr_entree, 4) if atr_entree else "",
        "pnl_pct": pnl_pct,
        "valeur_totale": round(valeur_totale, 2),
    }
    save_row(chemin_journal, row)
    print(f"[{nom}] {price} | RSI {rsi} | Tendance haussière: {tendance_haussiere} | Décision: {decision}")


def run_daily():
    fg_value, fg_label = get_fear_greed_index()
    for actif in ACTIFS:
        try:
            traiter_actif(actif, fg_value, fg_label)
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
        depart = CAPITAL_INITIAL
        fin = float(history[-1]["valeur_totale"])
        perf = (fin / depart - 1) * 100
        trades = [r for r in history if r["decision"].startswith("VENTE") and r.get("pnl_pct") != ""]
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
