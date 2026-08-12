# Intégration Jow pour Home Assistant (non officielle)

Planifie les repas de la semaine à partir des recettes Jow, agrège les ingrédients
dans une liste de courses, et affiche pour chaque jour une vignette cliquable
renvoyant vers la recette sur jow.fr.

> ⚠️ **Avertissement.** Jow ne publie pas d'API officielle. Cette intégration
> s'appuie sur le paquet communautaire [`jow-api`](https://pypi.org/project/jow-api/)
> (MIT, non affilié à Jow, dernière version en 2023). Elle n'accède **pas** à
> votre compte Jow : le planning est construit et stocké dans Home Assistant.
> L'API interne peut cesser de fonctionner sans préavis, et son usage n'est
> probablement pas prévu par les CGU de Jow. À utiliser à vos risques.

---

## Installation

1. Copier le dossier `custom_components/jow/` dans votre configuration HA
   (à côté de `configuration.yaml`).
2. Redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Jow**.
4. Indiquer le nombre de couverts par défaut.

Pour une distribution via HACS : publier le dépôt sur GitHub avec un
`hacs.json` (`{"name": "Jow", "render_readme": true}`), puis l'ajouter en
*dépôt personnalisé* de catégorie « Integration ».

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

Exemple :

```yaml
action: jow.plan_meal
data:
  query: poulet curry coco
  weekday: mardi
  covers: 4
```

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
Conversation), et demandez par exemple : « propose-moi cinq dîners équilibrés
autour de 600 kcal et planifie-les de lundi à vendredi ». L'agent enchaîne les
recherches puis les appels à `plan_meal`.

## Pistes d'évolution

- **Entité `calendar`** pour voir le menu dans le calendrier HA (une entrée par
  repas, comme le fait l'intégration Mealie).
- **Petit-déjeuner / déjeuner / dîner** : ajouter une clé `meal_type` dans le
  stockage et multiplier les capteurs.
- **Synchronisation Grocy** : appeler `grocy.add_missing_products_to_shopping_list`
  après `refresh_shopping_list` pour tenir compte du stock réel.
- **Suivi calorique** : le paquet `jow-api` ne renvoie pas les valeurs
  nutritionnelles ; il faudrait les estimer côté LLM ou croiser les ingrédients
  avec Open Food Facts, puis pousser le résultat dans l'intégration
  *Calorie Tracker*.
