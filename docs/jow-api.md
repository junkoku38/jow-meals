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
3. [Suggestions & recommandations](#suggestions--recommandations)
4. [Compte utilisateur](#compte-utilisateur)
5. [Menu / liste de courses](#menu--liste-de-courses)
6. [Édition & interaction recettes](#édition--interaction-recettes)
7. [Contenus éditoriaux & divers](#contenus-éditoriaux--divers)
8. [Analytics (gol)](#analytics-gol--à-éviter)
9. [En-têtes](#en-têtes)
10. [Pièges connus](#pièges-connus)
11. [Inventaire complet des routes observées](#inventaire-complet-des-routes-observées)

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

## Suggestions & recommandations

### `POST /recipes/reco/main?availabilityZoneId=FR` ⭐ route clé

Le **moteur de recommandation du site** (la home « Mes suggestions »).
Access token recommandé (répond aussi anonymement).

- Corps : `{}` fonctionne ; `count`, et surtout `userProfile`
  (`{eatingHabits: {vegetarian, vegan, porkless, glutenFree, dairyFree,
  pescatarian}, excludedIngredientTastes: [ids], ownedRecipeTools: []}`)
  pour personnaliser — c'est ce que le site envoie
- Réponse : `{"data": {"mainMeals": [15 recettes], "suggestions": [3]}}`
- Recettes complètes (title, imageUrl, composition, constituents…)

### `POST /recipes/reco/more?availabilityZoneId=FR&count=N`

Recommandations additionnelles (« encore des idées »).

- Corps : `{"context": "cookbook-menu", "excludedRecipesIds": ["…"],
  "count": N, "userProfile": {…}}` — `excludedRecipesIds` permet le
  « suggest-moi autre chose » du cookbook
- Réponse : `{"data": [recettes]}` (liste, pas de wrapper)

### `POST /recipes/filtered-search` ⭐ recherche avancée

Le moteur de recherche **filtré** (contrairement à quicksearch, il
accepte les paramètres en query string) :

`POST /recipes/filtered-search?returnUnavailable=true&availabilityZoneId=FR&query=curry&limit=5&sortByAvailability=true`
corps `{}` — réponse `{"data": {"content": […], "totalCount": n}}`.

### `POST /recipes/search` et `POST /recipes`

Variantes de recherche par lots (non testées en détail).

### `POST /menu/budget/compute-price` (menu par budget)

Le générateur de menu par budget du site. Corps observé dans le bundle :
`{groceryBudget, recipesCount, recipeBudgets, userProfile}` — la config
borne le budget (voir `GET /menu/budget/config` →
`{groceryBudget: {min, max, value, step}}` en centimes). ⚠️ Répondu
**500** sur notre compte de test (état serveur) — mécanisme confirmé
par le code du site, à revalider sur un compte sain.

## Compte utilisateur

### `GET /profile/unified` ⭐ route clé

Le profil **complet** en un appel : `data.jowProfile` contient
`eatingHabits` (`{porkless, vegetarian, vegan, glutenFree, dairyFree,
pescatarian}`), `excludedIngredientTastes` (goûts exclus avec ids et
noms), `favoriteRecipes`, `ownedRecipeTools`, `household`, adresse,
téléphone, `profileCompletion`… Plus `psp`, `cards`, `insights`,
`blockedEntities`, flags de features. **Source idéale pour
`jow.sync_preferences`** (une seule requête au lieu de deviner).

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

### `GET /shoppinglist?availabilityZoneId=FR` ⭐ route clé

La liste de courses du compte **avec ingrédients agrégés** : contrairement
à `profile/letscook`, la réponse contient `recipeIngredients[]` — chaque
ingrédient avec `naturalUnitAmount`, `alternativeUnitsAmounts` (conversions
d'unités pré-calculées !), `source`. `manuallyAddedItems[]` pour les
articles ajoutés à la main. `204` = liste vide.

### `DELETE /shoppinglist/open`

Vide la liste ouverte (le menu). À combiner avec le POST pour une
réécriture propre.

### `POST /shoppinglist/open` (rappel) — écriture du menu

Le `isCooked` **dans le corps du POST** marque un plat comme cuisiné :
observé en test, le plat marqué ressort de la réponse — comportement
probable : les plats cuits sont retirés de la liste active (c'est ainsi
que le site « passe » un repas). ⚠️ À confirmer sur un compte sain ; le
endpoint `POST /recipe-actions` (voir plus bas) est probablement la voie
officielle, mais il répond 500 sur notre compte de test.

- `PATCH /shoppinglist/open/ingredient/{ingredientId}` — ajuste une
  quantité d'ingrédient
- `POST /shoppinglist/open/manual-item` / `PATCH`/`DELETE
  /shoppinglist/open/manual-item/{id}` — articles manuels
- `PUT /shoppinglist/open/validate` — valide/soumet la liste
- `GET /shoppinglists/validated` — listes validées
- `GET /shoppinglist/{shoppingListId}` — une liste précise

### `GET /menu/week` et `GET /menu` ❌ instables

Endpoints « officiels » du menu… **observés en erreur 500** (`{"error":
{"code": "ERROR"}}`) sur des comptes dont l'état serveur est corrompu
(y compris après reconstruction via le site). L'intégration les tente
en premier et **replie sur `profile/letscook`**, qui porte les mêmes
données. Ne pas construire sur ces routes.

## Édition & interaction recettes

Routes observées dans le bundle (non toutes testées — statut indiqué) :

| Route | Méthode | Rôle | Statut |
|---|---|---|---|
| `/recipe-actions` | POST | Actions sur recette (cuite, vue…) — corps non déterminé | ❌ 500 sur compte de test |
| `/recipes/uploaded` | GET | Ses propres recettes importées : `{recipes, total, links}` | ✔ 200 |
| `/recipes/uploaded/{id}` | PUT / DELETE | Modifier / supprimer sa recette | non testé |
| `/recipe-note` | POST/GET | Notes privées sur une recette (`PATCH/DELETE /recipe-note/{id}`) | non testé |
| `/feedback/recipe/{recipeId}` | POST | Noter une recette (`POST/DELETE /feedback/{id}/like` pour les likes) | non testé |
| `/recipe/{id}/sendcard` | POST | Envoyer une carte recette (partage) | non testé |
| `/recipe/report` | POST | Signaler une recette | non testé |
| `/users/{userId}/collections` | GET/POST | **Testé ✔** — lecture : `data.content[]` ; création (v1.2) : corps `{collection: {title, isPrivate}}` (⚠️ title à la racine → 500) | ✔ |
| `/users/{userId}/collections/populate` | POST | **Testé ✔** — `{recipeId, source, collectionsIds}`. ⚠️ **PIÈGE** : ne PAS passer `availabilityZoneId` en query — avec le param, l'API répond 200 mais n'écrit que les favoris, ignorant les collections custom | ✔ |
| `/collections/{collectionId}` | GET | Lecture d'une collection + ses recettes : `data.content.collection.recipes[]` | ✔ |
| `/recipes/uploaded` | GET | Recettes maison (créées via l'**app mobile** — la création n'est pas exposée dans l'API web ; PUT/DELETE sur existant) | ✔ |
| `/users/{userId}/collections/favorites` | POST | Ajouter un favori (le DELETE existe-t-il ? à vérifier) | non testé |
| `/share/link` | GET | Lien de partage du menu (`/recipe/{id}`, `/collection/{id}`, `/user/{id}`, `/meals`, `/details`) | non testé |
| `/signup/magiclink` | POST | Magic link de connexion | non testé |
| `/auth` / `/auth/attach` / `/auth/clone` | POST/GET | Connexion, rattachement de compte, clonage de session | non testé |

## Contenus éditoriaux & divers

- `GET /edito?availabilityZoneId=FR` ✔ — la home du site :
  `banners[]` (`{image, url}`) + `recipesLists[]` (18 listes thématiques
  avec `{id, label, route}` — ex. « Favoris », …)
- `GET /config?availabilityZoneId=FR` ✔ — configuration globale
  (`apiUrl`, `assetsBaseUrl`, tokens publics, textes…)
- `GET /challenges` ✔ — défis du compte (`{challenges, walletAmount,
  lockedChallengesDetails}`) ; `GET /challenges/{id}/state`
- `GET /recipes/featured` / `GET /recipes/visible` / `GET
  /ingredients/visible` — catalogues
- `GET /ingredients`, `/ingredients/search`, `/ingredients/units` —
  ingrédients et unités
- `GET /notifications` (+ `PUT /notifications`, `PUT
  /notifications/hide/{id}`) — notifications du compte
- `GET /blog/latest`, `/blog/menu-of-the-week` — blog
- `GET /translations/web.json` — traductions
- Commandes (`/orders*`, `/order*`, `/potential-order`), livraison
  (`/provider*`, `/stores*`), paiements (`/payment*`, `/payouts*`),
  social (`/social*`, `/users/search`), parrainage (`/refer-friends`,
  `/referral/*`) — hors périmètre de cette intégration, voir
  l'inventaire complet ci-dessous.

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
---

## Inventaire complet des routes observées

Extrait des définitions du bundle du site (181 routes, septembre 2026).
Format `MÉTHODE chemin`. ⭐ = utilisée par cette intégration, ✔ = testée
pendant l'exploration, ❌ = testée et instable, · = non testée.

```
· GET    /address/suggestions
· POST   /auth                      · POST   /auth/attach          · GET /auth/clone
✔ POST   /auth/refresh ⭐
· GET    /blog/latest               · GET    /blog/menu-of-the-week
· GET    /challenges                · GET    /challenges/:id/state
· GET    /config ⭐·                · GET    /config/availability_zones/:zoneId
· GET    /config/helpcenter         · GET    /config/timezone
· POST   /faq/config/:zoneId/filters  · POST /faq/config/:zoneId/config
· GET    /edito ✔
· GET    /ingredient/:id            · GET    /ingredient/:id/check-quantity
· GET    /ingredients               · GET    /ingredients/visible
· GET    /ingredients/search        · GET    /ingredients/search_ingredient_tastes
· GET    /ingredients/units         · GET    /ingredients/most_excluded_ingredient_tastes
· GET    /ingredients/uploaded/:id
· GET    /landing_page/:slug        · GET    /marketing_operation/:slug
❌ GET    /menu                      ❌ GET    /menu/week
· GET    /menu/:menuId              · POST   /menu/budget/compute-price (❌ 500 sur compte test)
· GET    /menu/budget/config ✔      · GET    /menu/ingredients/suggested (·404 en GET direct)
· GET    /notifications             · PUT    /notifications         · PUT /notifications/hide/:id
· GET    /optins                    · PATCH  /optins
· POST   /order (+/:id pay/update/validate, /history, /external/*)
· GET    /order_confirmation/:shortId (+validate/decline/viewed)
· GET    /orders (+/:id, /:id/calendar, /:id/tracking, /:id/priceDetails)
· GET    /payment/session
· GET    /potential-orders/:id      · POST   /potential-order
· GET    /profile ⭐✔               · GET    /profile/blocked_entities
· GET    /profile/letscook ⭐✔      · GET    /profile/letscook/menus ✔
· GET    /profile/letscook/menus/:entityType/:entityId
· GET    /profile/letscook/orders (+:orderId)   · GET /profile/letscook/shared
· GET    /profile/unified ✔         · GET    /profile/promocodes
· GET    /profile/cards (DELETE :pspCardId)
· GET    /provider* (cart, products, stores, delivery/slots…) — commande en ligne
· GET    /payouts/:id (+:id/pay, anonymous/*)
· POST   /prospect
✔ POST   /recipe/quicksearch ⭐
✔ GET    /recipe/:recipeId ⭐       · GET    /recipe/profiled/:recipeId   · POST /recipe/profiled
❌ POST   /recipe-actions (500 sur compte test — actions cuites/vues ?)
· POST   /recipe/:id/sendcard      · POST   /recipe/report
· GET    /recipe/:partnerId/partner (+/isPartnerRecipeEligible)
· GET    /recipe-note (POST/PATCH/DELETE)
· POST   /recipes                  · GET    /recipes/featured      · GET /recipes/visible
✔ POST   /recipes/filtered-search  · POST   /recipes/search       · POST /recipes/reco/main ✔
✔ POST   /recipes/reco/more        · POST   /recipes/partner/search
· POST   /recipes/recipesFromIngredientsId
· GET    /recipes/uploaded (PUT/DELETE /:id)
✔ GET    /recipes/favorites ⭐ (route intégration : /recipes/favorites)
· POST   /feedback/recipe/:id (POST/DELETE /feedback/:id/like, GET /recipe/:id/feedbacks)
✔ POST   /shoppinglist/open ⭐      ✔ GET    /shoppinglist ⭐       · GET /shoppinglist/:id
· DELETE /shoppinglist/open        · PUT    /shoppinglist/open/validate
· POST   /shoppinglist/open/manual-item (PATCH/DELETE /:id)
· PATCH  /shoppinglist/open/ingredient/:ingredientId
· GET    /shoppinglists/validated
❌ POST   /gol (analytics — ne rien écrire ici)
· POST   /openlog
· GET    /share/link (+/recipe/:id, /collection/:id, /user/:id, /meals, /details)
· GET    /stores_public/all        · GET    /terms/sales           · GET /translations/web.json
· POST   /signup/magiclink
· GET    /users/:userId/profile (+/recipes, /collections, /collections/follow)
· GET    /users/search             · GET    /social* (certified-users, search, links)
· POST   /users/:userId/collections (+/populate, /:id/modify, /:id/archive, /favorites)
· GET    /refer-friends            · GET    /referral/advocate-program
· POST   /wallet/campaigns/:id/products
```
