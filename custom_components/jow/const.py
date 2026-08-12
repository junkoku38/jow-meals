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
ATTR_ENTRY_NAME = "entry_name"

# Service jow.suggest
SERVICE_SUGGEST = "suggest"
SERVICE_SYNC_PROFILE = "sync_profile"
SERVICE_SYNC_FAVORITES = "sync_favorites"
SERVICE_SYNC_PREFERENCES = "sync_preferences"
SERVICE_MEAL_DONE = "meal_done"
SERVICE_SYNC_CALORIES = "sync_calories"
SERVICE_SEND_MENU = "send_menu"
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
CONF_JOW_TOKEN = "jow_token"
CONF_JOW_REFRESH_TOKEN = "jow_refresh_token"

# API Jow auth
JOW_API_BASE = "https://api.jow.fr/public"
JOW_AUTH_URL = f"{JOW_API_BASE}/auth"
JOW_AUTH_REFRESH_URL = f"{JOW_API_BASE}/auth/refresh"
JOW_PROFILE_URL = f"{JOW_API_BASE}/profile"
JOW_FAVORITES_URL = f"{JOW_API_BASE}/recipes/favorites"
JOW_MENU_URL = f"{JOW_API_BASE}/menu/week"
JOW_SHOPPING_URL = f"{JOW_API_BASE}/shoppinglist"
JOW_ORDERS_URL = f"{JOW_API_BASE}/orders"
# Access token valide 48h ; refresh token valide ~6 mois.
# On rafraîchit toutes les 24h (avant expiration du access) pour rester
# discret côté serveur et garder une marge de sécurité.
JOW_TOKEN_REFRESH_INTERVAL = 24 * 3600

# Google Sign-In (récupéré depuis jow.fr)
GOOGLE_CLIENT_ID = "908774541818-9tvsq3p6lggasvdnc3iq6i4mckf9e3ru.apps.googleusercontent.com"
# sync_calories service
# force re-download
