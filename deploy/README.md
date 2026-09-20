# Héberger l'agent sur un Compute Module 4

L'agent décide une fois par séance et dort le reste du temps — soit, en
comptant les week-ends et les jours fériés, plus de 99 % de sa vie. Sur un
portable, « est-ce qu'il tourne encore ? » se règle en regardant le terminal.
Sur une carte posée dans un placard, sans écran, avec une connexion qui va et
vient, il faut que la réponse soit donnée par la machine — et surtout que
l'agent **refuse de trader** quand la réponse est non.

Ce dossier contient de quoi faire ça : un service systemd, un minuteur de
sauvegarde, et un script qui installe les deux.

```bash
git clone <url> stockagent && cd stockagent
sudo ./deploy/install.sh
```

C'est tout. Le reste de ce fichier explique ce que ça a installé et ce qui se
passe quand quelque chose tombe.

---

## 1. Le matériel, et ce qu'il impose

Testé sur un **Compute Module 4** sous Raspberry Pi OS Lite 64 bits (Debian
bookworm, systemd 252). N'importe quel Pi sous systemd fera l'affaire.

Trois propriétés de cette carte changent la façon d'écrire le code, pas
seulement la façon de l'installer :

**Pas d'horloge sauvegardée.** Le CM4 n'a pas de pile RTC. Coupez le courant :
au redémarrage il croit qu'on est au moment où il s'est arrêté — ou en 1970.
Or tout l'ordonnancement de l'agent est « la prochaine cloche de clôture, heure
de New York », et `drop_unclosed` compare une séance à `now`. Une horloge
fausse ne fait pas un dégât cosmétique : elle jette des séances valides, ou en
accepte une qui est encore en cours de cotation — et une séance en cours, c'est
un signal qui repeint. D'où trois choses : le service est ordonné après
`time-sync.target`, l'installeur active `systemd-time-wait-sync` pour que cette
cible veuille bien dire « le NTP a répondu », et l'agent lui-même attend une
horloge crédible avant son premier tick (`await_clock`). Si vous avez un module
RTC (DS3231 sur l'I²C), branchez-le : ça supprime l'attente, ça ne supprime pas
les garde-fous.

**Le stockage est une carte SD ou de l'eMMC.** Trois conséquences, et la
troisième est propre à cet agent. La base est ouverte en `synchronous=FULL` :
sous le réglage par défaut de WAL, les derniers commits restent en cache et une
coupure de courant les emporte. Le journal systemd est plafonné à 200 Mo, parce
qu'un disque plein est un agent qui ne peut plus écrire sa propre base. Et
surtout : une synchronisation ne réécrit plus toute la table. Les prix actions
sont *rétroactivement* retraités — un split réécrit toutes les séances
antérieures — donc chaque synchronisation remplaçait la série entière de chaque
valeur : plusieurs centaines de milliers de lignes par jour sur une carte flash
pour enregistrer une clôture. `replace_bars` n'écrit désormais que la
différence, et le résultat est strictement identique.

**Mémoire limitée.** Un tick vivant ne charge plus l'historique complet de
chaque valeur, seulement la fenêtre dont il a besoin (`Settings.live_window`,
soit l'échauffement, le rattrapage et de quoi couvrir l'horizon de tendance à
12 mois). Mesuré sur les 88 noms : 55 Mo de RSS au pic, contre de l'ordre de
600 si on lisait les trente-six ans de séances. Le backtest, lui, lit tout,
volontairement — lancez-le sur une machine de bureau. 2 Go de RAM suffisent
largement pour les deux agents ; 1 Go passe si vous ne backtestez pas sur la
carte.

---

## 2. Ce que l'installeur fait

| | |
|---|---|
| `/opt/stockagent` | le code, copié depuis le dépôt, en lecture seule pour le service |
| `/var/lib/stockagent/live.db` | toute la mémoire de l'agent : compte, positions, ordres au repos, séances, décisions |
| `/var/lib/stockagent/backups/` | sept instantanés quotidiens |
| utilisateur système `stockagent` | sans login, sans home, sans capacité |
| `stockagent.service` | l'agent, démarré au boot |
| `stockagent-backup.timer` | l'instantané quotidien |

Le code est **copié** et non lié : l'unité pose `ProtectHome=true`, donc un
dépôt sous `/home` serait invisible pour le service. Relancer le script après
un `git pull` est le chemin de mise à jour.

Rien n'est installé dans le venv. Le projet n'a aucune dépendance d'exécution,
et un venv qu'il faut reconstruire avec internet est une chose de plus qui peut
échouer sur une carte dont le but est justement de se passer d'internet.

L'installeur touche aussi trois réglages hors du projet, et c'est délibéré :
`systemd-timesyncd` (l'horloge), la taille du journal (la carte SD), et le
chien de garde matériel du BCM2711 — `--no-watchdog` pour s'en passer.

---

## 3. Vérifier

```bash
systemctl status stockagent            # actif, et la ligne d'état du prochain réveil
journalctl -u stockagent -f            # le direct
journalctl -u stockagent -b            # depuis le dernier démarrage

sudo -u stockagent /opt/stockagent/.venv/bin/python -m trader \
  --db /var/lib/stockagent/live.db health
```

`health` répond sur les trois questions qui comptent — l'horloge, la fraîcheur
des données, l'intégrité de la base — et sort avec un code non nul si l'une
d'elles ne va pas, ce qui le rend utilisable depuis n'importe quelle
supervision extérieure :

```
health: healthy
  clock      ok   NTP synchronised
  database   ok
  data       ok    newest session opened 21.4h ago (limit 4.0d)
  last tick  2026-09-18 20:20 UTC (14.2h ago)
  next run   2026-09-21 20:20 UTC
  positions  6 open, 2 queued
```

Un alias rend ça vivable :

```bash
echo "alias stock='sudo -u stockagent /opt/stockagent/.venv/bin/python -m trader --db /var/lib/stockagent/live.db'" >> ~/.bashrc
# puis : stock health | stock status | stock log | stock watch | stock trends
```

---

## 4. Ce qui se passe quand ça tombe

| Panne | Ce qui se passe | Où ça se voit |
|---|---|---|
| **Redémarrage** (voulu ou non) | le service repart au boot, sans session ouverte ni `linger`. L'agent relit la base : liquidités, positions, ordres au repos, cliquet de drawdown, dernière séance vue. Il rejoue les séances closes pendant l'arrêt — les stops au repos ont pu être touchés et les ordres en attente exécutés à une ouverture, les deux vivent chez le courtier | `journalctl -b -u stockagent` |
| **Coupure de courant** | idem, plus : `synchronous=FULL` garantit que le dernier tick commité est sur le disque. Au démarrage l'agent attend que le NTP ait répondu avant de décider quoi que ce soit | `stock health` (ligne `clock`) |
| **Coupure internet courte** | la synchronisation abandonne après deux valeurs injoignables au lieu de quatre-vingt-huit, le tick se fait sur le cache — encore frais — et la vie continue. Les flux optionnels (résultats, titres de presse) ne sont même pas sollicités | `! link is down` dans le journal |
| **Coupure internet longue** | dès que la séance la plus récente a plus de `max_data_age_days` (4 jours par défaut), **les entrées sont suspendues**. Les stops et les ordres au repos, eux, restent surveillés. L'agent revient toutes les 5, 10, 20, 40 minutes puis toutes les heures au lieu d'attendre la cloche | `stock health` passe à `DEGRADED`, et `stock log` porte la ligne `entries suspended` |
| **Week-end, jour férié** | rien. Le seuil est en jours calendaires précisément parce que le marché est fermé les deux tiers du temps : lundi 16 h 05, la dernière clôture a trois jours et tout va bien | — |
| **Retour du réseau** | le tick suivant rattrape les séances manquées et le drapeau dégradé se lève tout seul | `stock status` |
| **L'agent plante** | `Restart=always`, `RestartSec=30`, et surtout `StartLimitIntervalSec=0` : sans cette dernière ligne systemd renonce après cinq échecs en dix secondes et laisse l'unité morte jusqu'à intervention humaine | `systemctl status` |
| **L'agent se bloque sans planter** | le cas que `Restart=always` ne voit pas. La boucle envoie un battement toutes les 30 s, y compris à chaque valeur pendant une synchronisation de 88 noms ; dix minutes de silence et systemd le tue et le relance | `Watchdog timeout` dans le journal |
| **Le noyau se fige** | le chien de garde matériel du BCM2711 redémarre la carte | la carte revient toute seule |
| **La base est corrompue** | `stock health` le dit au lieu d'attendre une exception trois semaines plus tard ; restauration ci-dessous | `database BAD` |
| **Un tick échoue** | l'exception est écrite dans le journal de décisions plutôt qu'avalée, et le réveil suivant est reporté explicitement — sans quoi une heure de réveil dans le passé fait tourner la boucle à vide, c'est-à-dire martèle le réseau | `stock log` |

Un chiffre pour rendre ça concret. Lien mort, 88 valeurs, deux hôtes : avant,
chaque valeur essayait trois fois chaque hôte avec backoff, soit 3 min 14 par
valeur et **près de cinq heures** pour ne rien ramener. Maintenant deux valeurs
suffisent à conclure, sans réessai sur un hôte injoignable : **deux minutes**,
mesurées.

Ce qui n'est **pas** couvert : une carte SD morte (sauvegardez ailleurs), un
changement d'API côté Yahoo, et le fait que ce soit du papier — il n'y a aucun
identifiant de courtier dans cette installation et aucun chemin de code capable
de passer un ordre réel.

---

## 5. Est-ce que ça gagne de l'argent ?

C'est la raison d'être de la carte, et la réponse se lit avec deux commandes.

```bash
stock track      # le compte de cet agent : six conditions, cochées ou non
sudo python3 /opt/stockagent/deploy/portfolio.py \
     /var/lib/stockagent/live.db /var/lib/cryptoagent/live.db
```

Le service dote le compte tout seul au premier tick, avec le capital de la
configuration, et écrit la date de départ dans la base. Pour choisir un autre
budget, faites-le **avant** de démarrer le service :

```bash
sudo systemctl stop stockagent
sudo -u stockagent /opt/stockagent/.venv/bin/python -m trader \
     --db /var/lib/stockagent/live.db fund 100000
sudo systemctl start stockagent
```

Après, `fund` refuse : redoter un compte qui a déjà tradé déplacerait la ligne
de départ de la mesure, et c'est la seule chose qu'un test vers l'avant ne
survit pas. `--restart` force le passage, et l'ancien run reste au registre.

**Le rythme de consultation compte.** Un `track` par semaine est déjà plus
souvent que nécessaire : la barre statistique se franchit en années, pas en
mois, et regarder tous les jours une courbe qui a besoin de quatre ans est le
meilleur moyen de la couper au premier mauvais trimestre. Le README principal
a le tableau des durées.

---

## 6. Les deux agents sur la même carte

`stockagent` et `cryptoagent` s'installent exactement pareil et ne se
connaissent pas : deux utilisateurs système, deux répertoires d'état, deux
unités, deux minuteurs.

```bash
git clone <url-stock>  stockagent  && sudo ./stockagent/deploy/install.sh
git clone <url-crypto> cryptoagent && sudo ./cryptoagent/deploy/install.sh
```

Trois remarques pour la cohabitation :

* **Les réveils ne se chevauchent presque jamais.** `stockagent` décide une
  fois par séance, peu après la cloche de New York (22 h 20 UTC en hiver,
  21 h 20 en été) ; `cryptoagent` décide 3 à 5 fois par jour sur les clôtures
  de bougies 4 h. Rien à régler.
* **Les minuteurs de sauvegarde, eux, se chevaucheraient** : les deux sont
  `OnCalendar=daily`. D'où le `RandomizedDelaySec=30m` dans chacun.
* **La mémoire est la seule ressource partagée qui compte**, et c'est
  `stockagent` qui dimensionne la carte : 88 valeurs contre 20 paires.
  Surveillez avec `systemd-cgtop` les premiers jours.

Une vue d'ensemble :

```bash
systemctl status stockagent cryptoagent
journalctl -u stockagent -u cryptoagent -f
systemd-analyze security stockagent.service
```

---

## 7. Sauvegardes et restauration

Un instantané par jour, sept conservés, dans `/var/lib/stockagent/backups/`.
Pris avec l'API de sauvegarde de SQLite et non avec `cp`, parce que l'agent
peut très bien être en train d'écrire : copier le fichier à la main pendant
qu'un WAL est ouvert produit quelque chose qui ressemble à une base et n'en est
pas une. Le minuteur est `Persistent=true`, donc une sauvegarde manquée parce
que la carte était éteinte est rattrapée au démarrage suivant.

Restaurer :

```bash
sudo systemctl stop stockagent
sudo -u stockagent cp /var/lib/stockagent/backups/live-20260918-031200.db \
                      /var/lib/stockagent/live.db
sudo systemctl start stockagent
```

Ces instantanés sont sur la même carte SD que l'original, ce qui les protège
d'une corruption logique et pas du tout d'une carte morte. Pour une vraie
sauvegarde, tirez-les ailleurs :

```bash
rsync -a pi@cm4:/var/lib/stockagent/backups/ ~/sauvegardes/stockagent/
```

---

## 8. Mettre à jour

```bash
cd ~/stockagent && git pull
sudo ./deploy/install.sh
```

Le script recopie le code, réinstalle les unités et redémarre le service. La
base n'est jamais touchée — pas même par `--uninstall`. Les migrations de
schéma se font toutes seules à l'ouverture (`Store._migrate`), donc un compte
papier survit à une montée de version au lieu de devoir être remis à zéro.

Le premier démarrage télécharge tout l'historique disponible pour les 88 noms,
ce qui prend un bon moment. Pour le faire à la main avant de lancer le
service :

```bash
sudo systemctl stop stockagent
sudo -u stockagent /opt/stockagent/.venv/bin/python -m trader \
  --db /var/lib/stockagent/live.db sync
sudo systemctl start stockagent
```

---

## 9. Quand ça ne démarre pas

```bash
systemctl status stockagent -l --no-pager
journalctl -u stockagent -b --no-pager | tail -50
systemd-analyze verify /etc/systemd/system/stockagent.service
```

| Symptôme | Cause habituelle |
|---|---|
| `status=203/EXEC` | le venv n'existe pas — relancer `install.sh` |
| `Failed to determine user credentials` | l'utilisateur système a été supprimé — relancer `install.sh` |
| bloqué sur `waiting for the clock to be set` | pas de NTP. `timedatectl` dira `System clock synchronized: no`. L'attente est bornée à 10 minutes, après quoi l'agent démarre quand même sans autoriser d'entrée |
| `entries suspended` en boucle | les données ne se rafraîchissent pas : `curl -sSI https://query1.finance.yahoo.com/v8/finance/chart/SPY` depuis la carte |
| `EDGAR refused the request (403)` | le calendrier de résultats est activé et `sec_user_agent` n'a pas d'adresse de contact. Voir `events.py` |
| la base grossit vite | c'est le cache de séances, et c'est normal : quelques centaines de Mo pour trente-six ans sur 88 noms |

Tout désinstaller, sans perdre le compte papier :

```bash
sudo ./deploy/install.sh --uninstall
```
