# Recherche : stratégies d'investissement crypto et actions

Revue de la littérature académique et praticienne, lue avec une seule question
en tête : **qu'est-ce qui, dans ce corpus, change quelque chose pour ces deux
agents ?**

Les deux agents sont des suiveurs de tendance long-only qui entrent sur cassure
d'un canal de Donchian, dimensionnent par la distance au stop et sortent sur un
stop suiveur en ATR. Le crypto tourne sur 20 paires en 4 h avec 9 ans
d'historique ; l'actions sur 87 grandes capitalisations américaines en
journalier. Ce document ne résume pas la littérature pour elle-même : il trie
ce qui **confirme** ce qui est déjà mesuré, ce qui **contredit** une hypothèse
en place, et ce qui constitue un **candidat testable** — dans cet ordre
d'utilité.

Une chose à dire d'emblée, parce qu'elle conditionne tout le reste : la
littérature ne contient aucun réglage à copier. Elle contient des **tests à
faire** et des **manières de mesurer**. Les deux meilleurs apports de cette
recherche sont d'ailleurs une réfutation (le biais de survie n'est pas fatal)
et une méthode (le t corrigé n'est pas la bonne barre).

---

## 1. Le socle : le momentum de série temporelle est le bon cadre

Moskowitz, Ooi et Pedersen (2012) documentent le *time-series momentum* sur 58
contrats futures liquides — indices actions, devises, matières premières,
obligations. Les rendements persistent sur 1 à 12 mois puis se retournent
partiellement au-delà, ce qui est cohérent avec une sous-réaction initiale
suivie d'une sur-réaction retardée. Un portefeuille diversifié de ces
stratégies affiche un alpha substantiel, peu exposé aux facteurs classiques, et
**performe le mieux dans les marchés extrêmes**.

C'est exactement le cadre de ces deux agents, et c'est une bonne nouvelle : la
thèse n'est pas exotique. Trois conséquences concrètes.

**L'horizon de détention est cohérent.** La persistance documentée court de 1 à
12 mois. Un `min_hold_bars` de 4 séances avec un trail à 5 × ATR laisse la
position vivre dans cette fenêtre. Un objectif de gain fixe la tuerait — ce que
le backtest crypto avait déjà trouvé seul (`take_profit_r = 0`).

**La cassure comme état plutôt qu'événement est le bon choix.** Le README
crypto a remplacé un croisement EMA 12/26 (événement d'une bougie, perdu si
manqué) par une cassure de Donchian (état persistant). Sur l'étude Turtle
appliquée au SAFEX, les systèmes 20 et 55 jours produisent des rendements
anormaux significatifs, mais avec une volatilité extrême qui dégrade la
constance du risque ajusté. Le taux de réussite sous 50% avec des séries de
petites pertes est la signature normale du dispositif, pas un défaut à corriger.

**Attention au corpus lui-même.** Sullivan, Timmermann et White (1999)
montrent que les règles d'analyse technique perdent leur pouvoir prédictif sur
les indices américains après le milieu des années 1980, et qu'après correction
du data snooping les meilleures règles ne restent significatives que sur **2
marchés futures sur 17**. Le fait que le cadre soit académiquement établi ne
dit rien de la significativité d'une implémentation particulière. C'est la
section 7.

## 2. La critique qui compte : et si l'edge venait du dimensionnement ?

C'est le résultat le plus dérangeant de cette recherche, et celui qui mérite le
premier test.

Des travaux ultérieurs sur le TSMOM (notamment *Time series momentum and
volatility scaling*, Journal of Financial Markets) concluent que **les
résultats de Moskowitz-Ooi-Pedersen sont largement portés par la normalisation
par la volatilité, pas par le signal de momentum lui-même**. Sans cette
normalisation, le TSMOM et un simple buy-and-hold offrent des rendements
cumulés comparables, avec des alphas qui ne diffèrent pas significativement.

Or ces deux agents font de la normalisation par la volatilité sans l'appeler
ainsi. La formule

```
quantité = (capital × risque%) / (prix d'entrée − stop)
```

avec un stop à N × ATR revient à dimensionner en inverse de la volatilité : un
actif deux fois plus volatil reçoit deux fois moins de notionnel. Ce n'est pas
un détail d'implémentation, c'est peut-être **le moteur**.

Le test est direct et bon marché. Rejouer le backtest avec un dimensionnement à
notionnel fixe (même nombre de places, même capital par place, stop conservé
pour la sortie mais retiré du calcul de taille) et comparer. Trois issues :

| Résultat | Ce qu'il faut en conclure |
|---|---|
| L'edge survit | Le signal de cassure porte quelque chose. Tout le travail sur le déclencheur était légitime. |
| L'edge s'effondre | Le moteur est le dimensionnement. Le choix N=20 contre N=55 était du bruit ajusté, et la recherche doit se déplacer vers la construction de portefeuille. |
| L'edge s'inverse | Vérifier le test avant de conclure quoi que ce soit. |

La deuxième issue est la plus probable au vu de la littérature, et ce serait le
résultat le plus utile que ces projets puissent produire : il redirigerait
l'effort de « quel déclencheur ? » vers « quelle taille ? », là où les preuves
sont plus solides.

Le corollaire, côté portefeuille : Moreira et Muir (2017) montrent que le
*volatility targeting* augmente le Sharpe du marché actions américain et des
facteurs long-short — d'environ un cinquième pour le marché large — en évitant
les mois de forte volatilité dont le rendement par unité de risque est mauvais.
Mais l'effet n'est **pas universel** : négligeable sur obligations, devises et
matières premières, et le *Conditional Volatility Targeting* documente des cas
où le targeting conventionnel échoue à améliorer les marchés actions mondiaux
et **aggrave les drawdowns**. À tester comme candidat, pas à activer par
défaut.

## 3. Crypto : la littérature confirme la réfutation déjà faite

Liu, Tsyvinski et Wu (Journal of Finance, 2022) établissent le modèle à trois
facteurs de la crypto — marché, taille, momentum — qui capture la coupe
transversale des rendements attendus. Dix caractéristiques construisent des
long-short significatifs, tous expliqués par ces trois facteurs.

Mais le résultat qui compte ici est plus précis. Han, Kang et Ryu (*Time-Series
and Cross-Sectional Momentum in the Cryptocurrency Market: A Comprehensive
Analysis under Realistic Assumptions*) concluent que **l'évidence du momentum
de série temporelle est forte, celle du momentum transversal est faible**. Et
sous hypothèses réalistes — coûts de transaction, fluctuations intra-journée —
beaucoup de portefeuilles de momentum sont liquidés, et beaucoup de ceux dont
les rendements sont statistiquement significatifs dégagent des profits
insignifiants.

C'est une **corroboration externe directe** de ce que le README crypto a mesuré
seul : le filtre de force relative (`rs_top_k`), testé en in-sample, en
walk-forward et sur univers élargi, est livré désactivé parce qu'il ne tient
pas. La littérature dit la même chose sur des données différentes. L'hypothèse
« notre univers est trop petit » avait déjà été écartée par la mesure ; elle est
aussi écartée par le corpus. **Ne pas y revenir.**

Deux précisions utiles :

- Dobrynskaya, sur les 2 000 plus grosses crypto-monnaies (2014-2020), trouve
  du momentum sur des horizons **courts, de deux à quatre semaines**, et un
  **retournement significatif au-delà d'un mois**. Le `rs_lookback` de 42
  bougies 4 h (une semaine) est donc dans la zone de momentum ; un lookback
  trimestriel serait dans la zone de retournement. Si le classement devait être
  re-testé un jour, ce serait sur cet horizon-là, pas plus long.
- *A Decade of Evidence of Trend Following Investing in Cryptocurrencies*
  rapporte des Sharpe plein-période d'environ 1,09 (SMA), 1,35 (EMA) et 1,32
  (DEMA), mais des tranches 2015-2018 franchement médiocres, Sharpe sous 1 voire
  négatifs — et **des coûts de transaction supposés négligeables**. La
  conclusion des auteurs est que la crypto se comporte, pour un suiveur de
  tendance, comme une matière première du XXᵉ siècle. Les coûts modélisés à
  0,10% par côté plus 5 bp de slippage dans l'agent crypto sont donc plus
  honnêtes que ce papier, et l'écart explique une partie de la différence de
  résultats.

**Le régime 2025-2026 a changé.** L'institutionnalisation post-ETF réduit la
volatilité (une mesure sur la *power hour* passe de 1,074% à 0,862%, soit
−19,7%) et rend le marché *flow-driven* : sans flux institutionnel entrant, le
momentum de prix peine. S'ajoute la décroissance post-publication des anomalies
documentées. Pour un suiveur de tendance long-only, moins de volatilité et des
tendances plus dépendantes des flux, c'est un environnement plus difficile que
l'historique de backtest. À anticiper, et surtout **à ne pas re-tuner dessus**.

## 4. Actions : ce que le filtre EMA 200 achète vraiment

Le `market_anchor = SPY` et le filtre `regime_ema = 200` de l'agent actions
reposent sur le résultat de Faber (2007) : une règle de moyenne mobile à 10
mois appliquée à plusieurs classes d'actifs réduit fortement le drawdown maximal
et la volatilité en conservant des rendements comparables.

Il faut lire la deuxième moitié du résultat. **Le timing par moyenne mobile
réduit généralement un peu le rendement annualisé absolu**, à cause du retard
intrinsèque de l'indicateur. Un test sur le S&P 500 donne 6,45% de CAGR contre
7% en buy-and-hold, pour un drawdown maximal ramené à 28%. Siegel, sur le Dow
depuis 1900, conclut que le timing améliore le risque ajusté même après coûts
complets, mais **reste en retard en rendement absolu**.

Ce que cela implique pour l'agent actions est net : le filtre de régime est un
outil de **survie**, pas de rendement. Le benchmark SPY du `report.py` est donc
le bon choix, et il faut s'attendre à le battre en Sharpe et en drawdown
plutôt qu'en CAGR. Un agent actions long-only avec filtre de régime qui
afficherait un CAGR très supérieur à SPY sur le backtest devrait être suspecté
d'un biais avant d'être célébré.

**Le momentum transversal, en actions, est une autre histoire qu'en crypto.**
C'est là que la littérature est la plus solide (Jegadeesh-Titman), et c'est
l'inverse du verdict crypto de la section 3. Mais l'horizon documenté est le
**12-1** : rendement sur 12 mois en sautant le mois le plus récent. Or
`rs_lookback = 63` séances (un trimestre) n'est ni le 12-1, ni un horizon où
le momentum actions est bien documenté. Si le classement transversal est
re-testé côté actions, il doit l'être sur environ **252 séances de formation
moins les 21 dernières**, pas sur 63. C'est une correction de configuration,
pas une réécriture.

**Attention au dual momentum.** Combiner sélection relative et filtre de
tendance absolue (Antonacci, 2014) est séduisant et bien exposé, mais le
contrôle hors échantillon est sévère : depuis la publication du livre, GEM est
en retard sur un simple 60/40 et sur SPY, et son avantage en drawdown était
essentiellement un effet 2008. C'est un rappel utile : un backtest publié dont
l'avantage tient à un épisode unique ne se transporte pas. Le README crypto
fait la même remarque sur son propre backtest porté par un trade.

## 5. Le seul levier qui puisse porter le t : la breadth effective

Le README crypto pose le problème correctement : le t corrigé est passé de 0,66
à 1,24, il faudrait environ trois fois plus pour atteindre 2, et Binance n'a pas
trois fois plus. La loi fondamentale de la gestion active dit pourquoi et où
chercher.

```
IR = IC × √(nombre de paris indépendants)
```

Le terme qui compte est **indépendants**. Des actifs corrélés ne sont pas des
paris distincts : à corrélation élevée, malgré plusieurs titres, il n'y a
vraiment qu'un pari. Le S&P 500 ne représente pas 500 opportunités décorrélées,
et l'usage recommandé est une ACP pour compter les paris réellement
indépendants d'un univers.

C'est exactement la mesure déjà faite côté crypto : 20 paires, corrélation
moyenne par paires de 0,67, soit **1,46 actif indépendant**. Élargir de 5 à 20
paires n'a presque rien acheté en puissance statistique, et la loi explique
pourquoi. Les travaux sur les portefeuilles de suivi de tendance vont dans le
même sens : pour *n* actifs indistinguables et une structure de corrélation
donnée, le Sharpe optimal vaut √n fois celui d'un actif seul — le gain est en
racine du nombre de paris **indépendants**, pas du nombre de lignes.

D'où les deux conclusions pratiques.

**Passer aux actions est le bon coup, et pour la bonne raison.** Les secteurs
se découplent réellement là où les crypto-monnaies bougent ensemble. Mais il
faut le **mesurer** sur l'univers actions avec la même méthode que côté crypto
(corrélation moyenne par paires et comptage ACP) plutôt que le supposer. Sur 87
grandes capitalisations américaines, la corrélation moyenne restera élevée — un
facteur marché commun traverse tout — et le nombre de paris indépendants sera
très inférieur à 87. Cette mesure est le premier chiffre à produire côté
actions, avant tout réglage de stratégie.

**Le saut vient des classes d'actifs, pas des lignes supplémentaires.** Le
travail de Man Group sur le mix optimal d'un suiveur de tendance quantifie ce
que l'intuition suggère : ajouter des marchés complémentaires au-delà des
actions, obligations et métaux — devises, énergie, céréales, *softs* — atteint
une corrélation essentiellement orthogonale (0,01) avec un cœur de primes de
risque classique. Et le suivi de tendance appliqué conjointement aux actions,
matières premières, devises et obligations affiche une hausse de Sharpe
annualisé de 44 à 52% par rapport aux approches mono-classe sur 1980-2020.

Conséquence opérationnelle immédiate, et c'est probablement **la piste la plus
rentable de tout ce document** : l'agent actions lit déjà du journalier gratuit
chez Yahoo. Les mêmes séries existent, gratuitement et sur vingt à trente ans,
pour des ETF qui représentent d'autres classes d'actifs — obligations longues
et intermédiaires, or, panier de matières premières, dollar, immobilier coté,
actions internationales et émergentes. Aucune ligne de `strategy.py` ne change :
ce sont des symboles de plus dans `universe`, avec le même moteur. Le gain
attendu n'est pas du rendement, c'est de la **puissance statistique** — le seul
manque que le projet crypto a identifié sans pouvoir le combler.

## 6. Le biais de survie n'est pas fatal — cette affirmation doit être corrigée

`stockagent/trader/config.py` affirme que le biais de survie « est le pire
problème du backtest, qu'il ne peut pas être corrigé avec des données gratuites ».
La deuxième moitié de la phrase est trop forte, et c'est la principale
contradiction que cette recherche apporte au code existant.

Ce qui est gratuit :

- **La composition point-in-time du S&P 500 est reconstructible gratuitement**,
  à partir de l'historique des révisions de la page Wikipédia de l'indice ; la
  méthode est publiée avec son code Python. Elle donne, pour chaque date, la
  liste des membres telle qu'elle était, sans regard en arrière.
- Des fournisseurs tiennent un registre continu des changements d'appartenance
  (EODHD depuis le 4 avril 2012, Norgate avec les titres radiés), ce qui permet
  une reconstruction propre — payante, mais cela situe la frontière.
- **Un backtest sur SPY est moins biaisé qu'un backtest sur les membres
  actuels**, puisque l'ETF intègre les changements de composition au fil du
  temps.

Ce qui reste manquant : Yahoo et la plupart des API de courtiers **ne
conservent pas l'historique de prix des titres radiés**. La composition
historique est donc gratuite, les prix des morts ne le sont pas.

Le biais passe ainsi de « fatal et non mesurable » à « réductible et
quantifiable », en trois étapes de coût croissant :

1. **Le quantifier.** Comparer le backtest sur l'univers actuel à un backtest
   sur SPY seul avec la même stratégie. L'écart borne l'ampleur du biais de
   sélection d'univers. Gratuit, faisable tout de suite.
2. **Supprimer le regard en arrière sur l'appartenance.** N'autoriser une
   entrée sur un symbole que si ce symbole appartenait à l'indice à cette date.
   Cela n'élimine pas le biais — un membre de 2005 radié depuis reste absent
   des prix — mais cela supprime la partie du biais qui vient d'avoir *choisi*
   l'univers en 2026.
3. **Accepter la frontière.** Le reste exige des données payantes. Le dire dans
   le README, avec le chiffre de l'étape 1, vaut mieux que de le déclarer
   incorrigible.
   L'ordre de grandeur mérite d'être pris au sérieux : un test sur les 20 plus
   petites sociétés du S&P 500 affiche une croissance plus de **cinq fois**
   supérieure avec les membres actuels qu'avec un jeu de données incluant les
   radiés.

## 7. Mesurer honnêtement : le t corrigé n'est pas la bonne barre

Le README crypto vise un t de 2. C'est la barre d'un test unique. Or ces
projets ont testé, de leur propre aveu, un déclencheur contre un autre (N=20
contre N=55), des paramètres par tiers de capitalisation, des sorties
partielles, un filtre de force relative, 5 puis 20 symboles, deux longueurs
d'historique, plusieurs cadences. **Sous essais multiples, le maximum observé
est inflaté même si tous les candidats sont du pur bruit**, et la probabilité de
retenir une stratégie surajustée croît vite avec le nombre d'essais.

Trois outils répondent précisément à cela, et aucun ne demande de nouvelles
données :

| Outil | Ce qu'il corrige | Disponibilité |
|---|---|---|
| **Deflated Sharpe Ratio** (Bailey, López de Prado) | Biais de sélection sous essais multiples **et** non-normalité des rendements | Formule fermée, implémentable directement |
| **Probability of Backtest Overfitting** | Probabilité que le choix in-sample ne survive pas hors échantillon | Combinatoire sur les découpages existants |
| **Hansen SPA** (2005), extension studentisée du Reality Check de White (2000), sur bootstrap stationnaire de Politis-Romano | Le meilleur de N règles bat-il vraiment la référence ? | `arch.bootstrap.SPA` en Python |

Le SPA est le plus adapté ici : il est plus puissant que le Reality Check et
moins sensible à l'inclusion de candidats médiocres, ce qui compte quand on
compare une grille de paramètres dont la plupart sont mauvais. Le bootstrap
stationnaire rééchantillonne des blocs de longueur géométrique, ce qui préserve
l'autocorrélation des rendements — indispensable pour une stratégie de tendance.

La recommandation concrète, et elle est inconfortable : **reporter le DSR à côté
du t corrigé, en déclarant le nombre d'essais**. Il est probable que le t de
1,24 vaille encore moins qu'il n'en a l'air. C'est la direction honnête, et
c'est celle que la culture affichée de ces deux README — « mesuré, puis
réfuté » — impose.

Un point d'hygiène, tant qu'on y est : la section « Ce que ce projet ne prouve
pas » du README crypto parle encore de « deux ans, cinq symboles, un régime de
marché » alors que les sections suivantes documentent 9 ans et 20 paires. La
section des limites est en retard sur le reste du document ; c'est le genre de
décalage qui décrédibilise à tort un travail par ailleurs rigoureux.

## 8. Deux candidats testables que la littérature soutient

Au-delà des corrections ci-dessus, deux ajouts que ces agents n'ont pas essayés
et qui sont adossés à des résultats publiés.

### 8.1 Diversifier l'horizon de tendance au lieu de le choisir

Le README crypto raconte comment N=55 a failli être choisi contre N=20, et le
travail de sélection qui a tranché. La littérature CTA suggère que **la question
est mal posée**. Une grande partie de la variation de performance entre gérants
et indices reflète des différences de **mix d'horizons** — rapide, moyen, lent —
plutôt que des stratégies fondamentalement différentes, et l'horizon lent
ancre le portefeuille sur les dérives longues et **stabilise le comportement en
drawdown**.

Le test : faire tourner deux horizons en parallèle (par exemple
`breakout_bars` 20 et 55) avec le budget de risque partagé entre eux, plutôt
que de choisir. L'attente n'est pas un rendement supérieur mais une dépendance
moindre au choix d'un paramètre unique — ce qui est aussi une réduction du
surajustement, donc directement lié à la section 7. C'est le candidat que je
classerais premier parmi les ajouts de stratégie.

### 8.2 Le stop, mesuré comme un moteur de rendement et non comme une assurance

Han, Zhou et Zhu (*Taming Momentum Crashes: A Simple Stop-Loss Strategy*)
appliquent un stop à 10% sous le prix de début de mois au décile supérieur de
momentum 6 mois sur les actions américaines, 1926-2011. Les résultats sont
frappants : perte mensuelle maximale ramenée de −49,79% à −11,36% en
équipondéré et de −64,97% à −23,28% en pondéré par la capitalisation, Sharpe
**plus que doublé**, rendement mensuel moyen porté de 1,01% à 1,73% (contre
0,62% en buy-and-hold) et écart-type mensuel réduit de 6,07% à 4,67%.

Le point important n'est pas le chiffre de 10%, qui ne se transporte pas tel
quel à un trail en ATR sur bougies 4 h. C'est que **le stop y agit sur le
rendement, pas seulement sur le risque**. Le trail à 5 × ATR de ces agents est
large ; la littérature suggère qu'un stop plus serré peut augmenter le
rendement moyen au lieu de le rogner.

Avec une réserve méthodologique explicite : balayer `trail_atr_mult` sur une
grille est précisément l'essai multiple que la section 7 pénalise. Le test doit
être **pré-enregistré** — deux ou trois valeurs décidées à l'avance, mesurées en
walk-forward, avec le nombre d'essais déclaré dans le DSR.

C'est aussi la piste qui attaque directement le problème identifié dans les
« Pistes suivantes » du README crypto : le gain moyen vaut 3,8 fois la perte
moyenne, et c'est cet écart qu'il faut élargir. Les sorties partielles ont
montré que le raccourci évident allait dans le mauvais sens. Resserrer la queue
des pertes est l'autre côté du même levier, et il est mieux documenté.

## 9. Hors périmètre : ce qui n'est pas une piste pour ces agents

À dire une fois pour ne pas y revenir. Ces stratégies sont réelles et
documentées, mais **incompatibles avec la contrainte long-only au comptant**
que les deux agents se sont donnée.

- **Carry de financement et cash-and-carry crypto.** Acheter le spot et vendre
  le perpétuel à quantité égale pour encaisser le funding, delta-neutre. La BIS
  en fait une étude dédiée (*Crypto carry*, working paper n° 1087). Ce n'est pas
  de l'arbitrage sans risque : les trois risques sont le retournement du taux
  de financement, le risque de base et les coûts d'exécution, et quand le
  funding est faible les frais dépassent la collecte. Cela exige des
  perpétuels, donc du short et de la marge. Un autre agent, pas celui-ci.
- **Momentum long-short.** Les facteurs de Liu-Tsyvinski-Wu et le momentum
  actions classique sont construits en long-short. La jambe courte est souvent
  celle qui porte l'alpha, et c'est celle qui crashe. Un long-only capture au
  mieux la moitié du facteur — ce qui est une raison de plus de ne pas attendre
  d'un agent long-only les chiffres des papiers.
- **Levier.** Le *volatility targeting* de la section 2 suppose généralement de
  pouvoir monter au-dessus de 1× quand la volatilité est basse. Sans levier, le
  targeting ne peut que réduire l'exposition, ce qui en change la nature : on
  garde le frein, on perd l'accélérateur, et l'effet mesuré dans la littérature
  ne s'applique plus tel quel.

**Un mot sur le contexte 2025.** Les CTA ont connu des drawdowns record en
2025. Les études de long terme rappellent des périodes de non-performance
pluriannuelles — 2009-2013 en est l'exemple canonique. Un agent de suivi de
tendance lancé aujourd'hui peut parfaitement passer deux ou trois ans sous son
backtest sans que rien ne soit cassé. Le coupe-circuit à −35% depuis le pic et
la reprise à −15% sont dimensionnés pour ça ; il faut simplement ne pas
interpréter une traversée du désert comme un signal de re-tuning.

## 10. Priorités

Classées par rapport entre information produite et coût, pas par ambition.

| # | Action | Pourquoi maintenant | Où |
|---|---|---|---|
| 1 | **Backtest à notionnel fixe contre dimensionnement par le stop** | Répond à « l'edge est-il le signal ou la taille ? ». Une seule mesure, conséquences maximales sur tout le reste de la feuille de route. | `risk.py`, §2 |
| 2 | **Mesurer la breadth effective de l'univers actions** (corrélation moyenne par paires + ACP) | Chiffre manquant côté actions ; la thèse « les secteurs décorrèlent » est pour l'instant supposée, pas mesurée. | `backtest.py`, §5 |
| 3 | **Ajouter des ETF multi-classes à l'univers actions** (obligations, or, matières premières, dollar, immobilier, international) | Le seul levier connu sur la puissance statistique. Données gratuites, journalières, longues. Aucun changement de moteur. | `config.py`, §5 |
| 4 | **Reporter le Deflated Sharpe Ratio avec le nombre d'essais déclaré** | Le t corrigé n'est pas la bonne barre sous essais multiples. Va probablement dégrader le chiffre affiché ; c'est le but. | `backtest.py`, §7 |
| 5 | **Quantifier le biais de survie contre SPY, puis filtrer l'appartenance point-in-time** | La composition historique est gratuite, contrairement à ce qu'affirme le code. | `data.py`, `config.py`, §6 |
| 6 | **Diversifier l'horizon de tendance (20 et 55 en parallèle)** | Réduit la dépendance à un paramètre choisi sur les données. Soutenu par la pratique CTA. | `config.py`, `engine.py`, §8.1 |
| 7 | **Test pré-enregistré sur le serrage du stop suiveur** | Attaque le ratio gain/perte de 3,8 par le côté documenté. À faire après le 4, pour que le DSR compte les essais. | `strategy.py`, §8.2 |
| 8 | **Re-tester la force relative côté actions, en 12-1** | `rs_lookback = 63` n'est pas l'horizon documenté. Verdict crypto (désactivé) à conserver. | `ranking.py`, §4 |
| 9 | Pondération du risque par clusters de corrélation (HRP) au lieu des tiers de capitalisation | Les clusters de corrélation ne coïncident pas avec les secteurs ni avec la taille. Candidat propre mais moins urgent. | `risk.py` |

Une remarque sur l'ordre : les items 1, 2, 4 et 5 **produisent de l'information
sans changer la stratégie**. Les items 3, 6, 7, 8 et 9 la changent. Faire les
premiers d'abord n'est pas de la prudence, c'est de l'économie : deux d'entre
eux peuvent rendre inutile une partie des seconds.

---

## Sources

Momentum de série temporelle et suivi de tendance
- [Time series momentum — Moskowitz, Ooi, Pedersen (JFE 2012)](https://www.sciencedirect.com/science/article/pii/S0304405X11002613) · [PDF](https://elmwealth.com/wp-content/uploads/2017/06/timeseriesmomentum.pdf) · [données AQR](https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Original-Paper-Data)
- [Time series momentum and volatility scaling (Journal of Financial Markets)](https://www.sciencedirect.com/science/article/abs/pii/S1386418116301379)
- [Time Series Momentum: Theory and Evidence — Alpha Architect](https://alphaarchitect.com/time-series-momentum-theory-and-evidence/)
- [A Century of Evidence on Trend-Following Investing — AQR](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)
- [Optimal trend following portfolios](https://arxiv.org/pdf/2201.06635)

Dimensionnement et volatilité
- [Harnessing Volatility Targeting in Multi-Asset Portfolios — Research Affiliates](https://www.researchaffiliates.com/publications/press-exclusive/1014-harnessing-volatility-targeting)
- [The Impact of Volatility Targeting — Man Group](https://www.man.com/insights/the-impact-of-volatility-targeting)
- [Conditional Volatility Targeting (Financial Analysts Journal)](https://www.tandfonline.com/doi/full/10.1080/0015198X.2020.1790853)
- [An Introduction to Volatility Targeting — QuantPedia](https://quantpedia.com/an-introduction-to-volatility-targeting/)

Crypto
- [Common Risk Factors in Cryptocurrency — Liu, Tsyvinski, Wu (Journal of Finance 2022)](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13119) · [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3379131)
- [Time-Series and Cross-Sectional Momentum in the Cryptocurrency Market under Realistic Assumptions — Han, Kang, Ryu](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565) · [PDF](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- [Cryptocurrency Momentum and Reversal — Dobrynskaya](https://conference.hse.ru/files/download_file_ex?hash=FAE0AB2DC7A67656E89A0B1CB27D8C7D&id=3B5EE9A5-0B18-458A-9458-B4ED0F6C6664)
- [A Decade of Evidence of Trend Following Investing in Cryptocurrencies](https://arxiv.org/abs/2009.12155)
- [Cryptocurrency anomalies and economic constraints](https://www.sciencedirect.com/science/article/abs/pii/S1057521924001509)
- [Crypto carry — BIS Working Paper n° 1087](https://www.bis.org/publ/work1087.pdf)
- [Factor-based investing emerges in crypto](https://institutionalassetmanager.co.uk/gaining-momentum-factor-based-investing-emerges-in-crypto/)

Actions
- [A Quantitative Approach to Tactical Asset Allocation — Faber (2007)](https://www.trendfollowing.com/whitepaper/CMT-Simple.pdf) · [données](https://mebfaber.com/timing-model/)
- [Trend Following Strategy in S&P 500 — QuantifiedStrategies](https://www.quantifiedstrategies.com/trend-following-system-sp-500/)
- [Taming Momentum Crashes: A Simple Stop-Loss Strategy — Han, Zhou, Zhu](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2407199) · [PDF](https://www.cicfconf.org/sites/default/files/paper_811.pdf)
- [Stop-losses to Avoid Stock Momentum Crashes? — CXO Advisory](https://www.cxoadvisory.com/technical-trading/stop-losses-to-avoid-stock-momentum-crashes/)
- [Dual Momentum out of sample: does GEM still beat buy-and-hold?](https://quant4free.com/analysis/dual-momentum/)
- [Fragility Case Study: Dual Momentum GEM — Newfound](https://blog.thinknewfound.com/2019/01/fragility-case-study-dual-momentum-gem/)

Diversification et construction de portefeuille
- [The Fundamental Law of Active Management — ReSolve](https://investresolve.com/tactical-alpha-theory-practice-pt-i-fundamental-law-of-active-management/) · [la faille de la loi de Grinold](https://investresolve.com/the-case-for-tactical-alpha-part-2-the-fundamental-flaw-of-grinolds-fundamental-law/)
- [A Trend Following Deep Dive: The Optimal Market Mix — Man Group](https://www.man.com/insights/trend-following-optimal-market-mix)
- [What Trend Following Actually Adds to a Risk-Premia Core](https://beyondpassive.substack.com/p/what-trend-following-actually-adds)
- [Hierarchical Risk Parity — QuantPedia](https://quantpedia.com/hierarchical-risk-parity/) · [comparaison hors échantillon (Empirical Economics)](https://link.springer.com/article/10.1007/s00181-026-02900-x)
- [Decoding CTA Allocations by Trend Horizon — CFA Institute](https://rpc.cfainstitute.org/blogs/enterprising-investor/2026/decoding-cta-allocations-by-trend-horizon)
- [CTA Hedge Fund Report 2025 — With Intelligence](https://www.withintelligence.com/insights/cta-hedge-fund-report/)

Méthodologie et surajustement
- [The Deflated Sharpe Ratio — Bailey, López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) · [PDF](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- [Statistical Overfitting and Backtest Performance](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf)
- [A Test for Superior Predictive Ability — Hansen (2005)](https://cdr.lib.unc.edu/downloads/zp38wf793)
- [Testing the Predictive Ability of Technical Analysis Using a Stepwise SPA Test](https://homepage.ntu.edu.tw/~ckuan/pdf/Step-SPA-20090720.pdf)
- [Re-Examining the Profitability of Technical Analysis with White's Reality Check and Hansen's SPA Test](https://www.researchgate.net/publication/256066609_Re-Examining_the_Profitability_of_Technical_Analysis_with_White's_Reality_Check_and_Hansen's_SPA_Test)
- [arch.bootstrap.SPA — implémentation Python](https://arch.readthedocs.io/en/latest/multiple-comparison/generated/arch.bootstrap.SPA.html)

Biais de survie
- [Creating a Survivorship Bias-Free S&P 500 Dataset with Python — Teddy Koker](https://teddykoker.com/2019/05/creating-a-survivorship-bias-free-sp-500-dataset-with-python/)
- [Survivorship-bias free S&P 500 constituent lists](https://riazarbi.github.io/quant/backtesting-sp500-constituent-history/)
- [S&P 500 Historical Constituents Data — EODHD](https://eodhd.com/financial-apis-blog/sp-500-historical-constituents-data)
- [Addressing Survivorship Bias — EODHD Academy](https://eodhd.com/financial-academy/financial-faq/survivorship-bias-free-financial-analysis)
- [Survivorship Bias: Revealing the Hidden Truths of the S&P 500](https://medium.com/@jpolec_72972/survivorship-bias-revealing-the-hidden-truths-of-the-s-p-500-b70da639af9f)

Cassures de canal
- [Testing a price breakout strategy using Donchian Channels (UCT)](https://open.uct.ac.za/handle/11427/21754)
