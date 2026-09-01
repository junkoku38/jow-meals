# API Jow (non officielle) — connaissances accumulées

> **Avertissement** : l'API de jow.fr n'est **pas publique ni documentée**.
> Tout ce qui suit a été observé à l'occasion du développement de cette
> intégration (analyse du site, appels réels, réponses constatées). Elle
> peut changer sans préavis — chaque endpoint ci-dessous a été vérifié en
> septembre 2026. Utilisation à vos risques et périls, dans le respect des
> conditions de jow.fr.

Base : `https://api.jow.fr/public`
Statique (images) : `https://static.jow.fr/` (préfixer les `imageUrl`
relatifs, ex. `recipes/xxx.jpg.webp`)

## Sommaire

1. [Authentification](#authentification)
2. [Recettes](#recettes)
3. [Compte utilisateur](#compte-utilisateur)
4. [Menu / liste de courses](#menu--liste-de-courses)
5. [Analytics (gol)](#analytics-gol--à-éviter)
6. [En-têtes](#en-têtes)
7. [Pièges connus](#pièges-connus)

---

## Authentification

### `POST /auth/refresh?availabilityZoneId=FR`

Renouvelle l'access token à partir du refresh token. **C'est la seule
route d'auth à utiliser pour une intégration** : l'access token vit
~48 h, le refresh token ~6 mois.

- Corps JSON : `{"refreshToken": "<JWT>"}` — **sans** en-tête
  `authorization` (sinon 401)
- Réponse 200 : `{"data": {"accessToken": "…", "refreshToken": "…", "userId": "…", …}}`
- Les tokens sont des JWT ; le payload du refresh contient
  `"type": "refresh"`, celui de l'access `"type": "access"` (utile pour
  valider qu'on a collé le bon)
- `GET /profile` renvoie **401** dès que l'access token est expiré →
  refresh puis retry (voir `_async_jow_get` du manager)

> Récupérer un refresh token : compte jow.fr → localStorage (F12 →
> Application → `jow_store` → champ `data.auth.refreshToken`).

## Recettes

### `POST /recipe/quicksearch?start=0&availabilityZoneId=FR&query=…&limit=N`

Recherche publique (sans token). **Plafonnée à 50 résultats par page**,
paginée via `start` ; `totalCount` exposé dans la réponse.

- Corps : `{}` (les paramètres sont dans l'URL)
- Réponse : `{"data": {"content": [recette…], "totalCount": n}}`
- **Comportement clé** : recherche en OU logique sur les tokens —
  « burger asiatique » et « burger coréen » renvoient les mêmes
  résultats, dominés par le mot le plus fréquent seul. Un re-ranking
  lexical côté client (mots-clés du titre) est indispensable.
- Champs utiles par recette : `id`/`_id`, `title`, `description`,
  `imageUrl`, `preparationTime`, `cookingTime`, `constituents[]` (voir
  ci-dessous), `roundedCoversCount`
- ⚠️ `quantityPerCover` vit dans `constituents[].ingredient.quantityPerCover`

### `GET /recipe/{recipeId}`

Détail public d'une recette (avec `x-jow-withmeta: true`).

- Réponse : la recette **à la racine** (pas de wrapper `data`), avec
  `constituents[]` complets
- ⚠️ `quantityPerCover` vit ici **sur le constituant**
  (`constituents[].quantityPerCover`) et **pas** dans l'ingrédient —
  structure différente de la recherche pour la même donnée !
- Les calories ne sont **pas** dans la recherche ni le détail : il
  faut les déduire de la composition (l'intégration les stocke après
  calcul/fetch)

## Compte utilisateur

### `GET /profile`

Profil de l'utilisateur connecté (access token requis).

- Réponse : `{"data": {…}}` — peut être un dict **vide** pour un
  compte valide : un profil vide n'est PAS un échec d'auth (c'est le
  401 qui l'est)

### `GET /recipes/favorites?availabilityZoneId=FR&limit=N`

Favoris du compte (token requis).

- Réponse : `{"data": {"recipes": […]}}`
- ⚠️ Renvoie `[]` aussi bien pour « pas de favoris » que pour « token
  refusé » — distinguer via un appel profil de contrôle

### `GET /profile/letscook?availabilityZoneId=FR&nbMeals=N` ⭐ route clé

La **vraie source du menu** (route utilisée par le site). Stable, tout
contrairement à `/menu`. Contient :

- `data.openShoppingList` — la **liste ouverte = le menu courant** :
  `meals: [{recipe, coversCount, source, isCooked}]`, `state`,
  dates de création/MAJ. C'est ce que le site affiche comme « menu de
  la semaine » et ce que lit `jow.import_menu`.
- `data.pendingMenu` — menu en attente (rarement présent)
- `data.lastOrdersDetails`, `data.recipesToCook` — historique
- `data.favorites`, `data.shoppingLists`, `data.uploaded`, …
- `nbMeals` ne semble pas borner `openShoppingList.meals` (borne
  plutôt les listes historiques)

### `GET /profile/letscook/menus?count=N`

Menus historiques (commandes passées). Réponse : `{"data": [{id,
meals, referenceDate, type: "order"}…]}` — ce ne sont **pas** les
menus en cours.

## Menu / liste de courses

### `POST /shoppinglist/open?populateRecipes=true&populateIngredients=true&availabilityZoneId=FR` ⭐ route clé

**Crée/réécrit la liste ouverte = écrit dans le menu.** C'est
exactement le mécanisme du site (bouton « Générer ma liste »).

- Corps : `{"meals": [{"recipe": "<recipeId>", "coversCount": 2,
  "source": "jow"}]}`
- ⚠️ **Remplace tout le contenu** de la liste ouverte. Pour ne pas
  écraser les ajouts manuels : lire `profile/letscook` d'abord, puis
  re-POSTer l'union (algorithme de `jow.send_menu`)
- Réponse 200 : la liste réécrite (`meals` avec recettes peuplées)
- `PUT /shoppinglist/open/validate` — valide/soumet la liste
- `DELETE /shoppinglist/open/manual-item/{manualItemId}` — retire un
  article manuel

### `GET /shoppinglist?availabilityZoneId=FR`

Liste de courses du compte. `204` = liste vide (compte sain), `200` =
contenu dans `data`. Instable pour certains comptes en même temps que
`/menu`.

### `GET /menu/week` et `GET /menu` ❌ instables

Endpoints « officiels » du menu… **observés en erreur 500** (`{"error":
{"code": "ERROR"}}`) sur des comptes dont l'état serveur est corrompu
(y compris après reconstruction via le site). L'intégration les tente
en premier et **replie sur `profile/letscook`**, qui porte les mêmes
données. Ne pas construire sur ces routes.

## Analytics (gol) — à ÉVITER

### `POST /gol?type=recipeChosen&availabilityZoneId=FR`

Tracker d'événements d'analytique. Accepte à peu près tout (`204`)
sans rien écrire dans le menu — les recettes envoyées ici
**n'apparaissent pas** sur jow.fr. Une intégration a vécu des semaines
en croyant synchroniser le menu via cette route alors qu'elle
n'émettait que de la télémétrie. Autres types observés acceptés :
`recipeRemoved`, `menuReset`… tous sans effet observable sur le menu.

**N'utilisez pas gol pour écrire des données.**

## En-têtes

| En-tête | Valeur | Notes |
|---|---|---|
| `authorization` | `Bearer <accessToken>` | refresh sur 401 puis retry |
| `x-jow-withmeta` | `1` ou `true` | `true` requis pour `/recipe/{id}` ; `1` accepté ailleurs |
| `accept` | `application/json, text/plain, */*` | |
| `origin` / `referer` | `https://jow.fr` | recommandé (vérification CORS-like observée) |
| `user-agent` | navigateur | certains endpoints refusent les UA vides |

CORS : seuls `authorization` et `x-jow-withmeta` sont autorisés en
preflight (vérifié via `OPTIONS`).

## Pièges connus

1. **`/menu` et dérivés en 500** pour certains comptes — toujours
   prévoir le repli `profile/letscook`.
2. **Structure `quantityPerCover` différente** recherche vs détail
   (ingrédient vs constituant) — lire les deux emplacements.
3. **quicksearch en OU logique** — re-ranker sur les mots-clés du
   titre, sinon « burger asiatique » renvoie un burger tex-mex.
4. **`limit` plafonné à 50** par page sur quicksearch ; paginer avec
   `start` (pas de curseur).
5. **Listes vides ambiguës** : favoris/shoppinglist vides ≠ auth KO —
   contrôler avec `/profile`.
6. **POST shoppinglist/open écrase** la liste entière — merger avant.
7. **Access token 48 h** : le refresh doit être automatique (401 →
   refresh → retry), pas planifié.
8. **`populateRecipes`/`populateIngredients`** requis en query sur
   l'écriture de liste pour que la réponse soit peuplée.

---

## Sources de ces connaissances

- Code du site jow.fr (bundle Next.js : définitions des routes dans les
  chunks — recherche `getMenu:`, `createShoppingList:`, etc.)
- Appels réels documentés pendant le développement de cette
  intégration (septembre 2026)
- Les commits de ce dépôt référencent les découvertes : recherche en OU
  (0.9.5), structure quantityPerCover (0.11.1), route letscook
  (0.11.2), écriture shoppinglist/open (0.12.1)