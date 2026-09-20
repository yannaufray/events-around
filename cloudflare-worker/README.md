# Sync favoris → Google Calendar (partagée entre deux personnes)

Petit worker Cloudflare qui garde une liste de favoris partagée et la republie
comme flux `.ics`, à abonner une seule fois dans Google Calendar. Google relit
l'URL toutes les ~12-24h, donc les nouveaux favoris apparaissent
automatiquement, sans rien réimporter.

Par défaut, `sortie/index.html` reste **utilisable par tout le monde avec des
favoris purement locaux** (comme avant, rien n'est envoyé nulle part). La
synchro partagée est **opt-in** : un bouton "Activer la synchro partagée" sur
la page demande un code — ce code (`SYNC_SECRET`) protège l'accès en lecture
et en écriture au worker, et **n'est jamais écrit dans le repo ni dans
`config.json` ni dans le HTML/JS généré**. Il n'existe que :
- chez Cloudflare (`wrangler secret put`),
- dans le `localStorage` du navigateur de chaque personne qui l'a saisi,
- dans les paramètres Google Calendar de chaque personne (l'URL `.ics` qu'elle
  colle en contient une copie).

## Mise en place (une fois)

1. Compte Cloudflare gratuit, puis `npm install -g wrangler` et `wrangler login`.
2. Depuis ce dossier :
   ```bash
   wrangler kv namespace create FAVORIS_KV
   ```
   Copier l'`id` retourné dans `wrangler.toml` (`kv_namespaces[0].id`).
3. Mettre à jour `AGENDA_ICS_URL` dans `wrangler.toml` avec l'URL GitHub Pages
   réelle de `agenda.ics` (le workflow publie le contenu de `sortie/` comme
   racine du site, donc sans `/sortie/` dans l'URL).
4. Choisir un code et le définir côté worker :
   ```bash
   openssl rand -hex 16      # génère une valeur aléatoire
   wrangler secret put SYNC_SECRET
   ```
5. Déployer :
   ```bash
   wrangler deploy
   ```
   Note l'URL affichée (`https://agenda-favoris.<sous-domaine>.workers.dev`).
6. Dans `config.json` à la racine du repo, seule l'URL du worker est renseignée
   (**jamais le secret**) :
   ```json
   "sync_favoris": {
     "worker_url": "https://agenda-favoris.<sous-domaine>.workers.dev"
   }
   ```
   Régénérer la page (`python3 agenda_local.py`).
7. Donne le code choisi à l'étape 4 **de vive voix ou en message privé** à
   l'unique autre personne concernée.
8. Chacun, sur son propre appareil : ouvrir `sortie/index.html` (le site
   publié), cliquer **"Activer la synchro partagée"**, saisir le code. La page
   affiche alors le lien `.ics` (avec le code dedans) à coller dans Google
   Calendar : « Autres agendas » → « + » → « À partir de l'URL ».

## Fonctionnement

- Sans code saisi : comportement d'origine, favoris purement locaux, aucun
  appel réseau.
- Après activation : chaque clic ☆/★ envoie un ajout/retrait individuel au
  worker (`POST /favoris`, jamais un remplacement de toute la liste — deux
  personnes/appareils peuvent contribuer sans s'écraser). Au chargement, la
  page va aussi chercher l'état partagé (`GET /favoris?key=…`) pour rester à
  jour avec l'autre personne.
- `GET /favoris.ics?key=…` relit `agenda.ics` publié, ne garde que les
  `VEVENT` dont l'UID est dans la liste partagée, et renvoie ce sous-ensemble
  comme calendrier.
- Toutes les routes de lecture/écriture exigent le bon code (`key` en query
  string pour les `GET`, `secret` dans le corps JSON pour le `POST`) ; sans lui
  ou avec un mauvais code, le worker répond `403`.
