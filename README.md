# Maxxie 432Hz — bot Telegram

Bot léger en Python (bibliothèque standard uniquement), prévu pour Debian 13.

## Fonctionnement

- Tout message privé reçu, y compris `/start`, affiche l'accueil avec deux boutons : « Lire les règles du groupe » et « Rejoindre le groupe ». Le premier affiche le règlement en privé ; le second ouvre le lien du groupe.
- L'arrivée d'un membre dans le groupe configuré déclenche une tentative d'accueil en privé, avec les deux boutons. Telegram accepte l'envoi si le membre a déjà contacté le bot et ne l'a pas bloqué. Sinon, le bot reste silencieux. Aucun accueil n'est publié dans le groupe.
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
scp bot.py assistant_ai.py rules.txt .env.example UTILISATEUR_SSH@maxxie432.rondeau-cameira.fr:~/maxxie-bot/
scp deploy/maxxie-bot.service deploy/configure.py deploy/configure_openai.py UTILISATEUR_SSH@maxxie432.rondeau-cameira.fr:~/maxxie-bot/
```

Puis sur le VPS :

```bash
sudo apt-get update
sudo apt-get install -y python3 ca-certificates
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin maxxie-bot
sudo install -d -m 755 /opt/maxxie-bot
sudo install -m 644 ~/maxxie-bot/bot.py ~/maxxie-bot/assistant_ai.py ~/maxxie-bot/rules.txt /opt/maxxie-bot/
sudo install -m 644 ~/maxxie-bot/configure.py ~/maxxie-bot/configure_openai.py /opt/maxxie-bot/
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

Le bot ne conserve ni profils ni messages sur disque. Telegram conserve les mises à jour en attente : elles peuvent être traitées au démarrage. Une interruption après l'envoi d'un message et avant sa confirmation peut occasionner un doublon.

## Administrateurs reconnus automatiquement

Dans `/etc/maxxie-bot.env`, ajouter `ADMIN_USER_IDS=123456789,987654321` en remplaçant les exemples par les identifiants des comptes autorisés. Chaque personne peut obtenir son propre identifiant en envoyant `/id` en privé au bot. Seuls ces identifiants numériques sont utilisés ; un nom affiché, un @pseudo, un transfert ou un numéro de téléphone ne donne aucun droit.

Pour accorder également la suppression des messages et la gestion des membres (restriction, bannissement et débannissement), ajouter les identifiants concernés dans `MODERATOR_USER_IDS`. Cette liste doit être un sous-ensemble de `ADMIN_USER_IDS`. Exemple : `MODERATOR_USER_IDS=123456789`. Le bot doit lui-même disposer des droits correspondants. Les administrateurs existants de cette liste reçoivent les droits manquants sans perdre leurs autres permissions ; le propriétaire conserve naturellement tous ses droits.

Le bot doit être administrateur du **supergroupe** avec le droit « Ajouter des administrateurs » (`can_promote_members`). Telegram ne permet cette API que pour les supergroupes et les canaux. Le profil accordé par défaut est un administrateur aux droits limités (`can_manage_chat`), capable de publier dans ce groupe réservé aux administrateurs, sans suppression de messages, bannissement ou nomination d'autres administrateurs. Le profil de modération ajoute la suppression et la gestion des membres ; il n'accorde pas la nomination d'autres administrateurs.

Après modification de la liste, redémarrer le service. La promotion se déclenche quand le compte autorisé rejoint le groupe, lors d'un événement de statut ou lorsqu'il envoie `/start` en privé au bot s'il est déjà membre. Les messages ordinaires du groupe peuvent également déclencher la reconnaissance. Un compte absent du groupe n'est pas promu. Une liste vide désactive la fonction. Retirer un identifiant de la liste empêche les promotions futures mais ne retire pas un rôle déjà accordé : le propriétaire doit le retirer dans Telegram. Une rétrogradation manuelle d'un compte encore autorisé peut entraîner une nouvelle promotion ; retirer d'abord son identifiant de la liste.

## Publication des MP3

Un administrateur (ou le propriétaire) du groupe peut envoyer un MP3 en privé au bot : celui-ci copie le message dans le groupe, avec sa légende, puis confirme la publication en privé. Le statut d'administrateur est vérifié auprès de Telegram à chaque envoi ; la liste de promotion automatique ne suffit pas à autoriser une publication.

Les MP3 sont acceptés comme audio ou document, selon leur extension `.mp3` (ou leur type `audio/mpeg` si aucun nom n'est fourni). Le fichier n'est ni téléchargé sur le VPS ni converti. Un MP3 envoyé comme document reste un document. Les autres fichiers audio sont refusés. Le bot doit pouvoir publier dans le groupe.

## Assistant IA en privé

L'intégration utilise l'[API Responses OpenAI](https://developers.openai.com/api/docs/guides/text) et le modèle configurable `gpt-5.6-terra`. Elle est désactivée sans `OPENAI_API_KEY`. Pour l'activer sur une installation existante :

```bash
ssh debian@maxxie432.rondeau-cameira.fr
sudo python3 /opt/maxxie-bot/configure_openai.py
sudo systemctl restart maxxie-bot
```

L'assistant de configuration demande la clé en saisie masquée, fait une courte requête API facturée pour vérifier l'accès au modèle, puis l'enregistre dans `/etc/maxxie-bot.env` (root uniquement). En cas d'échec, la configuration reste inchangée. Ne jamais ajouter la clé dans le dépôt, le chat ou une commande conservée dans l'historique. Pour une installation neuve, transférer aussi `deploy/configure_openai.py` et l'installer dans `/opt/maxxie-bot/`.

Quand l'IA est active, les textes privés hors commandes reçoivent une réponse sur le groupe et ses règles. `/start`, `/id`, les boutons, les promotions et les MP3 gardent leur fonctionnement habituel. Aucun message du groupe, fichier ou identifiant Telegram n'est envoyé à OpenAI : seules la question privée, les règles et les informations publiques du groupe sont transmises. Le menu d'accueil signale l'usage de l'IA. Aucune action Telegram n'est confiée au modèle. Les instructions lui demandent de rester sur le sujet et de ne pas inventer de catalogue ; les réponses restent générées et peuvent être inexactes.

Chaque question est indépendante, sans historique partagé ou enregistré. `store=false` désactive la conservation de la réponse pour sa récupération par l'API ; cela ne constitue pas une garantie d'absence de toute rétention chez OpenAI. Consulter les [contrôles de données OpenAI](https://developers.openai.com/api/docs/guides/your-data).

Limites : 2000 caractères en entrée, 600 jetons de sortie maximum, une requête par utilisateur toutes les 10 secondes et `OPENAI_HOURLY_LIMIT=60` requêtes par heure au total (échecs inclus). Ces compteurs en mémoire repartent à zéro au redémarrage ; ce n'est pas un plafond de dépense en euros. Une requête attend au maximum 25 secondes et peut retarder temporairement les autres événements du bot. En cas de panne API, les règles et le lien restent accessibles.

## Vérification locale

```bash
python3 -m unittest discover -s tests -v
```

Après installation : contacter le bot en privé, vérifier les deux boutons ; faire rejoindre ce compte au groupe et vérifier la réception de l'accueil en privé, sans publication du bot dans le groupe. Un membre n'ayant jamais ouvert le bot ou l'ayant bloqué ne doit pas recevoir d'accueil. Vérifier aussi que le bot reste silencieux pendant une conversation ordinaire.
