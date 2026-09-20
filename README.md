# stock-paper-trader

Agent autonome de **paper trading** actions : il décide seul d'acheter, vendre
ou ne rien faire, une fois par séance, sur un compte virtuel de 100 000 USD et
un univers de 87 grandes capitalisations américaines.

> **Simulation uniquement.** Le projet ne contient aucune clé de courtier,
> aucun code de signature, aucun appel vers un endpoint d'exécution. Le seul
> accès réseau est un `GET` public et non authentifié sur l'historique
> quotidien. Les ordres n'existent que sous forme de lignes dans une base
> SQLite locale.

C'est la version actions de [`cryptoagent`](https://github.com/Guiroux555/cryptoagent),
et ce n'est pas un portage : trois contraintes du marché actions changent la
structure du moteur, et le seul chiffre qui comptait vraiment là-bas — la
significativité statistique — change de camp ici.

---

## Installation

```bash
git clone <url> && cd stock-paper-trader
python3 -m pip install -e ".[dev]"     # aucune dépendance runtime
```

Python 3.10+. La bibliothèque standard suffit : pas de pandas, pas de numpy,
pas de framework.

## Démarrage

```bash
python -m trader sync                # 36 ans d'historique, 88 symboles, ~1 min
python -m trader backtest            # rejoue l'historique dans le moteur live
python -m trader backtest --split 0.6      # in-sample / hors échantillon
python -m trader backtest --walk-forward 12
python -m trader tick                # prend une décision maintenant
python -m trader run                 # tourne en continu, une décision par séance
python -m trader status              # compte, positions, ordres en attente
python -m trader watch               # ce que la stratégie voit, valeur par valeur
python -m trader trends              # tendances court et moyen terme, par secteur et par valeur
python -m trader news --sync         # collecte et archive les titres de presse
python -m trader log -n 30           # journal des décisions, HOLD compris
python -m trader reset               # remet le compte à zéro, garde l'historique
```

---

## Ce qui change quand on passe de la crypto aux actions

### 1. La clôture n'est pas un prix qu'on peut obtenir

C'est la différence qui structure tout le reste. Un marché ouvert 24 h sur 24
laisse un agent voir une clôture et traiter à ce prix. Un marché actions non :
quand le prix de clôture existe, le carnet est fermé.

Remplir à la clôture qu'on vient de lire est la façon la plus courante
d'inventer de l'argent dans un backtest actions, et elle est presque toujours
silencieuse. Ici la décision et l'exécution sont séparées **par construction,
pas par discipline** :

```
séance D,  clôture   →  le signal est lu, un ordre est mis en file
séance D+1, ouverture →  l'ordre est exécuté au premier cours coté
```

Un ordre estampillé de la séance courante n'est jamais exécuté à l'ouverture de
celle-ci. C'est le garde-fou, et il a son test :
`test_an_order_is_never_filled_on_the_session_that_created_it`.

**Ce que coûte cette honnêteté :** rien. Elle rapporte.

| | complet | in-sample | hors éch. |
|---|---|---|---|
| ouverture suivante (le modèle réel) | **+1 555,9%** | **+407,8%** | **+214,6%** |
| clôture du jour (le raccourci) | +1 394,7% | +375,8% | +184,4% |

Le résultat est contre-intuitif et mérite d'être lu correctement. Le raccourci
**perd** ici parce que cette stratégie entre sur cassure : la clôture d'une
séance de cassure est le prix le plus étendu de la journée, et l'ouverture du
lendemain en rend souvent une partie. Ce n'est pas une propriété du raccourci,
c'est un hasard de cette règle-ci. Sur une stratégie de retour à la moyenne, le
même raccourci flatterait franchement. On ne l'a pas gardé pour la performance,
on l'a gardé parce qu'il est vrai — et le mode malhonnête reste accessible
(`execute_at_close`) uniquement pour que ce tableau soit reproductible.

Trois conséquences du même choix, chacune testée :

- **Un achat en file est annulé si le titre a gappé** de plus d'un ATR pendant
  la nuit. Un ordre au marché à l'ouverture n'est pas une promesse d'acheter à
  n'importe quel prix. Sans ça, l'agent achète chaque saut de résultats au
  sommet du gap, avec un stop calculé pour un prix qui n'existe plus.
- **Un achat qui ouvre sous son propre stop est annulé.**
- **Le stop est actif dès la séance d'entrée.** Une valeur qui ouvre en hausse
  puis se retourne toute la journée sort le même jour ; la laisser courir
  jusqu'à la clôture offrirait à l'agent une nuit gratuite qu'il n'a pas eue.

### 2. Le stop dort chez le courtier, le stop suiveur non

Un stop est un **ordre au repos**. Il vit à la bourse et n'a pas besoin que
l'agent soit réveillé — c'est la seule chose qu'un agent non surveillé obtienne
gratuitement, et la modéliser correctement est ce qui rend une cadence basse
survivable.

Déplacer un stop suiveur, en revanche, est un ordre à annuler et replacer.
L'agent endormi ne peut pas l'envoyer. Donc : **les séances sans décision
vérifient les stops et exécutent les ordres en file, mais ne bougent aucun stop
et n'en posent aucun nouveau.** Cette asymétrie est le vrai coût d'une cadence
basse, et elle est mesurée plus bas au lieu d'être supposée.

### 3. Le calendrier a des trous

Une séance n'est pas une tranche d'horloge. Rien dans ce code ne fait
d'arithmétique sur les timestamps pour trouver « la bougie suivante » : la
suite des séances est celle que disent les données. Les jours fériés ne sont
codés nulle part — l'agent se réveille à la clôture du prochain jour ouvré, ne
trouve pas de nouvelle séance, et ne décide pas. C'est auto-correcteur ; une
table de fériés serait une chose de plus à maintenir juste.

Les prix sont **ajustés des splits et des dividendes**. Sans ça un détachement
apparaît comme un gap baissier nocturne, et le stop suiveur se fait toucher par
un versement que le porteur a encaissé.

---

## Ce qu'il fait

**Univers** 87 grandes capitalisations américaines, réparties délibérément sur
les onze secteurs. **Séances quotidiennes**, de 1990 à aujourd'hui.
**Long uniquement, au comptant.** Ni vente à découvert, ni levier. **Actions
entières.**

### La décision d'entrée — cinq filtres

| # | Filtre | Raison |
|---|--------|--------|
| 1 | Prix au-dessus de l'EMA 200 | Ne jamais être long dans une tendance baissière |
| 2 | EMA 50 effectivement en hausse | Éviter une moyenne franchie par accident |
| 3 | ATR entre 0,5% et 10% du prix | Garde-fou, et il ne mord jamais — voir plus bas |
| 4 | RSI < 78 | Ne pas acheter ce qui a déjà couru |
| 5 | **Déclencheur : clôture au-dessus du plus-haut des 20 séances précédentes** | Une cassure confirmée, pas un croisement de moyennes |

Une cassure est un **état**, pas un événement : le prix est au-dessus du niveau
franchi ou il ne l'est pas. L'agent la voit donc quel que soit le moment de son
réveil — c'est la propriété que la version crypto avait dû reconstruire son
déclencheur pour obtenir, et elle est encore plus utile ici.

### Le gardien du marché

`regime.py` pose la question qu'aucune valeur seule ne peut trancher : **SPY
est-il au-dessus de sa propre EMA 200 ?** Sinon, plus aucune entrée. Le filtre
ne bloque **que les entrées** ; les sorties ne sont jamais bloquées.

SPY plutôt qu'un seuil d'ampleur, parce que c'est une condition bien définie
plutôt qu'un nombre réglé sur les données mêmes qui ont servi à régler la
stratégie. Ce qu'il vaut dépend entièrement de la présence d'un marché baissier
dans la fenêtre, et le dire est la version honnête de ce paragraphe :

| ancre SPY | rendement | maxDD | CAGR/DD |
|---|---|---|---|
| **1990-2012** (deux baisses de 50% de l'indice) | | | |
| activée | +407,8% | **14,4%** | **0,55** |
| désactivée | +289,0% | 30,6% | 0,22 |
| **2012-2026** (un vrai bear, deux krachs vite effacés) | | | |
| activée | +214,6% | 17,1% | 0,47 |
| désactivée | +222,3% | **13,6%** | **0,61** |

Hors échantillon, la protection **coûte** environ 0,2 point de CAGR. Elle est
conservée quand même : une protection ne s'évalue pas sur une période qui n'en
avait pas besoin. Mais le chiffre est là plutôt que caché.

### La sortie

Sortie si le prix repasse sous l'EMA 200 (la raison d'être de la position a
disparu — celle-là ignore la durée de détention minimale), sortie sur rupture
de tendance, et un stop suiveur large. Pas d'objectif de gain fixe.

### Le dimensionnement

```
quantité = (capital × 0,3%) / (prix d'entrée − stop),  arrondie à l'entier inférieur
```

Un stop large achète moins, un stop serré achète plus, et la perte si le stop
saute est la même 0,3% du capital dans les deux cas. Plafonds successifs : 4% du
capital par valeur, **25 positions simultanées**, 75% d'exposition totale.

Le prix utilisé pour dimensionner est **celui de l'exécution**, pas celui du
signal. Dimensionner sur la clôture risquerait discrètement plus que le budget
chaque fois que le titre ouvre en hausse — c'est-à-dire la plupart des nuits.

---

## Les résultats, et le chiffre qu'il ne faut pas escamoter

Sur **13 094 jours** (nov. 1990 → sept. 2026, 36 ans, 87 valeurs,
9 027 séances) :

```
Total return        1555.88%        Buy & hold SPY     +3062.21%
CAGR                   8.14%          its drawdown         55.19%
Max drawdown          15.93%          CAGR per drawdown      0.18
CAGR per drawdown       0.51        Avg exposure           59.4%
Positions               3875        Win rate              40.7%
Profit factor           1.74        Expectancy/trade     401.52 USD
```

**Acheter et garder SPY rapporte deux fois plus.** C'est la première chose à
lire, avant tout le reste. 10,11% par an contre 8,14%.

Ce que l'agent fait, c'est le rapporter en traversant un drawdown de 15,9% au
lieu de 55,2%, en étant investi à 59% en moyenne. Par unité de risque encaissé,
2,8 fois mieux : 0,51 contre 0,18. Ce n'est pas le même produit, et aligner deux
rendements sans le drawdown est la chose la plus flatteuse qu'on puisse faire à
l'indice.

Le second benchmark, le panier équipondéré des 87 valeurs, fait **+30 378%**. Il
ne prouve rien du tout : c'est le rendement d'un panier qu'on ne pouvait pas
constituer en 1990, puisqu'il est composé des entreprises qui sont encore
grandes et encore cotées en 2026. Il est imprimé pour que le biais soit visible,
pas pour être battu.

### Le profil, régime par régime

| fenêtre | agent | SPY | DD agent | DD SPY |
|---|---|---|---|---|
| 1990-11 → 2000-03 bull | +242,2% | **+297,1%** | 14,0% | 19,0% |
| 2000-03 → 2002-10 éclatement dot-com | **−8,1%** | −46,1% | **8,5%** | 46,8% |
| 2002-10 → 2007-10 reprise | +65,1% | **+116,6%** | 8,0% | 14,2% |
| 2007-10 → 2009-03 crise financière | **−7,5%** | −54,7% | **10,3%** | 54,7% |
| 2009-03 → 2020-02 le long bull | +169,2% | **+517,0%** | 10,9% | 19,3% |
| 2020-02 → 2020-03 krach covid | **−2,9%** | −32,1% | **2,9%** | 32,0% |
| 2020-03 → 2022-01 rebond covid | +40,4% | **+118,3%** | 7,2% | 9,4% |
| 2022-01 → 2022-10 bear 2022 | **−7,0%** | −24,4% | 11,1% | 24,4% |
| 2022-10 → 2026-09 depuis | +21,3% | **+124,4%** | 11,5% | 18,8% |

Il ne bat jamais l'indice en marché haussier et ne perd jamais vraiment en
marché baissier. C'est la signature d'un suiveur de tendance à stops, et il n'y
a rien d'autre à en tirer.

### La ligne qui compte

```
CONCENTRATION  (how much of this was one lucky trade?)
Best trade              1.8% of all gains
P&L less top 5   +1,346,122.26 USD
Positive years             29 of 37
```

Aucune position ne pèse plus de 1,8% des gains, retirer les cinq meilleures
laisse 1,35 million, et 29 années sur 37 sont positives. C'est la ligne à
surveiller à chaque évolution : un rendement porté par un trade est un tirage,
pas un système.

### Hors échantillon, et l'aveu qui va avec

```bash
python -m trader backtest --split 0.6      # coupure : 3 janvier 2012
```

| | agent | SPY |
|--|-----------|-----|
| In-sample (1993-02 → 2012-01) | +284,6% — CAGR 7,38%, DD 14,4%, CAGR/DD **0,51** | +300,9% — CAGR 7,62%, DD 55,2%, CAGR/DD 0,14 |
| **Hors éch. (2012-01 → 2026-09)** | +214,6% — CAGR 8,10%, DD 17,1%, CAGR/DD 0,47 | **+669,8%** — CAGR 14,89%, DD 33,7%, CAGR/DD 0,44 |

*(l'in-sample démarre en 1993 parce que SPY n'existe pas avant ; le backtest
refuse désormais de comparer une fenêtre que le benchmark ne couvre pas, au lieu
d'imprimer un 0% qui offrirait à l'agent trente points d'avance fictifs.)*

Sur les quinze dernières années, que le réglage n'a pas vues, l'agent gagne
8,1% par an pendant que l'indice en fait 14,9%. Il n'en garde qu'une chose : un
drawdown à peine plus élevé de moitié, pour un CAGR/DD comparable.

**Et voici l'aveu.** Le réglage décrit plus bas a **triplé** le résultat
in-sample et n'a **rien** apporté hors échantillon :

| | in-sample | hors échantillon |
|---|---|---|
| réglages initiaux (12 places, stop suiveur 5 ATR) | +128,5% | **+248,7%** (CAGR 8,86%) |
| réglages retenus (25 places, stop suiveur 8 ATR) | **+407,8%** | +214,6% (CAGR 8,10%) |

C'est le résultat le plus utile de tout ce document. Ce qui a été gardé du
réglage ne l'a pas été pour le rendement — il n'y en a pas — mais pour des
raisons structurelles qui tiennent dans les deux fenêtres, et elles sont
détaillées ci-dessous.

### Walk-forward

```bash
python -m trader backtest --walk-forward 12
```

Douze fenêtres consécutives d'environ trois ans : **médiane +27,8%,
12 fenêtres positives sur 12, pire +15,0%** — et l'indice battu dans
seulement **4 fenêtres sur 11**. Les deux moitiés de cette phrase sont le
projet entier.

---

## La significativité : ce que la crypto ne pouvait pas atteindre

Le README de `cryptoagent` se termine sur un constat et une piste :

> « Le t corrigé est passé de 0,66 à 1,15 en allongeant l'historique — le seul
> levier qui ait marché. Il en faudrait environ trois fois plus pour atteindre
> 2, et Binance n'a pas trois fois plus. La suite serait de sortir de la crypto,
> vers des actifs moins corrélés entre eux : c'est un autre projet. »

C'est ce projet, et la piste était bonne.

### La corrélation, mesurée

Sur la fenêtre commune aux 87 valeurs (2013-01 → 2026-09, 3 449 séances) :

| | corrélation moyenne | actifs effectifs |
|---|---|---|
| 20 paires crypto | 0,67 | **1,46** / 20 |
| **87 actions US** | **0,338** | **2,89** / 87 |

Avec `k/(1+(k−1)ρ)`. Quadrupler l'univers crypto avait fait passer
l'indépendance effective de 1,31 à 1,46 ; passer aux actions la porte à 2,89.
Ce n'est toujours pas 87 — ça ne le sera jamais, tout monte et descend
ensemble — mais c'est le double, et le double suffit.

### Pourquoi : les secteurs se découplent, l'intérieur d'un secteur non

| | corrélation moyenne | paires |
|---|---|---|
| à l'intérieur d'un secteur | **0,467** | 378 |
| entre secteurs | **0,324** | 3 363 |

Le détail est plus parlant que la moyenne :

| secteur | valeurs | ρ | actifs effectifs |
|---|---|---|---|
| Énergie | 5 | **0,769** | 1,23 / 5 |
| Services aux collectivités | 3 | 0,740 | 1,21 / 3 |
| **Financières** | 11 | **0,671** | **1,43 / 11** |
| Consommation de base | 8 | 0,500 | 1,78 / 8 |
| Industrie | 10 | 0,456 | 1,96 / 10 |
| Technologie | 15 | 0,428 | 2,14 / 15 |
| Santé | 12 | 0,388 | 2,28 / 12 |
| Communication | 7 | 0,320 | 2,40 / 7 |

Les onze financières de cet univers corrèlent à **0,671** — exactement le
chiffre que l'agent crypto mesurait sur ses vingt paires. **Un secteur, c'est un
marché crypto.** Toute l'indépendance vient de la diversification sectorielle,
aucune de l'empilement de valeurs dans un même secteur, et c'est la raison pour
laquelle l'univers est construit par secteur plutôt que par capitalisation.

### Le t corrigé

```
positions                    3 875
P&L moyen par position      +401,52 USD   (écart-type 3 248,26)
IC 95% naïf             [+299,24 ; +503,79] USD
t naïf                        7,69
durée moyenne de détention   65 jours sur 35,8 ans
périodes non chevauchantes     201
paris indépendants            ~580
t corrigé                     2,98
```

| | positions | t naïf | paris indép. | t corrigé |
|---|---|---|---|---|
| crypto, 5 symboles, 2 ans | 121 | 1,79 | ~32 | 0,92 |
| crypto, 20 symboles, 9 ans | 1 601 | 4,27 | ~117 | 1,15 |
| **actions, 87 valeurs, 36 ans** | **3 875** | **7,69** | **~580** | **2,98** |

**t = 2,98 franchit le seuil de 2.** Sur la mesure corrigée de la corrélation,
avec la même méthode de correction que le projet crypto — volontairement, pour
que les deux chiffres soient comparables — l'espérance par position est
distinguable du hasard à 95%.

Deux leviers, pas un : trois fois plus de paris indépendants par unité de temps
(les secteurs) **et** quatre fois plus de temps (36 ans contre 9).

### Ce que ce t ne dit pas

Il dit que l'espérance mesurée **sur cet échantillon** n'est pas du bruit. Il ne
dit rien sur le biais de l'échantillon, et celui-ci est massif :

**Ces 87 valeurs sont celles qui sont grandes et cotées aujourd'hui.** Les
rejouer depuis 1990 suppose qu'on les aurait choisies en 1990 — on ne l'aurait
pas fait. Enron, Lehman, Kodak, Sears, Nortel, GM et des centaines d'autres n'y
sont pas. C'est un **biais de survie**, aucune donnée gratuite ne le corrige, et
il flatte la stratégie comme son panier. Un t de 2,98 sur un échantillon biaisé
est un t à propos d'un échantillon biaisé.

C'est aussi pour ça que SPY est imprimé à côté : l'indice porte ses propres
morts, et c'est la seule comparaison qui n'ait pas ce défaut. L'agent y perd
deux points de rendement annuel — et c'est de ce chiffre-là qu'il faut partir,
pas de l'écart de 28 000 points avec le panier.

---

## Ce que le réglage a appris

Tout ce qui suit a été balayé **sur les 60% in-sample uniquement** (1990 →
janvier 2012), puis vérifié hors échantillon.

### Le stop suiveur ne gagne pas sa place

| stop suiveur | 3 ATR | 4 | 5 | 6 | 8 | 10 | désactivé |
|---|---|---|---|---|---|---|---|
| in-sample | +60,2% | +178,8% | +208,1% | +303,8% | **+407,8%** | +416,7% | +432,6% |
| hors éch. | +117,6% | +200,6% | +209,1% | +204,6% | +214,6% | +236,0% | **+244,4%** |

Monotone, dans les deux fenêtres. Ce n'est donc pas un surapprentissage, c'est
une propriété de l'actif : un ATR journalier vaut environ 1,5% du prix, donc
5 ATR est un repli de 7%, et une grande capitalisation rend 7% à l'intérieur
d'une tendance parfaitement saine. Sur bougie 4 h, le même multiple est un
mouvement bien plus petit par rapport à la tendance qu'il essaie de survivre —
c'est pour ça que 5 ATR était le bon réglage en crypto et coupe la moitié du
rendement ici.

Retenu à **8 ATR**, là où il cesse de nuire. Le laisser là plutôt que le
désactiver coûte environ 0,7 point de CAGR hors échantillon et achète un pire
cas défini sur une position très éloignée de son EMA 200 — la seule situation
que la sortie de régime traite lentement. C'est un arbitrage, il est assumé, il
est chiffré.

### Élargir le livre n'ajoute pas de rendement — il retire la dépendance

À stop suiveur constant, seul le nombre de places change (le risque par position
est réduit pour garder le risque total constant) :

| places / risque | in-sample | maxDD | meilleur trade | hors éch. | maxDD |
|---|---|---|---|---|---|
| 12 à 0,6% | +405,6% | 19,5% | 10,8% des gains | +210,5% | 18,1% |
| 18 à 0,4% | +424,6% | 17,8% | 8,4% | +189,8% | 17,1% |
| **25 à 0,3%** | +407,8% | **14,4%** | **7,0%** | **+214,6%** | 17,1% |
| 35 à 0,2% | +365,1% | 13,6% | 6,2% | +191,5% | 16,9% |

Le rendement ne bouge pas — un demi-point de CAGR sépare les quatre lignes dans
les deux fenêtres. Ce qui bouge, c'est le drawdown et la dépendance au meilleur
trade.

**C'est exactement ce que la version crypto avait cherché et n'avait pas
obtenu.** Là-bas, quadrupler l'univers laissait l'indépendance effective à 1,46
et dégradait le t corrigé. Ici l'élargissement fonctionne, et la mesure de
corrélation plus haut dit pourquoi.

### La cadence : l'agent peut dormir une semaine

Sur les 36 ans complets :

| décide toutes les | 1 séance | 2 | 3 | 5 | 10 | 21 |
|---|---|---|---|---|---|---|
| rendement | +1 555,9% | +1 523,9% | +1 309,1% | **+1 593,5%** | +1 071,4% | +857,7% |
| maxDD | 15,9% | 16,9% | 16,5% | **15,5%** | 17,3% | 13,7% |
| positions | 3 875 | 3 448 | 3 161 | 2 648 | 2 062 | 1 289 |

Plat de 1 à 10 séances, cassé à 21. **Se réveiller tous les jours n'apporte rien
par rapport à une revue hebdomadaire**, et c'est le miroir exact du résultat
crypto : là-bas il fallait au moins trois réveils par jour ; ici un par jour est
déjà du zèle. La raison est la même dans les deux cas — une cassure est un état,
pas un événement.

Le défaut reste à 1 : il n'y a aucune raison de dormir, et c'est la cadence qui
prend le plus de décisions, donc celle sur laquelle les statistiques sont les
moins minces. Les séances sans décision continuent de faire travailler les stops
et d'exécuter la file : sinon une cadence basse hériterait d'une protection
qu'elle n'a pas payée — et échapperait aussi à des pertes qu'elle a bien prises.

### Le momentum transversal ne marche toujours pas — et cette fois l'excuse ne tient plus

`cryptoagent` accusait son univers : le momentum transversal est conçu pour des
centaines de valeurs, pas cinq coins corrélés. L'hypothèse est testable ici, et
elle est fausse.

| filtre RS | désactivé | top 10 | top 20 | top 30 | top 45 | top 60 |
|---|---|---|---|---|---|---|
| in-sample | **+407,8%** | +208,3% | +305,1% | +402,6% | +431,3% | +407,1% |
| maxDD | 14,4% | 12,3% | 14,1% | 14,8% | 16,3% | 15,4% |

Serré, il détruit le rendement. Large, il est indiscernable de « désactivé ». Sur
87 valeurs réparties sur onze secteurs, avec l'univers pour lequel ce facteur a
été inventé. Le problème n'était donc pas la taille de l'univers : le momentum
transversal achète ce qui a déjà été le plus fort, cette stratégie gagne sur des
cassures de retardataires, et les deux paris se combattent.

Livré désactivé (`rs_top_k = 0`), implémenté et testé.

### Les sorties partielles perdent, ici aussi

| réglage | in-sample | maxDD |
|---|---|---|
| **désactivé** | **+407,8%** | 14,4% |
| solder 50% à 3R | +345,9% | 13,5% |
| solder 33% à 5R | +359,8% | 13,7% |
| objectif fixe 4R | +269,0% | 13,2% |
| objectif fixe 8R | +316,2% | 13,6% |

La raison est arithmétique, pas empirique, et c'est la même qu'en crypto : avec
41% de réussite l'espérance vit dans la queue droite, et solder la moitié à 3R
coupe en deux chaque trade qui serait allé à 10R sans rien faire pour les 59%
qui perdent.

### Les filtres, un par un

| ablation | in-sample | maxDD | CAGR/DD |
|---|---|---|---|
| référence | +407,8% | 14,4% | **0,55** |
| sans plafond RSI | +374,6% | 16,1% | 0,48 |
| plafond RSI à 70 | +339,0% | 15,2% | 0,48 |
| **sans bande d'ATR** | **+407,8%** | **14,4%** | **0,55** |
| sans ancre SPY | +289,0% | 30,6% | 0,22 |
| ampleur 50% en plus | +395,6% | 16,1% | 0,49 |
| sans garde anti-gap | +418,7% | 14,7% | 0,55 |
| garde anti-gap à 0,5 ATR | +383,0% | 15,8% | 0,49 |

Deux lignes méritent un commentaire.

**La bande d'ATR est inerte.** Retirer le filtre ne change strictement rien, au
dernier point de base : une grande capitalisation américaine ne sort
pratiquement jamais de 0,5%–10% d'ATR journalier. Il est conservé comme garde
contre une valeur qui cesse de se comporter comme telle — suspension de cotation,
OPA, effondrement — et signalé ici comme inerte plutôt que crédité en silence du
résultat.

**La garde anti-gap coûte 11 points in-sample.** Ce n'est pas une amélioration
du rendement et elle n'est pas présentée comme telle : c'est une limite de perte
extrême, sur un événement que 21 ans d'historique in-sample contiennent peu.

### Les coûts

| | rendement | CAGR |
|---|---|---|
| sans coûts (fantasme) | +1 747,5% | 8,48% |
| **tels que livrés** (2 bp de frais + 5 bp de slippage par côté) | **+1 555,9%** | **8,14%** |
| coûts triplés | +1 127,4% | 7,24% |

0,34 point de CAGR pour les coûts modélisés, 0,9 point si on les triple. La
stratégie n'est pas fragile aux coûts — ce qui est normal pour 108 positions par
an sur des grandes capitalisations, et ne serait pas vrai à une fréquence plus
élevée.

### Les actions entières

Sur 36 ans, aucune importance : le compte compose, et la contrainte disparaît dès
qu'il est gros. Elle mord sur un petit compte, et c'est là qu'il faut la
regarder — fenêtre 2016-2026 :

| capital de départ | actions entières | fractionnées | positions |
|---|---|---|---|
| 100 000 USD | +138,4% | +135,5% | 1 231 / 1 236 |
| 10 000 USD | +128,0% | +135,5% | 1 228 / 1 236 |
| 3 000 USD | **+116,7%** | +135,5% | 1 180 / 1 236 |

À 3 000 USD, l'arrondi coûte 19 points sur la décennie et 56 positions : un
budget de risque de 9 USD face à une action à 500 USD ne peut tout simplement
pas exprimer une petite position. `whole_shares = false` rend le
dimensionnement continu si votre courtier le permet.


---

## Les tendances court et moyen terme

Le déclencheur de la stratégie pose une question, sur un horizon : la clôture
est-elle au-dessus du plus-haut des vingt dernières séances ? C'est un bon
déclencheur et une mauvaise description. Il ne dit pas si la valeur grimpe
depuis six mois ou depuis six jours, si son secteur mène ou saigne, ni si le
marché s'élargit ou se resserre — et ce sont les premières questions qu'un
humain devant le même écran poserait.

`trends.py` y répond, sur une échelle d'horizons :

| | 1 semaine | 1 mois | 3 mois | 6 mois | 12 mois |
|---|---|---|---|---|---|
| séances | 5 | 21 | 63 | 126 | 252 |
| | ce qui vient de se passer | court terme | moyen terme | moyen terme confirmé | contexte |

Chaque horizon est rapporté brut **et** divisé par l'ATR% de la valeur. La
normalisation compte plus qu'il n'y paraît : sans elle, un classement de
tendances est un classement de volatilité, et la valeur la plus large de
l'univers mène chaque hausse et chaque baisse. Divisée par l'ATR%, la question
devient « combien de terrain couvert par unité de risque porté ».

```bash
python -m trader trends
```

```
  Market    17 rising, 25 falling of 87   breadth 57% above EMA200, 23% up short-term

  BY SECTOR   (equal weight, strongest medium term first)
  sector                        1w      1m      3m      6m     12m    short   medium  breadth
  Health care                +3.1%   -2.8%  +16.1%  +15.9%  +29.5%    +0.05    +7.03      83%
  Information technology     -0.4%   +3.4%   +3.2%  +37.1%  +54.9%    +0.35    +5.75      67%
  ...
  Consumer discretionary     -1.9%   -9.6%   -9.7%   -8.2%  -15.2%    -2.28    -3.78      11%
  Utilities                  -1.9%   -5.9%   -6.1%   -9.7%   +4.9%    -2.53    -5.09       0%
```

Une valeur haussière sur le trimestre et baissière sur le mois n'est pas en
tendance haussière : elle est en repli. L'étiquette le dit (`rising`,
`falling`, `pullback`, `rebound`) au lieu d'arrondir à l'un ou l'autre.

### Et puis on a essayé de s'en servir pour décider

Deux portes, ajoutées et mesurées avec la même discipline que le reste :

- `min_medium_trend` — n'entrer que sur une valeur dont le score moyen terme
  (rendement 3 et 6 mois sur ATR%) dépasse un seuil ;
- `sector_top_k` — n'entrer que dans les `k` secteurs les plus forts.

La seconde méritait vraiment d'être testée. La mesure de corrélation plus haut
dit que **le secteur est l'unité qui se découple réellement** : 0,467 à
l'intérieur d'un secteur, 0,324 entre deux. Si le momentum transversal devait
marcher quelque part dans cet univers, c'était là. L'hypothèse était bien
motivée.

Elle est fausse.

| | in-sample | hors échantillon |
|---|---|---|
| **désactivé** | +407,8% | **+214,6%** |
| moyen terme > 0 | +416,1% | +211,4% |
| moyen terme > 2 | +417,4% | +189,1% |
| secteurs top 3 | +269,0% | +121,7% |
| **secteurs top 5** | **+460,1%** | **+139,9%** |
| secteurs top 9 | +452,4% | +220,4% |

Le classement s'inverse complètement. `top 5` est le **meilleur** réglage
in-sample et le **pire** hors échantillon ; `top 9` — qui ne filtre presque
rien — est le seul à ne pas nuire, ce qui est une autre façon de dire que le
filtre ne sert à rien. C'est la signature manuelle du surapprentissage, et
c'est exactement l'histoire du `N = 55` de la version crypto, sur une
hypothèse bien mieux argumentée.

La porte moyen terme, elle, ne fait rien : un point d'écart in-sample, un point
d'écart hors échantillon, dans l'autre sens. Elle échange un peu de rendement
contre un taux de réussite un peu meilleur, ce qui est la description d'un
filtre qui retire des trades au hasard.

**Les deux sont livrées désactivées.** Le module de tendances reste : c'est un
bon tableau de bord, et un tableau de bord n'a pas à battre l'indice pour
mériter sa place. Mais il ne décide de rien, et le backtest est identique bit
pour bit quand les portes sont à zéro — c'est testé
(`test_the_gates_change_nothing_while_they_are_off`).

---

## Les actualités : collectées, archivées, et branchées sur rien

L'agent sait lire l'actualité. Il n'en fait rien, et c'est délibéré.

```bash
python -m trader news --sync
```

Il récupère les titres de presse de chaque valeur sur une source publique sans
clé, les score avec un lexique, et les écrit dans la base. **Aucune règle de
cet agent ne lit un titre.** `Engine.step` ne prend pas d'argument « news », et
le moteur n'importe pas le module — l'absence est structurelle plutôt qu'un
réglage à zéro que quelqu'un pourrait basculer, et elle est épinglée par un
test qui inspecte le graphe d'imports.

### Pourquoi

D'abord une raison de principe : **un signal d'actualité n'est pas
backtestable ici.** Tout le reste de ce projet est mesuré sur 36 ans ; les
sources gratuites ne servent que les derniers jours. Il n'y a pas d'archive à
rejouer, donc pas de façon honnête de tester une règle qui en lirait une.

Ensuite deux mesures, prises sur la première collecte complète de l'univers
(854 titres) :

- **12% seulement des titres portent un score.** Le reste est du contenu
  syndiqué — « Is a Recession Coming in 2026? », « Could $5,000 Invested in
  SpaceX Help You Retire a Millionaire? » — servi sous un ticker parce que le
  ticker y apparaît quelque part.
- **L'attribution au ticker n'est pas fiable, et elle l'est mal.** Le flux a
  classé « Warren Buffett Steps Down as Berkshire's Chair » sous **GOOGL** et
  « $949 million fraud verdict costs CVS a business » sous **AMZN**. Les deux
  scorent −1,00, et les deux parlent d'une autre entreprise. Un veto construit
  là-dessus aurait refusé des entrées à cause de la mauvaise journée de
  quelqu'un d'autre.

Le score lui-même est un lexique de mots-clés sur des titres. C'est un
instrument faible et autant le dire franchement : il ne lit pas l'ironie, ne
pèse pas une rumeur contre un dépôt réglementaire, et « Apple crushes
estimates » et « Apple crushed by lawsuit » ne diffèrent que par un mot qu'il
ne comprend pas. Il est là parce qu'il est **auditable** — chaque score se
trace aux mots qui l'ont produit, et le rapport les imprime — pas parce qu'il
est bon.

### Alors à quoi sert cette partie

À une seule chose, et elle est réelle : **construire l'archive.**

Chaque titre vu est écrit une fois et jamais réécrit (`INSERT OR IGNORE`, pas
`OR REPLACE`), en conservant l'instant où il a été *vu*. C'est la seule version
de cette donnée qui pourra un jour être rejouée sans regarder l'avenir. Au bout
d'un an de ticks, il y a un an d'archive ; à ce moment-là une règle
d'actualité devient mesurable, et c'est à ce moment-là qu'il faudra l'écrire —
pas avant. `reset` efface le compte et garde l'archive : ce n'est pas un état
du portefeuille, c'est un enregistrement de ce qui était public à quel moment,
et il ne se reconstitue pas.

C'est aussi pour ça que `news_enabled` est à `false` par défaut : aujourd'hui,
la collecte coûte une requête par valeur et par tick, et ne rapporte qu'une
archive plus longue demain.

---

## Ce que ce projet ne prouve pas

- **Ce n'est pas un conseil financier** et ce n'est pas un système rentable
  démontré. Il gagne deux fois moins que l'indice sur 36 ans.
- **Le biais de survie n'est pas corrigé** et ne peut pas l'être avec des
  données gratuites. C'est le défaut principal, devant tous les autres.
- **Les paramètres ont été touchés après avoir vu les données.** Le
  hors-échantillon est un garde-fou, pas une preuve — et il dit d'ailleurs que
  le réglage n'a rien apporté.
- **Le backtest suppose des exécutions au premier cours coté**, avec des coûts
  constants. L'enchère d'ouverture réelle ne fonctionne pas tout à fait ainsi, et
  un ordre de taille sur une valeur moyenne encore moins.
- **Aucun dividende n'est modélisé comme du cash** : la série est ajustée, donc
  les dividendes sont implicitement réinvestis dans la position. Aucune fiscalité
  n'est modélisée, et sur une stratégie qui tourne 108 fois par an en
  plus-values court terme, ce n'est pas un détail.
- **L'agent ne lit pas l'actualité pour décider**, et tant qu'il n'y a pas
  d'archive assez longue pour le tester, c'est une fonctionnalité absente, pas
  une fonctionnalité désactivée.
- **Passer en réel demanderait bien plus** : gestion des pannes, réconciliation
  d'ordres, règles de pattern day trading, arrondis de lot, et une tolérance au
  risque que ce code ne prétend pas encadrer.

Le projet est solide en tant que *banc d'essai* : le moteur de décision est le
même en live et en backtest, tout est journalisé, les mesures d'honnêteté sont
imprimées par défaut, et le modèle d'exécution refuse un prix que l'agent ne
pouvait pas obtenir.

---

## Architecture

```
trader/
  models.py      dataclasses (Bar, Position, Trade, Signal, Order, Decision)
  config.py      tous les réglages, dans un seul objet figé
  indicators.py  EMA, RSI, ATR (Wilder), canal de Donchian — Python pur, alignés
  strategy.py    fonctions pures : signal d'entrée, de sortie, stop suiveur
  ranking.py     force relative transversale (désactivée par défaut)
  trends.py      tendances 1s/1m/3m/6m/12m, par valeur et par secteur
  regime.py      régime de marché : ancre SPY + ampleur
  risk.py        dimensionnement par distance au stop, actions entières, coupe-circuits
  portfolio.py   compte virtuel : frais, slippage, P&L
  news.py        collecte et score des titres de presse — ne décide de rien
  store.py       SQLite : séances, positions, ordres, trades, décisions, archive presse
  engine.py      LE pas de décision — partagé mot pour mot par le live et le backtest
  scheduler.py   quand se réveiller, sur un calendrier troué
  agent.py       boucle live : sync, rattrapage, tick, persistance, programmation
  backtest.py    rejeu + métriques + mesures de concentration + deux benchmarks
  report.py      status, watchlist, journal
  cli.py         interface en ligne de commande
```

Trois invariants portent le reste :

1. **La séance en cours est toujours écartée.** Agir sur une bougie non
   clôturée fait « repeindre » le signal.
2. **Rien n'est exécuté au prix qui a servi à le décider.** La file d'ordres et
   l'estampille `created_ts` existent pour ça, et rien d'autre.
3. **Le live et le backtest appellent `Engine.step`**, le même code. Si le
   backtest exécutait autre chose, il décrirait un système qui n'existe pas.
4. **Rien qui ne soit pas mesurable n'atteint le compte.** C'est pourquoi les
   actualités sont collectées et archivées mais jamais consultées, et pourquoi
   le moteur n'importe même pas leur module.

## Configuration

Tous les réglages sont dans `trader/config.py`. Pour les surcharger sans
toucher au code :

```bash
python -m trader config > mes-reglages.json
python -m trader --config mes-reglages.json backtest
```

Une clé inconnue déclenche une erreur explicite plutôt qu'un silence.

## Tests

```bash
python -m pytest        # 187 tests, hors ligne, ~11 s
python -m ruff check .
```

Aucun test ne touche au réseau : les séries synthétiques sont déterministes et
à graines fixes, et le calendrier synthétique saute les week-ends, parce qu'une
bonne partie de ce code existe précisément pour vivre avec un calendrier troué.

Les deux tests les plus importants :

- `test_the_strategy_does_not_peek_at_the_future` — rejouer un historique
  tronqué doit reproduire, trade pour trade, les décisions du run complet sur la
  même fenêtre.
- `test_every_fill_happens_at_an_opening_print` — aucun ordre ne se remplit au
  prix qui l'a déclenché.
- `test_no_trading_decision_can_read_a_headline` — le moteur n'importe pas le
  module d'actualités et `Engine.step` ne prend pas d'argument pour en
  recevoir. Une absence se perd facilement dans un refactor, donc elle est
  épinglée plutôt que confiée à un commentaire.

## Pistes suivantes

- **Le biais de survie est le prochain vrai sujet**, et c'est un problème de
  données, pas de code : il faudrait la composition historique d'un indice, qui
  n'est pas gratuite. Tout le reste est du réglage à côté.
- Le réglage in-sample n'a rien rendu hors échantillon. La conclusion raisonnable
  n'est pas de mieux régler, c'est d'arrêter de régler et de chercher un signal
  différent, testé avec la même discipline avant d'être activé.
- **L'archive de presse est la seule chose de ce dépôt qui s'améliore toute
  seule.** Elle ne vaut rien aujourd'hui et vaudra quelque chose dans un an, à
  la seule condition qu'on laisse l'agent tourner. La règle qui la lira reste à
  écrire, et à tester comme le reste.
- L'agent garde 41% de son capital en cash en moyenne, sans rien en faire. Un
  placement monétaire sur cette trésorerie est la seule amélioration de
  rendement de ce document qui ne demande aucun pari — et sur 36 ans à 41%, ce
  n'est pas une décimale.
- La comparaison honnête avec SPY est *à exposition égale*. La calculer et
  l'imprimer serait plus utile que n'importe quel paramètre.
