"""Constantes de l'intégration Jow."""

DOMAIN = "jow"

STORAGE_KEY = "jow.data"
STORAGE_VERSION = 1

SIGNAL_UPDATE = "jow_update"

# Jours de la semaine (index 0 = lundi, comme datetime.weekday())
WEEKDAYS = [
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
]

DEFAULT_COVERS = 2
RECIPE_BASE_URL = "https://jow.fr/recipes/"

# Services
SERVICE_PLAN_MEAL = "plan_meal"
SERVICE_CLEAR_MEAL = "clear_meal"
SERVICE_CLEAR_WEEK = "clear_week"
SERVICE_SEARCH = "search"
SERVICE_REFRESH_SHOPPING_LIST = "refresh_shopping_list"

ATTR_QUERY = "query"
ATTR_DATE = "date"
ATTR_WEEKDAY = "weekday"
ATTR_COVERS = "covers"
ATTR_LIMIT = "limit"
ATTR_CHOICE = "choice"
ATTR_WEEK_OFFSET = "week_offset"

# Service jow.suggest
SERVICE_SUGGEST = "suggest"
ATTR_CRITERIA = "criteria"
ATTR_WEATHER_ENTITY = "weather_entity"
ATTR_AI_ENTITY = "ai_entity"
ATTR_PREFS = "preferences"
ATTR_ALLERGIES = "allergies"

# Options config
CONF_ALLERGIES = "allergies"
CONF_PREFERENCES = "preferences"
CONF_AI_ENTITY = "ai_entity"
CONF_WEATHER_ENTITY = "weather_entity"
