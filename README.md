# Intégration Jow pour Home Assistant (non officielle)

Planifie les repas de la semaine à partir des recettes Jow, agrège les ingrédients
dans une liste de courses, et affiche pour chaque jour une vignette cliquable
renvoyant vers la recette sur jow.fr. Inclut un service de **suggestion par IA**
qui adapte les recettes aux allergies, préférences et météo.

> ⚠️ **Avertissement.** Jow ne publie pas d'API officielle. Cette intégration
> interroge directement `https://api.jow.fr/public/recipe/quicksearch` (endpoint
> public non documenté). Elle n'accède **pas** à votre compte Jow : le planning
> est construit et stocké dans Home Assistant. L'API interne peut cesser de
> fonctionner sans préavis, et son usage n'est probablement pas prévu par les
> CGU de Jow. À utiliser à vos risques.

---

## Installation

### Via HACS (recommandé)

1. **HACS → ⋮ → Dépôts personnalisés**
2. URL : `https://github.com/junkoku38/jow-meals` · Catégorie : `Integration` → **Ajouter**
3. **+ Explorer et télécharger des dépôts** → **Jow** → **Télécharger**
4. Redémarrer Home Assistant
5. **Paramètres → Appareils et services → + Ajouter une intégration → Jow**

### Manuellement

1. Copier le dossier `custom_components/jow/` dans votre configuration HA
   (à côté de `configuration.yaml`).
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Jow**.

## Connexion au compte Jow (optionnelle)

La connexion au compte Jow est **optionnelle**. Sans token, le plugin fonctionne
avec l'API publique (recherche de recettes, planning, liste de courses, suggestions IA).

Avec un token Jow, le plugin synchronise en plus :
- **Allergènes** depuis votre compte Jow (ex. coriandre, fruits à coque, poisson)
- **Habitudes alimentaires** (végétarien, sans gluten, etc.)
- **Foyer** (nombre d'adultes, enfants)
- **Favoris** et **liste de courses** Jow

Les allergènes sont utilisés pour **filtrer automatiquement** les suggestions de
`jow.suggest` : les recettes contenant un ingrédient exclu n'apparaissent pas.

### Comment ça marche

1. Le plugin utilise l'**API publique** Jow pour les recettes (pas de token requis)
2. Le token Jow sert uniquement à **synchroniser les allergènes/préférences**
3. Le refresh token (valide **~6 mois**) permet de générer un access token
   (valide **48h**) automatiquement, toutes les **24h**, sans intervention
4. Quand le refresh token expire, le plugin continue de fonctionner normalement
   avec l'API publique + les derniers allergènes syncés
5. Pour renouveler : récupérez un nouveau refresh token sur jow.fr et mettez-le
   à jour dans la configuration de l'intégration

> ℹ️ Le refresh token est persisté dans la config entry : il survit aux
> redémarrages de Home Assistant. Une seule saisie suffit pour ~6 mois
> d'autonomie.

### Récupérer le refresh token

1. Allez sur [jow.fr](https://jow.fr) et connectez-vous à votre compte
2. Ouvrez la console développeur (**F12** → onglet **Console**)
3. Tapez :
   ```js
   JSON.parse(localStorage.getItem('jow_store')).data.refreshToken
   ```
4. Copiez la chaîne `eyJ...` (c'est votre refresh token)
5. Dans Home Assistant : **Paramètres → Appareils et services → Jow → Configurer**
   → collez le refresh token dans le champ dédié

## Entités créées

| Entité | Contenu |
|---|---|
| `sensor.jow_lundi` … `sensor.jow_dimanche` | Nom de la recette du jour, `entity_picture` = vignette, attributs `url`, `ingredients`, `preparation_time`, `cooking_time`, `covers`, `date` |
| `sensor.jow_repas_du_jour` | Idem, pour la date du jour |
| `sensor.jow_ingredients_a_sauver` | Ingrédients périssables expirant sous 3 jours (anti-gaspillage, mode rescue) |
| `sensor.jow_synchro` | Santé de la connexion jow.fr (`ok` / `token_expiré` / `sans_compte`) + attributs de divergence — base d'alertes |
| `sensor.jow_compte` | Compte connecté, allergies/préférences synchronisées, agent IA |
| `sensor.jow_plats_dans_jow` | Plats réels de la liste ouverte jow.fr (cache des synchros, avec le détail) |
| `calendar.jow_menu` | **Le menu comme calendrier HA** — un événement par repas (19:00), automatisable nativement |
| `todo.jow_courses` | Liste de courses, cochable, éditable |
| `todo.jow_liste_approuvee` | Articles à toujours acheter (hors planning), fusionnés automatiquement avec `todo.jow_courses` lors du `refresh_shopping_list` |

## Commande d'ingrédients (partenaires)

### Commande d'ingrédients — état : lecture uniquement

L'API jow attache les sessions magasin au navigateur (cookie sticky + MFA par webview) : **la commande se fait sur jow.fr ou l'app mobile** (votre menu y est synchronisé). Les services `order_*` restent disponibles pour la lecture (enseignes) — les écritures renverront un diagnostic explicite tant que la politique de jow ne change pas.

## Blueprints## Blueprints d'automatisation

Trois blueprints prêts à importer (Blueprints →Importer un blueprint, URL du fichier) :
- **`jow_digest_matin`** — notification du matin : repas du soir, temps, kcal, périssables à sauver
- **`jow_alerte_synchro`** — prévient quand le token/synchro se dégrade (agir avant de tout perdre)
- **`jow_renouvellement_semaine`** — dimanche soir : renouvelle la semaine prochaine + régénère les courses + notifie

## Services

| Service | Rôle |
|---|---|
| `jow.plan_meal` | Cherche une recette et l'épingle sur un jour |
| `jow.clear_meal` / `jow.clear_week` | Efface un repas / la semaine |
| `jow.refresh_shopping_list` | Régénère la liste de courses depuis le planning |
| `jow.search` | Renvoie des recettes (réponse de service, pour un agent LLM) |
| `jow.suggest` | Suggère des recettes via l'IA (allergies + préférences + météo) |

### `jow.plan_meal`

```yaml
action: jow.plan_meal
data:
  query: poulet curry coco
  weekday: mardi
  covers: 4
```

### `jow.suggest` — suggestion par IA

Génère une requête de recherche adaptée aux **allergies**, **préférences** et
**météo** via un agent `ai_task` (Ollama, OpenAI, Gemini…), puis interroge l'API
Jow et renvoie les recettes correspondantes.

```yaml
action: jow.suggest
data:
  criteria: plat frais pour canicule
  limit: 3
  covers: 2
  weather_entity: weather.maison
  ai_entity: ai_task.ollama_ai_task
```

| Champ | Rôle | Requis |
|---|---|---|
| `criteria` | Demande libre (ex. « plat frais pour canicule », « dîner léger végétarien ») | Non (si absent, l'IA utilise allergies + préférences seules) |
| `limit` | Nombre de résultats (1-20) | Non (défaut 5) |
| `covers` | Nombre de couverts | Non (défaut = config) |
| `weather_entity` | Capteur météo pour contextualiser | Non (défaut = config) |
| `ai_entity` | Agent `ai_task.*` | Non (défaut = config) |

**Flux** : `ai_task.generate_data` → requête Jow contextualisée → `jow.search`
→ recettes avec ingrédients, URL, image.

**Fallback** : si l'IA échoue ou n'est pas configurée, `criteria` est utilisé
directement comme requête de recherche.

### `jow.search`

```yaml
action: jow.search
data:
  query: salade fraîche
  limit: 5
```

Renvoie `{recipes: [...]}` — utile pour un agent conversationnel ou un script.

## Carte dédiée « menu de la semaine »

Une carte Lovelace complète (vignettes, drag & drop, favoris, boutons ± couverts,
suggestions IA, multi-instance) est disponible dans le dépôt
[jow-card-ha](https://github.com/junkoku38/jow-card-ha) (type
`custom:weekly-menu-card`, installable via HACS).

## Carte « menu de la semaine » avec vignettes et liens (markdown, sans extension)

Carte markdown, une ligne par jour, image + lien vers la recette :

```yaml
type: markdown
title: Menu de la semaine
content: >-
  {% set jours = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche'] %}
  {% for j in jours %}
  {% set e = 'sensor.jow_' ~ j %}
  {% if state_attr(e, 'planned') %}
  ### {{ j | capitalize }}
  [![{{ states(e) }}]({{ state_attr(e, 'image') }})]({{ state_attr(e, 'url') }})
  **[{{ states(e) }}]({{ state_attr(e, 'url') }})** — {{ state_attr(e, 'preparation_time') }} min
  {% else %}
  ### {{ j | capitalize }} — _rien de prévu_
  {% endif %}
  {% endfor %}
```

Grande vignette du repas du soir (l'`entity_picture` est reprise automatiquement) :

```yaml
type: picture-entity
entity: sensor.jow_repas_du_jour
show_state: true
show_name: false
tap_action:
  action: url
  url_path: "{{ state_attr('sensor.jow_repas_du_jour', 'url') }}"
```

> `picture-entity` n'accepte pas les templates dans `url_path` selon les
> versions ; en cas de souci, passer par `custom:button-card` (HACS) ou par la
> carte markdown ci-dessus, qui gère le lien nativement.

Liste de courses :

```yaml
type: todo-list
entity: todo.jow_courses
```

Liste approuvée (articles récurrents hors planning — sel, huile, produit ménager,
etc.) : ces articles sont fusionnés automatiquement avec les ingrédients du
planning à chaque appel à `jow.refresh_shopping_list`, en dédoublonnant sur le
libellé normalisé (casse et espaces ignorés).

```yaml
type: todo-list
entity: todo.jow_liste_approuvee
```

## Automatisation : liste de courses du dimanche soir

```yaml
alias: Courses Jow du dimanche
triggers:
  - trigger: time
    at: "18:00:00"
conditions:
  - condition: time
    weekday: [sun]
actions:
  - action: jow.refresh_shopping_list
    data:
      week_offset: 1
      keep_checked: false
```

## Planifier la semaine via un agent conversationnel

Exposez `jow.search` et `jow.plan_meal` à votre agent LLM (Anthropic / OpenAI
Conversation, ou Ollama via `conversation.*`), et demandez par exemple :
« propose-moi cinq dîners équilibrés autour de 600 kcal et planifie-les de
lundi à vendredi ». L'agent enchaîne les recherches puis les appels à
`plan_meal`.

## Automatisation : suggestions adaptées à la météo

Utilisez `jow.suggest` dans une automatisation pour proposer des plats frais
en cas de canicule, ou des plats réconfortants en hiver :

```yaml
alias: Suggestion Jow adaptée météo
triggers:
  - trigger: time
    at: "17:00:00"
conditions:
  - condition: numeric_state
    entity: weather.maison
    attribute: temperature
    above: 28
actions:
  - action: jow.suggest
    data:
      criteria: plat froid et frais pour canicule
      limit: 3
      weather_entity: weather.maison
      ai_entity: ai_task.ollama_ai_task
    response_variable: jow_suggestions
  # Optionnel : épingler la première suggestion sur le jour courant
  # (WEEKDAYS attend un jour en français : lundi, mardi, …)
  - action: jow.plan_meal
    data:
      query: "{{ jow_suggestions.recipes[0].name }}"
      weekday: >
        {{ ["lundi", "mardi", "mercredi", "jeudi", "vendredi",
            "samedi", "dimanche"][now().weekday()] }}
```

## Carte UI de planification

Créez trois input helpers (**Paramètres → Appareils et services → Entrées**) :

| Type | Nom | Options |
|---|---|---|
| Texte | `jow_query` | max 100 caractères |
| Sélection | `jow_day` | `lundi,mardi,mercredi,jeudi,vendredi,samedi,dimanche` |
| Nombre | `jow_covers` | min 1, max 12, slider, init 2 |

Carte Lovelace :

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Planifier un repas
    entities:
      - entity: input_text.jow_query
        name: Recette
      - entity: input_select.jow_day
        name: Jour
      - entity: input_number.jow_covers
        name: Couverts
  - type: button
    name: Planifier
    icon: mdi:silverware-fork-knife
    tap_action:
      action: call-service
      service: jow.plan_meal
      service_data:
        query: "{{ states('input_text.jow_query') }}"
        weekday: "{{ states('input_select.jow_day') }}"
        covers: "{{ states('input_number.jow_covers') | int }}"
```

## Pistes d'évolution

- **Entité `calendar`** pour voir le menu dans le calendrier HA (une entrée par
  repas, comme le fait l'intégration Mealie).
- **Petit-déjeuner / déjeuner / dîner** : ajouter une clé `meal_type` dans le
  stockage et multiplier les capteurs.
- **Synchronisation Grocy** : appeler `grocy.add_missing_products_to_shopping_list`

## Documentation de l'API Jow (non officielle)

L'API de jow.fr n'est ni publique ni documentée. Les routes utilisées par
cette intégration — et tous les pièges découverts en les testant — sont
consignés dans **[docs/jow-api.md](docs/jow-api.md)** : authentification
(refresh 48 h / 6 mois), recherche (OU logique, plafond 50/page), structure
des recettes (deux emplacements pour `quantityPerCover`), le menu réel
(`profile/letscook` + réécriture via `shoppinglist/open`), et les routes
instables à éviter (`/menu`, `gol`).
  après `refresh_shopping_list` pour tenir compte du stock réel.
- **Suivi calorique** : l'API Jow ne renvoie pas les valeurs nutritionnelles ;
  il faudrait les estimer côté LLM ou croiser les ingrédients avec Open Food
  Facts, puis pousser le résultat dans l'intégration *Calorie Tracker*.
- **Filtre allergènes post-recherche** : après `jow.search`/`jow.suggest`,
  filtrer les recettes dont les ingrédients contiennent un allergène déclaré,
  plutôt que de compter sur l'IA pour l'éviter.
