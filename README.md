# Maxxie 432Hz — bot Telegram

Bot léger en Python (bibliothèque standard uniquement), prévu pour Debian 13.

## Fonctionnement

- La commande privée `/start` affiche l'accueil avec cinq boutons sur quatre lignes : « Lire les règles du groupe », « Rejoindre le groupe », puis « Artistes » et « Hasard » côte à côte, et enfin « Aide ». Les boutons Artistes et Hasard ouvrent les mêmes fonctions que `/artistes` et `/hasard` en privé. Les commandes inconnues et les messages ordinaires restent sans réponse. `/aide`, `/recherche`, `/artistes`, `/hasard`, `/id`, `/statut` et la publication des MP3 gardent leur fonctionnement dédié.
- L'arrivée d'un membre dans le groupe configuré déclenche une tentative d'accueil en privé, avec les cinq boutons. Telegram accepte l'envoi si le membre a déjà contacté le bot et ne l'a pas bloqué. Sinon, le bot reste silencieux. Aucun accueil n'est publié dans le groupe.
- Le règlement reste en privé. Les messages ordinaires du groupe sont ignorés.
- Le lien du groupe est accessible à toute personne contactant le bot. Il ne s'agit pas d'un contrôle d'admission ni d'une obligation d'accepter les règles.

Un bot ne peut pas initier une conversation privée avec un utilisateur qui ne l'a jamais démarré. Telegram contrôle cette permission lors de l'envoi ; il n'est pas nécessaire de conserver une liste locale des conversations. Le bot reçoit les messages de service d'arrivée même avec le mode confidentialité activé. Les messages de service natifs de Telegram indiquant une arrivée ne sont pas supprimés. Voir la [documentation Telegram](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

## Préparer Telegram

1. Ajouter le bot au groupe Maxxie 432Hz et l'autoriser à envoyer des messages. Les droits d'administrateur ne sont pas nécessaires pour ces fonctions.
2. Conserver le mode confidentialité activé dans BotFather.
3. Personnaliser `rules.txt` (3500 caractères maximum).
4. Vérifier que `https://t.me/Maxxie_432Hz` ouvre bien le groupe. Pour un groupe privé, utiliser son lien d'invitation dans `GROUP_URL`.
5. Récupérer l'identifiant numérique du groupe. Pour ce groupe public, depuis le dossier du projet, exécuter :

```bash
python3 -c 'from getpass import getpass; from bot import Telegram; print(Telegram(getpass("Jeton du bot : ")).call("getChat", chat_id="@Maxxie_432Hz")["id"])'
```

Le jeton est saisi sans affichage et ne figure pas dans l'historique de la commande. Le bot doit déjà avoir accès au groupe. Ne pas publier le jeton ou le fichier de configuration.

## Installer sur le VPS Debian 13

### Installation préparée sur maxxie432.rondeau-cameira.fr

Le compte SSH est `debian`, le bot attendu est `@Maxxie_432Hz_Bot`.
Si les fichiers et le service sont déjà installés, terminer directement sur le VPS :

```bash
ssh debian@maxxie432.rondeau-cameira.fr
sudo python3 /opt/maxxie-bot/configure.py
sudo nano /opt/maxxie-bot/rules.txt
sudo systemctl enable --now maxxie-bot
sudo systemctl status maxxie-bot
```

L'assistant demande le jeton sans l'afficher, vérifie l'identité du bot et son accès au groupe public, puis récupère son identifiant numérique. Il crée `/etc/maxxie-bot.env` avec des permissions réservées à root et refuse d'écraser une configuration existante. Ajouter le bot au groupe avant de lancer l'assistant. Le service reste arrêté tant que cette configuration n'est pas terminée.

### Installation depuis zéro

Les commandes `sudo` suivantes sont à exécuter sur le VPS. Copier d'abord le projet depuis votre ordinateur (adapter `UTILISATEUR_SSH`) :

```bash
ssh UTILISATEUR_SSH@maxxie432.rondeau-cameira.fr 'mkdir -p ~/maxxie-bot'
scp bot.py reliability.py catalogue.py backups.py welcome.txt rules.txt .env.example UTILISATEUR_SSH@maxxie432.rondeau-cameira.fr:~/maxxie-bot/
scp deploy/maxxie-bot.service deploy/maxxie-backup.service deploy/maxxie-backup.timer deploy/configure.py UTILISATEUR_SSH@maxxie432.rondeau-cameira.fr:~/maxxie-bot/
```

Puis sur le VPS :

```bash
sudo apt-get update
sudo apt-get install -y python3 ca-certificates
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin maxxie-bot
sudo install -d -m 755 /opt/maxxie-bot
sudo install -m 644 ~/maxxie-bot/bot.py ~/maxxie-bot/reliability.py ~/maxxie-bot/catalogue.py ~/maxxie-bot/backups.py ~/maxxie-bot/welcome.txt ~/maxxie-bot/rules.txt /opt/maxxie-bot/
sudo install -m 644 ~/maxxie-bot/configure.py /opt/maxxie-bot/
sudo install -m 600 ~/maxxie-bot/.env.example /etc/maxxie-bot.env
sudo nano /etc/maxxie-bot.env
```

Renseigner `BOT_TOKEN`, `GROUP_ID` et vérifier `GROUP_URL`. Ne pas laisser le jeton d'exemple. Puis :

```bash
sudo install -m 644 ~/maxxie-bot/maxxie-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maxxie-bot
sudo systemctl status maxxie-bot
sudo journalctl -u maxxie-bot -n 50 --no-pager
```

Si l'utilisateur système existe déjà, ne pas répéter `useradd`. Pour une mise à jour, ne pas recopier `.env.example` sur la configuration contenant votre jeton. Après modification du code, des règles ou de la configuration : `sudo systemctl restart maxxie-bot`.

Le service utilise le long polling : seules des connexions HTTPS sortantes vers Telegram sont nécessaires. Aucun serveur web, certificat TLS ou port entrant supplémentaire n'est requis. Ne lancer qu'une instance avec ce jeton ; un webhook déjà configuré doit être retiré avant utilisation. Le bot refuse de supprimer automatiquement un webhook existant.

Le bot conserve les identifiants des visiteurs déjà signalés et une file de tâches SQLite protégée. Les événements en attente sont enregistrés avant leur confirmation auprès de Telegram. Leurs données sont effacées une fois le traitement terminé ; les métadonnées de diagnostic sont conservées 30 jours.

## Administrateurs reconnus automatiquement

Dans `/etc/maxxie-bot.env`, ajouter `ADMIN_USER_IDS=123456789,987654321` en remplaçant les exemples par les identifiants des comptes autorisés. Chaque personne peut obtenir son propre identifiant en envoyant `/id` en privé au bot. Seuls ces identifiants numériques sont utilisés ; un nom affiché, un @pseudo, un transfert ou un numéro de téléphone ne donne aucun droit.

Pour accorder également la suppression des messages et la gestion des membres (restriction, bannissement et débannissement), ajouter les identifiants concernés dans `MODERATOR_USER_IDS`. Cette liste doit être un sous-ensemble de `ADMIN_USER_IDS`. Exemple : `MODERATOR_USER_IDS=123456789`. Le bot doit lui-même disposer des droits correspondants. Les administrateurs existants de cette liste reçoivent les droits manquants sans perdre leurs autres permissions ; le propriétaire conserve naturellement tous ses droits.

Le bot doit être administrateur du **supergroupe** avec le droit « Ajouter des administrateurs » (`can_promote_members`). Telegram ne permet cette API que pour les supergroupes et les canaux. Le profil accordé par défaut est un administrateur aux droits limités (`can_manage_chat`), capable de publier dans ce groupe réservé aux administrateurs, sans suppression de messages, bannissement ou nomination d'autres administrateurs. Le profil de modération ajoute la suppression et la gestion des membres ; il n'accorde pas la nomination d'autres administrateurs.

Après modification de la liste, redémarrer le service. La promotion se déclenche quand le compte autorisé rejoint le groupe, lors d'un événement de statut ou lorsqu'il envoie `/start` en privé au bot s'il est déjà membre. Un compte absent du groupe n'est pas promu. Une liste vide désactive la fonction. Retirer un identifiant de la liste empêche les promotions futures mais ne retire pas un rôle déjà accordé : le propriétaire doit le retirer dans Telegram. Une rétrogradation manuelle d'un compte encore autorisé peut entraîner une nouvelle promotion ; retirer d'abord son identifiant de la liste.

## Publication des MP3

Un administrateur (ou le propriétaire) du groupe peut envoyer un MP3 en privé au bot : celui-ci copie le message dans le groupe, avec sa légende, puis confirme la publication en privé. Le statut d'administrateur est vérifié auprès de Telegram à chaque envoi ; la liste de promotion automatique ne suffit pas à autoriser une publication.

Les MP3 sont acceptés comme audio ou document, selon leur extension `.mp3` (ou leur type `audio/mpeg` si aucun nom n'est fourni). Le fichier n'est ni téléchargé sur le VPS ni converti. Un MP3 envoyé comme document reste un document. Les autres fichiers audio sont refusés. Le bot doit pouvoir publier dans le groupe.

## Recherche musicale et aide

En privé, `/aide` présente les commandes disponibles : `/start`, `/recherche artiste ou titre`, `/artistes`, `/hasard`, `/aide` et `/id`. L’aide des administrateurs réels du groupe précise comment publier et indexer ; celle du propriétaire affiche également `/statut`. Les commandes peuvent être adressées au bot (`/aide@Maxxie_432Hz_Bot`). Les commandes inconnues, notamment `/status`, restent silencieuses.

Exemples : `/recherche Edith Piaf`, `/recherche vie en rose`. La recherche ignore accents et majuscules et exige la présence de tous les mots, dans n’importe quel ordre, dans le titre, l’artiste, le nom du fichier ou la légende. Elle affiche dix résultats par page avec des boutons vers les messages du groupe et « Suivant / Précédent » pour parcourir toutes les correspondances. Les réponses restent en privé.

`/artistes` affiche les artistes par ordre alphabétique, avec le nombre de morceaux et dix artistes par page. Choisir un artiste ouvre ses morceaux, également paginés. Les variantes d’accents et de majuscules sont regroupées ; un artiste absent des métadonnées apparaît dans « Artiste non renseigné ». Les anciennes entrées au format « artiste — titre » sont migrées automatiquement ; les nouveaux MP3 et les imports conservent l’artiste explicitement.

`/hasard` propose un morceau du groupe avec un lien pour l’écouter et un bouton « Encore au hasard ». Le fichier n’est pas renvoyé et aucun message n’est publié dans le groupe. Le même morceau peut être tiré plusieurs fois.

Les boutons de navigation utilisent une session privée valable une heure, conservée dans `STATE_DB` et liée à son destinataire pour survivre à un redémarrage. Cette session contient la recherche ou l’artiste sélectionné ; les sessions expirées sont supprimées au nettoyage horaire (donc au plus tard une heure après expiration en fonctionnement normal). Les sauvegardes privées de la base peuvent en contenir une copie. Après expiration, relancer `/recherche` ou `/artistes`. Si le catalogue change pendant la navigation, les pages reflètent son état courant.

Les MP3 publiés par l’intermédiaire du bot et ceux reçus directement dans le groupe configuré sont automatiquement indexés. Les modifications reçues des messages du groupe mettent à jour leur entrée. Le catalogue ne contient ni fichiers audio, ni profils de visiteurs : il est conservé dans la table `music` de `STATE_DB`, couverte par les sauvegardes existantes et conservée indépendamment du nettoyage des tâches.

**Anciens morceaux :** le Bot API ne permet pas de parcourir tout l’historique. Un administrateur peut répondre à un ancien message MP3, **dans le groupe**, avec `/indexer`. Le bot vérifie les droits actuels et confirme uniquement en privé si l’administrateur a démarré le bot. Répéter l’indexation met à jour la même entrée sans doublon et ne republie pas le fichier. Sans catalogue ou sans correspondance, le bot l’indique explicitement.

Le bot doit être administrateur pour recevoir les publications ordinaires du groupe avec le mode confidentialité activé. Les publications d’autres bots peuvent ne pas lui être transmises. Les suppressions ordinaires de messages ne sont pas notifiées au bot : un résultat peut donc pointer vers un message supprimé. Pour les groupes privés, les liens nécessitent d’être membre. Voir la [FAQ Telegram](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get) et les [liens vers les messages](https://core.telegram.org/api/links#message-links).

### Import d’un export Telegram Desktop

`import_catalogue.py` permet d’ajouter en une transaction les MP3 d’un export JSON de supergroupe. Il vérifie la correspondance entre l’identifiant du groupe exporté et `GROUP_ID`, ignore les messages non musicaux et conserve les métadonnées des entrées déjà indexées. Il ne republie aucun message, n’ouvre pas la file de tâches du bot et ne télécharge pas les fichiers audio.

Après une sauvegarde, exécuter avec le compte du service et un export placé dans un dossier privé :

```bash
sudo -u maxxie-bot python3 /opt/maxxie-bot/import_catalogue.py /CHEMIN/export.json --database /var/lib/maxxie-bot/tasks.sqlite3 --group-id=-100IDENTIFIANT
```

Remplacer le chemin et l’identifiant par les valeurs réelles. Le bilan indique le nombre de MP3, les ajouts, les entrées déjà présentes et le total. Une erreur annule les ajouts de la transaction. Supprimer la copie temporaire de l’export après vérification ; ne jamais versionner l’historique privé dans Git.

## Notifications de nouveaux visiteurs

Configurer `NOTIFY_CHAT_ID` avec l'identifiant numérique du destinataire privé, puis redémarrer le service. Le destinataire doit avoir démarré le bot et ne pas l'avoir bloqué. À son premier `/start` privé (y compris un lien `/start rules`), chaque utilisateur déclenche une notification contenant son nom affiché, son @pseudo éventuel et son identifiant Telegram. Les messages ordinaires et les commandes du groupe ne déclenchent aucune notification.

Les identifiants signalés sont conservés dans `VISITORS_DB` (par défaut `/var/lib/maxxie-bot/visitors.sqlite3`). Le service systemd crée ce dossier privé avec `StateDirectory=maxxie-bot`. Seuls les identifiants sont stockés, pas les messages ni les noms. Le suivi commence à l'activation : un ancien utilisateur qui refait `/start` sera donc signalé une fois. Une notification refusée temporairement est retentée automatiquement ; un refus permanent (bot bloqué, par exemple) ne bloque pas l'accueil et pourra être retenté à un prochain `/start`. Une interruption entre l'envoi et l'enregistrement peut exceptionnellement entraîner un doublon.

## Réactivité et réseau

Le client réutilise les connexions HTTPS, avec une connexion distincte par thread. Les connexions réseau attendent au maximum 3 secondes par adresse ; la lecture des actions Telegram attend 8 secondes, celle du long polling 35 secondes pour une attente serveur de 30 secondes. Les appels de lecture peuvent être retentés une fois après une rupture de connexion ; les envois et copies ne sont pas automatiquement renvoyés par le client pour éviter une publication en double. Une interruption réseau au moment d'un envoi peut toujours laisser son résultat incertain.

`TELEGRAM_IPV4_ONLY=1` est le réglage par défaut pour éviter la route IPv6 défaillante observée sur ce VPS (un simple appel avait pris 46 secondes). La résolution DNS et la validation TLS du nom `api.telegram.org` sont conservées, sans IP codée en dur. Sur un serveur avec IPv6 fonctionnel, `TELEGRAM_IPV4_ONLY=0` permet les deux familles.

Les événements et actions en attente sont enregistrés dans `STATE_DB` (par défaut `/var/lib/maxxie-bot/tasks.sqlite3`). Deux threads traitent séparément les réponses aux utilisateurs et les notifications/promotions. La réception Telegram est indépendante des envois ; à partir de 1000 tâches en attente, elle ralentit pour laisser les traitements rattraper leur retard, sans supprimer les tâches déjà reçues.

Une erreur 429 reprogramme la tâche après le délai `retry_after`. Les étapes déjà confirmées ne sont pas rejouées : si la copie du MP3 a réussi mais sa confirmation privée est refusée temporairement, seule cette dernière sera renvoyée. Les erreurs temporaires de lecture sont également retentées, jusqu'à 8 tentatives par tâche. Les refus permanents sont journalisés sans boucle infinie.

Chaque action possède un identifiant d'étape stable. Sa requête est enregistrée avant son envoi, puis son résultat confirmé : une reprise conserve le contenu prévu initialement, même si welcome.txt ou les règles ont été modifiés entre-temps. Les requêtes enregistrées sont effacées à la fin de la tâche. Les droits d’une promotion refusée temporairement sont recalculés à chaque tentative ; un changement de destinataire ou d’identité annule la reprise, pour ne pas réutiliser une ancienne autorisation. Si le service s'arrête pendant un envoi ou si la connexion coupe sans confirmation, l'état devient **incertain** : aucun renvoi automatique de cette action, car Telegram a peut-être accepté le fichier. Cette précaution réduit les doublons mais ne garantit pas une livraison exactement une fois. Vérifier le groupe avant de renvoyer un MP3 signalé comme incertain. Les appels de lecture restent retentables.

Les connexions HTTPS sont réutilisées. Les appels d'action dépassant une seconde sont journalisés avec leur méthode et leur durée, sans jeton ni contenu des messages. L'acquittement d'un bouton est facultatif : son échec réseau ou son expiration n'empêche pas l'affichage des règles. Le long polling renvoie immédiatement les nouveaux événements : ses 30 secondes d'attente ne sont pas un délai imposé aux réponses.

Au démarrage, une panne réseau ou Telegram entraîne une nouvelle tentative avec attente progressive (jusqu'à 60 secondes), au lieu de faire quitter le service. Un jeton refusé ou un webhook existant restent des erreurs explicites. SIGTERM/SIGINT arrêtent la réception et laissent terminer les tâches en cours ; les tâches restantes reprennent au prochain démarrage.

## Stockage, accueil et diagnostic

`WELCOME_FILE` pointe vers `welcome.txt`, qui contient le texte du menu d'accueil. Modifier ce fichier puis redémarrer le service ; les cinq boutons sont conservés. Ne pas écraser un fichier personnalisé lors d'une mise à jour.

Les événements en attente peuvent contenir le texte privé, le nom affiché, le pseudonyme et les références des fichiers Telegram. Les MP3 eux-mêmes ne sont jamais téléchargés. Le dossier `/var/lib/maxxie-bot` est réservé au compte du service ; les fichiers SQLite ne sont pas versionnés dans Git. Les données des tâches terminées ou abandonnées sont effacées, puis leurs métadonnées et empreintes d'actions sont nettoyées après 30 jours. Une tâche en attente peut conserver ses données pendant une longue indisponibilité. Sauvegarder les bases avec l'API de sauvegarde SQLite ou après arrêt du service, en tenant compte des fichiers WAL ; ne pas copier seulement le fichier principal pendant son utilisation.

Pour surveiller les erreurs et les envois incertains :

```bash
sudo journalctl -u maxxie-bot -n 100 --no-pager
```

Un verrou de fichier exclusif refuse une seconde instance avant toute récupération des tâches. Une seule instance du bot doit utiliser les bases et le jeton. Une fois cette version déployée, conserver les bases lors des mises à jour pour préserver le suivi et la prévention des doublons.

## Commande privée /statut

`OWNER_USER_ID` désigne le seul compte autorisé à consulter `/statut` (à défaut, l'identifiant privé `NOTIFY_CHAT_ID` est utilisé). Les administrateurs du groupe n'obtiennent pas ce droit automatiquement. La commande fonctionne uniquement en privé et indique le nombre de tâches en attente, en cours, terminées, échouées ou incertaines, les identifiants des cinq dernières tâches à vérifier et la date de la dernière sauvegarde contrôlée. Une sauvegarde de plus de 36 heures, ou dont le dossier a disparu, déclenche un avertissement dans la réponse. Aucun contenu des messages, jeton ou chemin privé n'est transmis.

## Sauvegardes quotidiennes et restauration

Le timer `maxxie-backup.timer` lance une sauvegarde quotidienne vers **03 h 30, heure de Paris**, avec un décalage aléatoire de dix minutes au maximum. `Persistent=true` rattrape un déclenchement manqué après un arrêt du VPS. Les deux bases sont copiées avec l'API SQLite, y compris leurs données WAL, puis contrôlées avec `quick_check` et SHA-256. Chaque sauvegarde est réellement restaurée dans un dossier temporaire avant validation.

Les sauvegardes restent **sur le VPS**, dans `BACKUP_DIR` (par défaut `/var/lib/maxxie-bot/backups`), accessible uniquement au compte du service. `BACKUP_KEEP=14` conserve les quatorze dernières sauvegardes réussies. La nouvelle copie est explicitement protégée de la rotation, même si plusieurs sauvegardes sont créées dans la même seconde. Ces copies locales ne couvrent pas la perte totale du VPS. Les deux bases sont sauvegardées successivement, pas dans une transaction commune.

Installation du timer après installation des scripts :

```bash
sudo install -m 644 ~/maxxie-bot/maxxie-backup.service ~/maxxie-bot/maxxie-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maxxie-backup.timer
sudo systemctl start maxxie-backup.service
sudo systemctl list-timers maxxie-backup.timer
sudo journalctl -u maxxie-backup.service -n 20 --no-pager
```

Pour tester manuellement une restauration, choisir un dossier de sauvegarde `backup-…` et une **destination nouvelle** :

```bash
sudo -u maxxie-bot python3 /opt/maxxie-bot/backups.py --restore /var/lib/maxxie-bot/backups/backup-CHOISIE --restore-to /var/lib/maxxie-bot/restauration-test
```

La commande vérifie les empreintes, refuse tout écrasement et ne touche pas aux bases actives. La restauration et la neutralisation des anciennes tâches se font dans un dossier temporaire ; le dossier final ne reçoit les bases qu’après validation complète. Un échec de copie ou de validation ne laisse pas de restauration partielle à la destination. Les tâches en attente d'une ancienne sauvegarde sont neutralisées en état `uncertain` : elles ont peut-être été exécutées depuis la sauvegarde. Les réactiver aveuglément pourrait republier des MP3.

Pour rétablir réellement ces bases : arrêter le timer, le service de sauvegarde et le bot ; conserver à part les bases actuelles et leurs éventuels fichiers `-wal`/`-shm` ; installer les deux fichiers restaurés à leurs chemins configurés, avec propriétaire `maxxie-bot` et permissions `600` ; redémarrer le bot et le timer. Ne jamais mélanger une base restaurée avec un ancien WAL. Vérifier `/statut`, les journaux et le groupe avant de traiter manuellement les tâches incertaines. La restauration revient à la date de la sauvegarde et peut aussi faire oublier des visiteurs signalés depuis.

## Protection contre les commandes répétées

La réception conserve au maximum cinq messages textuels ou clics de bouton privés par utilisateur sur une fenêtre commune de dix secondes. Les suivants sont ignorés sans réponse supplémentaire, pour ne pas amplifier un spam. Le compteur survit aux redémarrages et les reprises d'une même mise à jour ne consomment pas de nouveau quota. Les fichiers audio et documents ont un quota séparé de vingt en dix secondes. Le propriétaire configuré dans `OWNER_USER_ID` est exempt de ces limites ; les comptes dans `ADMIN_USER_IDS` sont également exempts du quota de fichiers. Les autres administrateurs peuvent publier avec le quota de fichiers normal. Les anciennes entrées de compteur sont nettoyées après une journée.

## Copie chiffrée sur l'ordinateur via SSH

Le certificat public `BACKUP_CERT=/opt/maxxie-bot/backup-public.pem` permet au VPS de produire `offsite-latest.enc` après chaque sauvegarde. Le chiffrement utilise OpenSSL CMS, AES-256-GCM et RSA-OAEP. **La clé privée reste sur l'ordinateur** ; elle n'est ni envoyée au VPS ni publiée dans Git.

Sur cet ordinateur, la clé est dans `backups/keys/backup-private.pem`, le certificat dans `backups/keys/backup-public.pem` et les copies dans `backups/offsite/`, sous le dossier du projet. Le dossier `backups/` est privé et ignoré par Git. Conserver séparément une copie sûre de la clé privée : sans elle les archives sont indéchiffrables.

`offsite_pull.py` récupère uniquement l'archive chiffrée via SSH, la déchiffre dans un dossier temporaire privé et vérifie sa restauration et ses empreintes avant de la conserver. Les données en clair du test sont supprimées ensuite. Les quatorze dernières archives distinctes sont conservées ; aucune nouvelle copie identique n'est ajoutée.

```bash
python3 offsite_pull.py --key backups/keys/backup-private.pem --destination backups/offsite
```

Le timer utilisateur `maxxie-offsite.timer` sur l'ordinateur vérifie les nouvelles copies chaque heure, avec un décalage maximal de cinq minutes. Il rattrape une échéance manquée lorsque la session et l'ordinateur redeviennent disponibles. La connexion SSH par clé doit fonctionner sans interaction ; si la clé SSH nécessite un agent, celui-ci doit être accessible au gestionnaire de services utilisateur.

```bash
systemctl --user status maxxie-offsite.timer
journalctl --user -u maxxie-offsite.service -n 20 --no-pager
```

Pour une autre installation, remplacer `@PROJECT_DIR@` dans `deploy/maxxie-offsite.service.in` par le chemin absolu du projet et installer le service et `deploy/maxxie-offsite.timer` dans `~/.config/systemd/user/`. Le timer du VPS et celui de l'ordinateur sont indépendants : si l'ordinateur reste éteint, aucune nouvelle copie hors VPS n'est faite.

Pour utiliser une archive locale en restauration, la déchiffrer d'abord avec `openssl cms -decrypt -binary -inform DER -in COPIE.enc -inkey backups/keys/backup-private.pem -out snapshot.tar.gz`, puis appliquer la procédure de restauration ci-dessus sur les trois fichiers vérifiés de l'archive. Le script effectue déjà ce test automatiquement pour chaque récupération.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions exécute automatiquement ces tests sur Python 3.11 et 3.13 à chaque push et pull request. Les tests simulent les erreurs 429, coupures réseau, interruptions pendant les envois, redémarrages et une file de plus de 128 tâches, sans contacter Telegram.

Après installation : contacter le bot en privé, vérifier les cinq boutons ; faire rejoindre ce compte au groupe et vérifier la réception de l'accueil en privé, sans publication du bot dans le groupe. Un membre n'ayant jamais ouvert le bot ou l'ayant bloqué ne doit pas recevoir d'accueil. Vérifier aussi que le bot reste silencieux pendant une conversation ordinaire.

Les sauvegardes du VPS sont verrouillées pendant la création, la rotation et le chiffrement pour éviter les exécutions simultanées. La copie sur l’ordinateur refuse une archive de plus de 36 heures (ou datée de plus de cinq minutes dans le futur) ; le service signale alors un échec sans remplacer les copies locales existantes.
