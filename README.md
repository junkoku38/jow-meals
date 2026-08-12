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
2. URL : `https://github.com/junkoku38/ha-jow` · Catégorie : `Integration` → **Ajouter**
3. **+ Explorer et télécharger des dépôts** → **Jow** → **Télécharger**
4. Redémarrer Home Assistant
5. **Paramètres → Appareils et services → + Ajouter une intégration → Jow**

### Manuellement

1. Copier le dossier `custom_components/jow/` dans votre configuration HA
   (à côté de `configuration.yaml`).
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Jow**.

## Configuration

Lors de l'ajout (ou via **Paramètres → Appareils et services → Jow → Configurer**) :

| Champ | Rôle | Exemple |
|---|---|---|
| Couverts par défaut | Nombre de couverts pour les recettes | `2` |
| Allergies / interdits | Évités par l'IA lors des suggestions | `fruits à coque, tomates` |
| Préférences | Prises en compte par l'IA | `méditerranéenne, végétarienne` |
| Agent IA ai_task | Entité `ai_task.*` pour les suggestions | `ai_task.ollama_ai_task` |
| Capteur météo | Entité `weather.*` pour contextualiser | `weather.maison` |

## Entités créées

| Entité | Contenu |
|---|---|
| `sensor.jow_lundi` … `sensor.jow_dimanche` | Nom de la recette du jour, `entity_picture` = vignette, attributs `url`, `ingredients`, `preparation_time`, `cooking_time`, `covers`, `date` |
| `sensor.jow_repas_du_jour` | Idem, pour la date du jour |
| `todo.jow_courses` | Liste de courses, cochable, éditable |
| `todo.jow_liste_approuvee` | Articles à toujours acheter (hors planning), fusionnés automatiquement avec `todo.jow_courses` lors du `refresh_shopping_list` |

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

## Carte « menu de la semaine » avec vignettes et liens

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
  - action: jow.plan_meal
    data:
      query: "{{ jow_suggestions.recipes[0].name }}"
      weekday: "{{ now().strftime('%A') | lower }}"
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
  après `refresh_shopping_list` pour tenir compte du stock réel.
- **Suivi calorique** : l'API Jow ne renvoie pas les valeurs nutritionnelles ;
  il faudrait les estimer côté LLM ou croiser les ingrédients avec Open Food
  Facts, puis pousser le résultat dans l'intégration *Calorie Tracker*.
- **Filtre allergènes post-recherche** : après `jow.search`/`jow.suggest`,
  filtrer les recettes dont les ingrédients contiennent un allergène déclaré,
  plutôt que de compter sur l'IA pour l'éviter.
