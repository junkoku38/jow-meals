"""Client Jow et stockage du planning / de la liste de courses."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
from datetime import date, timedelta
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
import requests

from .const import (
    CONF_JOW_REFRESH_TOKEN,
    CONF_JOW_TOKEN,
    DEFAULT_COVERS,
    JOW_AUTH_REFRESH_URL,
    JOW_FAVORITES_URL,
    JOW_MENU_URL,
    JOW_PROFILE_URL,
    JOW_SHOPPING_URL,
    JOW_TOKEN_REFRESH_INTERVAL,
    RECIPE_BASE_URL,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)

# Limites défensives sur les données issues de l'API Jow (non officielle).
_MAX_FIELD_LEN = 2000
_MAX_NAME_LEN = 200
_MAX_ITEMS = 500
_MAX_SUMMARY_LEN = 500

# Mémoire des rejets : un plat effacé (« je ne veux pas de celui-là »)
# reste exclu des suggestions pendant cette durée, même s'il n'est plus
# dans le planning ; au-delà il peut revenir. Borné en nombre d'entrées.
REJECT_MEMORY_DAYS = 60
_MAX_REJECTED = 200

# Anti-répétition du CHOIX : au-delà des ids exclus, on écarte aussi les
# plats partageant le même mot-clé fort (curry, risotto, tajine…) que les
# N derniers plats planifiés/rejetés — sinon une semaine peut recevoir
# « curry lentilles », « curry poulet », « curry légumes ».
_SIMILAR_WINDOW = 6
_GENERIC_WORDS = {
    "recette", "plat", "facon", "façon", "style", "maison", "rapide",
    "simple", "facile", "express", "light", "leger", "léger", "vrai",
    "petit", "grand", "bon", "filet", "morceaux", "restes", "assorti",
}

# Péremption : durée de conservation (jours) par mot-clé d'ingrédient.
# Heuristique volontairement grossière (moyenne ménagère) — utilisée par
# le mode « rescue » de suggest pour prioriser les plats qui écoulent
# ce qui expire, et par le capteur de péremption.
_SHELF_LIFE_DAYS: dict[str, int] = {
    # viande/poisson frais : 2-3 jours
    "poulet": 2, "viande": 3, "boeuf": 3, "bœuf": 3, "porc": 3, "agneau": 3,
    "dinde": 2, "steak": 2, "haché": 2, "saucisse": 4, "lardon": 4, "jambon": 5,
    "poisson": 2, "saumon": 2, "cabillaud": 2, "colin": 2, "thon frais": 2,
    "crevette": 2, "crevettes": 2,
    # crèmerie : 5-15 jours
    "lait": 5, "yaourt": 10, "fromage": 15, "emmental": 15, "mozzarella": 10,
    "chèvre": 10, "comté": 30, "beurre": 20, "crème": 10, "oeuf": 14, "œuf": 14, "oeufs": 14,
    # fruits/légumes : 4-14 jours
    "salade": 4, "laitue": 4, "épinard": 3, "epinard": 3, "tomate": 7, "tomates": 7,
    "concombre": 5, "courgette": 7, "aubergine": 7, "champignon": 4, "champignons": 4,
    "banane": 4, "fraise": 2, "fraises": 2, "framboise": 2, "pêche": 4, "abricot": 4,
    "avocat": 4, "herbe": 4, "basilic": 3, "persil": 5, "cilantro": 3,
    "carotte": 14, "carottes": 14, "poireau": 10, "oignon": 30, "oignons": 30,
    "ail": 30, "pomme de terre": 30, "patate": 30, "patates": 30,
    # épicerie fraîche
    "tofu": 5, "saucisson": 30, "lardons": 4,
}

# API Jow (non officielle).
_JOW_SEARCH_URL = "https://api.jow.fr/public/recipe/quicksearch"
_JOW_RECIPE_URL = "https://api.jow.fr/public/recipe"
_JOW_STATIC_URL = "https://static.jow.fr/"
_JOW_HEADERS = {
    "accept": "application/json",
    "accept-language": "fr",
    "content-type": "application/json",
    "x-jow-withmeta": "1",
    "origin": "https://jow.fr",
    "referer": "https://jow.fr/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
_ALLOWED_URL_SCHEMES = {"http", "https"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_url(value: Any, fallback: str | None = None) -> str | None:
    """Retourne une URL HTTP(S) sûre, ou fallback, ou None."""
    if not value or not isinstance(value, str):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme in _ALLOWED_URL_SCHEMES and parsed.netloc:
        return value
    _LOGGER.warning("URL Jow rejetée (schéma/host invalide) : %r", value)
    return fallback


def _safe_static_url(value: Any, fallback: str | None = None) -> str | None:
    """Valide un fragment d'URL statique Jow AVANT concaténation.

    L'API renvoie des chemins relatifs ("img/x.jpg" ou "//cdn/x.jpg") ;
    un fragment malicieux ("javascript:…") collé derrière l'origine
    donnerait une URL en apparence valide après assemblage.
    """
    if not value or not isinstance(value, str):
        return fallback
    text = value.strip()
    # Tout fragment contenant un schéma est rejeté, sauf https explicite.
    if "://" in text or text.lower().startswith(("javascript:", "data:", "vbscript:")):
        parsed = urlparse(text)
        if parsed.scheme == "https" and parsed.netloc:
            return text
        _LOGGER.warning("Fragment d'URL Jow rejeté : %r", value)
        return fallback
    if text.startswith("//"):
        # "//cdn.jow.fr/x" : schéma-relatif -> https explicite
        if urlparse(text).netloc:
            return f"https:{text}"
        return fallback
    # Chemin relatif simple ("img/x.jpg", "/img/x.jpg") : assemblage sûr,
    # ni schéma ni protocole possible.
    return f"{_JOW_STATIC_URL}{text.lstrip('/')}"


def _safe_id(value: Any) -> str | None:
    """Retourne un identifiant alphanumérique safe, ou None."""
    if not value:
        return None
    text = str(value)
    return text if _ID_RE.match(text) else None


def _truncate(value: Any, limit: int) -> str | None:
    """Tronque une chaîne à `limit` caractères ; None si vide."""
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _jow_ingredient_unit(constituent: dict) -> str:
    """Résout l'unité d'un constituant Jow (naturalUnit ou alternativeUnit)."""
    try:
        unit_id = constituent.get("unit", {}).get("id")
        ing = constituent.get("ingredient", {})
        natural = ing.get("naturalUnit", {})
        if natural.get("_id") == unit_id or natural.get("id") == unit_id:
            return natural.get("name", "")
        for alt in ing.get("alternativeUnits", []):
            u = alt.get("unit", {})
            if u.get("_id") == unit_id or u.get("id") == unit_id:
                return u.get("name", "")
    except (TypeError, AttributeError):
        pass
    return ""


# Codes allergènes du règlement INCO (UE) 1169/2011, comme la carte JS.
# 1=gluten, 2=crustacés, 3=œufs, 4=poissons, 5=arachides, 6=soja, 7=lait,
# 8=fruits à coque, 9=céleri, 10=moutarde, 11=sésame, 12=sulfites,
# 13=lupin, 14=mollusques.
#
# Mapping des « tastes » Jow (catégories d'ingrédients) vers les codes INCO.
# Les noms Jow sont matchés en minuscules par inclusion.
_TASTE_TO_INCO: dict[str, int] = {
    "pâtes": 1, "pate": 1, "pâte": 1,  # gluten
    "pain": 1, "farine": 1, "blé": 1, "ble": 1, "seigle": 1, "orge": 1,
    "avoine": 1, "épeautre": 1, "epautre": 1, "couscous": 1, "boulgour": 1,
    "gnocchi": 1, "pizza": 1, "quiche": 1, "feuille de brick": 1,
    "crustacé": 2, "crevette": 2, "crabe": 2, "homard": 2, "langoustine": 2,
    "oeuf": 3, "œuf": 3,
    "poisson": 4, "saumon": 4, "thon": 4, "cabillaud": 4, "morue": 4,
    "sardine": 4, "anchois": 4, "merlu": 4, "lieu": 4,
    "arachide": 5, "cacahuète": 5, "cacahuete": 5,
    "soja": 6, "tofu": 6, "sauce soja": 6,
    "lait": 7, "fromage": 7, "crème": 7, "creme": 7, "beurre": 7,
    "yaourt": 7, "mozzarella": 7, "parmesan": 7, "comté": 7, "comte": 7,
    "emmental": 7, "gruyère": 7, "gruyere": 7, "feta": 7, "ricotta": 7,
    "mascarpone": 7, "chèvre": 7, "chevre": 7, "roquefort": 7, "bleu": 7,
    "fruits à coque": 8, "noix": 8, "amande": 8, "noisette": 8, "cajou": 8,
    "pistache": 8, "pécan": 8, "pecan": 8, "macadamia": 8,
    "céleri": 9, "celeri": 9,
    "moutarde": 10,
    "sésame": 11, "sesame": 11,
    "sulfite": 12, "vin": 12, "vinaigre": 12,
    "lupin": 13,
    "mollusque": 14, "moule": 14, "huître": 14, "huitre": 14, "coquille": 14,
}



def _deduce_allergens(recipe: Any) -> tuple[list[int], str]:
    """Déduit les codes allergènes INCO depuis les `tastes` des constituants.

    L'API Jow n'expose pas directement les allergènes INCO, mais chaque
    constituant porte une liste de `tastes` (catégories d'ingrédients).
    On mappe ces noms vers les 14 codes du règlement UE 1169/2011.

    Retourne (codes_triés, source) où source vaut "estimated" car la
    déduction est heuristique (les noms Jow peuvent varier).
    """
    codes: set[int] = set()
    # Clés triées par longueur décroissante : la plus longue matche
    # d'abord à chaque position ("bleu" fromage avant "ble" blé —
    # sinon « Fromage bleu » serait tagué gluten ; « quiche » et
    # « bleu » dans « Quiche au bleu » donnent bien gluten ET lait).
    ordered_keys = sorted(_TASTE_TO_INCO.keys(), key=len, reverse=True)
    for const in recipe.get("constituents", []) or []:
        ing = const.get("ingredient", {})
        for taste in ing.get("tastes", []) or []:
            name = (taste.get("name") or "").lower().strip()
            if not name:
                continue
            pos = 0
            while pos < len(name):
                matched = False
                for key in ordered_keys:
                    if name.startswith(key, pos):
                        codes.add(_TASTE_TO_INCO[key])
                        pos += len(key)
                        matched = True
                        break
                if not matched:
                    pos += 1
    return sorted(codes), "estimated"


def _recipe_to_dict(recipe: Any, covers: int) -> dict:
    """Convertit une recette Jow (dict JSON de l'API) en dict sérialisable.

    On stocke un dict plutôt que l'objet : il doit survivre à un redémarrage
    de Home Assistant et être lisible depuis les templates Jinja.
    """
    if not isinstance(recipe, dict):
        return {}

    ratio = 1.0
    base_covers = recipe.get("roundedCoversCount") or DEFAULT_COVERS
    if base_covers:
        try:
            ratio = covers / float(base_covers)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 1.0

    ingredients = []
    for const in recipe.get("constituents", []) or []:
        ing = const.get("ingredient", {})
        # quantityPerCover vit à DEUX endroits selon l'endpoint :
        # - recherche (quicksearch) : constituant.ingredient.quantityPerCover
        # - détail (/recipe/{id})   : constituant.quantityPerCover
        # Les favoris/suggestions épinglés par recipe_id passent par le
        # détail — ne lire que le premier emplacement perdait les quantités.
        qty_per_cover = ing.get("quantityPerCover")
        if qty_per_cover is None:
            qty_per_cover = const.get("quantityPerCover")
        try:
            qty_per_cover = float(qty_per_cover) if qty_per_cover else None
        except (TypeError, ValueError):
            qty_per_cover = None
        quantity = round(qty_per_cover * ratio, 2) if qty_per_cover else None
        ingredients.append(
            {
                "name": _truncate(ing.get("name", ""), _MAX_NAME_LEN) or "",
                "quantity": quantity,
                "quantity_per_cover": qty_per_cover,
                "unit": _truncate(_jow_ingredient_unit(const), _MAX_NAME_LEN) or "",
                "optional": bool(const.get("isOptional", False)),
            }
        )

    recipe_id = _safe_id(recipe.get("_id") or recipe.get("id"))
    url = f"{RECIPE_BASE_URL}{recipe_id}" if recipe_id else None
    # Valider chaque fragment AVANT concaténation : un imageUrl malicieux
    # ("javascript:…") collé derrière static.jow.fr donnerait une URL en
    # apparence valide après assemblage.
    image = None
    if recipe.get("imageUrl"):
        image = _safe_static_url(recipe["imageUrl"])
        if image is None:
            _LOGGER.warning("imageUrl Jow rejeté : %r", recipe.get("imageUrl"))
    video = None
    if recipe.get("videoUrl"):
        video = _safe_static_url(recipe["videoUrl"])
        if video is None:
            _LOGGER.warning("videoUrl Jow rejeté : %r", recipe.get("videoUrl"))

    # Allergènes INCO déduits des tastes des constituants (heuristique).
    allergens, allergens_source = _deduce_allergens(recipe)

    return {
        "id": recipe_id,
        "name": _truncate(recipe.get("title", "Recette Jow"), _MAX_NAME_LEN) or "Recette Jow",
        "url": url,
        "image": image,
        "video": video,
        "description": _truncate(recipe.get("description"), _MAX_FIELD_LEN),
        "preparation_time": recipe.get("preparationTime"),
        "cooking_time": recipe.get("cookingTime"),
        "covers": covers,
        "calories": recipe.get("_calories"),
        "ingredients": ingredients,
        "allergens": allergens,
        "allergens_source": allergens_source,
    }


# Mapping ingrédient -> rayon pour trier la liste de courses.
# Les noms sont matchés par inclusion (minuscules).
_AISLE_ORDER = [
    "Fruits & Légumes", "Boucherie", "Poissonnerie", "Crémerie",
    "Épicerie salée", "Épicerie sucrée", "Surgelés", "Boissons", "Autre",
]
_AISLE_MAP: dict[str, str] = {
    # Fruits & Légumes
    "tomate": "Fruits & Légumes", "oignon": "Fruits & Légumes",
    "ail": "Fruits & Légumes", "carotte": "Fruits & Légumes",
    "pomme": "Fruits & Légumes", "courgette": "Fruits & Légumes",
    "salade": "Fruits & Légumes", "épinard": "Fruits & Légumes",
    "poireau": "Fruits & Légumes", "potiron": "Fruits & Légumes",
    "citron": "Fruits & Légumes", "banane": "Fruits & Légumes",
    "avocat": "Fruits & Légumes", "persil": "Fruits & Légumes",
    "basilic": "Fruits & Légumes", "herbe": "Fruits & Légumes",
    "champignon": "Fruits & Légumes", "poivron": "Fruits & Légumes",
    "aubergine": "Fruits & Légumes", "brocoli": "Fruits & Légumes",
    "fenouil": "Fruits & Légumes", "céleri": "Fruits & Légumes",
    "endive": "Fruits & Légumes", "radis": "Fruits & Légumes",
    # Boucherie
    "poulet": "Boucherie", "bœuf": "Boucherie", "boeuf": "Boucherie",
    "porc": "Boucherie", "veau": "Boucherie", "agneau": "Boucherie",
    "lard": "Boucherie", "bacon": "Boucherie", "jambon": "Boucherie",
    "saucisse": "Boucherie", "merguez": "Boucherie", "viande": "Boucherie",
    # Poissonnerie
    "saumon": "Poissonnerie", "thon": "Poissonnerie", "cabillaud": "Poissonnerie",
    "crevette": "Poissonnerie", "moule": "Poissonnerie", "poisson": "Poissonnerie",
    # Crémerie
    "lait": "Crémerie", "beurre": "Crémerie", "crème": "Crémerie",
    "creme": "Crémerie", "fromage": "Crémerie", "yaourt": "Crémerie",
    "œuf": "Crémerie", "oeuf": "Crémerie", "mozzarella": "Crémerie",
    "parmesan": "Crémerie", "emmental": "Crémerie", "feta": "Crémerie",
    # Épicerie salée
    "pâtes": "Épicerie salée", "pate": "Épicerie salée", "spaghetti": "Épicerie salée",
    "macaroni": "Épicerie salée", "penne": "Épicerie salée", "tagliatelle": "Épicerie salée",
    "noodle": "Épicerie salée", "lasagne": "Épicerie salée",
    "riz": "Épicerie salée",
    "couscous": "Épicerie salée", "quinoa": "Épicerie salée",
    "lentille": "Épicerie salée", "pois chiche": "Épicerie salée",
    "haricot": "Épicerie salée", "tomate concentré": "Épicerie salée",
    "sauce": "Épicerie salée", "huile": "Épicerie salée",
    "olive": "Épicerie salée", "câpre": "Épicerie salée",
    "bouillon": "Épicerie salée", "épice": "Épicerie salée",
    # Épicerie sucrée
    "sucre": "Épicerie sucrée", "miel": "Épicerie sucrée",
    "chocolat": "Épicerie sucrée", "farine": "Épicerie sucrée",
    "levure": "Épicerie sucrée", "vanille": "Épicerie sucrée",
    # Surgelés
    "surgelé": "Surgelés", "frozen": "Surgelés",
    # Boissons
    "vin": "Boissons", "bière": "Boissons", "biere": "Boissons",
    "jus": "Boissons", "eau": "Boissons", "soda": "Boissons",
}


def _aisle_for(item: str) -> str:
    """Détermine le rayon d'un article de la liste de courses."""
    name = item.lower()
    for key, aisle in _AISLE_MAP.items():
        if key in name:
            return aisle
    return "Autre"


class JowManager:
    """Garde le planning de la semaine et la liste de courses."""

    def __init__(
        self,
        hass: HomeAssistant,
        default_covers: int,
        allergies: str = "",
        preferences: str = "",
        ai_entity: str = "",
        weather_entity: str = "",
        jow_token: str = "",
        jow_refresh_token: str = "",
        entry_id: str = "",
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id

        self.default_covers = default_covers
        self.allergies = allergies
        self.preferences = preferences
        self.ai_entity = ai_entity
        self.weather_entity = weather_entity
        self.jow_token = jow_token
        # Un access token (48h) n'est pas un refresh token (~6 mois) :
        # pas de fallback, sinon /auth/refresh échoue inutilement.
        self.jow_refresh_token = jow_refresh_token
        self._token_refresh_cancel: Any = None
        # Clé de stockage unique par instance pour éviter l'écrasement
        # mutuel en multi-instance.
        store_key = f"{STORAGE_KEY}.{entry_id}" if entry_id else STORAGE_KEY
        self._store: Store = Store(hass, STORAGE_VERSION, store_key)
        # Migration : les versions antérieures stockaient sous "jow.data"
        # (clé partagée). Si la clé par instance est vide, on retombe une
        # fois sur la clé legacy pour ne pas perdre le planning existant.
        self._legacy_store: Store | None = (
            Store(hass, STORAGE_VERSION, STORAGE_KEY) if store_key != STORAGE_KEY else None
        )
        # {"2026-08-10": {recette...}}
        self.plan: dict[str, dict] = {}
        # [{"uid": "...", "summary": "200 g de riz", "done": False}]
        self.shopping: list[dict] = []
        # [{"uid": "...", "summary": "sel", "done": False}]
        # Liste approuvée d'articles à fusionner systématiquement avec
        # la liste de courses générée depuis le planning.
        self.approved: list[dict] = []
        # Favoris Jow (mis en cache par sync_favorites)
        self.favorites: list[dict] = []
        # Ingrédients à éviter (préférence, pas allergie) — modifiable via la carte
        self.avoid_ingredients: list[str] = []
        # Ingrédients interdits (allergie) — modifiable via la carte
        # Synchronisé avec Jow au démarrage, mais l'utilisateur peut en
        # ajouter/retirer manuellement.
        self.banned_ingredients: list[str] = []
        # Plats rejetés (« Effacer ce jour » sur un plat non voulu) :
        # [{"id", "name", "ts"}] — l'anti-répétition doit les éviter même
        # s'ils ne sont plus dans le planning (sinon l'IA les reproposait
        # immédiatement après effacement).
        self.rejected: list[dict] = []
        # Cache de la liste ouverte jow.fr (menu du compte) — rempli par
        # les services de synchro (import_menu/send_menu/meal_done),
        # lu par le capteur d'état « Plats dans Jow ».
        self.jow_open_meals: list[dict] = []
        # Statistiques de synchro (capteur d'état / alertes) :
        # dates et compteurs des derniers import/export menu.
        self.last_import: dict | None = None   # {"ts": iso, "imported": n, "skipped": n}
        self.last_send: dict | None = None     # {"ts": iso, "added": n}

    @property
    def update_signal(self) -> str:
        """Signal dispatcher propre à cette instance (isolation
        multi-instance : un save ne réveille que ses entités)."""
        return f"{SIGNAL_UPDATE}.{self.entry_id}" if self.entry_id else SIGNAL_UPDATE

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data and self._legacy_store is not None:
            # Migration depuis la clé partagée "jow.data" (première
            # instance qui charge récupère les données ; elles seront
            # ré-enregistrées sous la clé par instance au premier save).
            legacy = await self._legacy_store.async_load()
            if legacy:
                _LOGGER.info("Migration du stockage legacy « %s » vers « jow.data.%s »", STORAGE_KEY, self.entry_id)
                data = legacy
        data = data or {}
        self.plan = data.get("plan", {})
        self.shopping = data.get("shopping", [])
        self.approved = data.get("approved", [])
        # Ingrédients interdits / à éviter : persistés pour survivre aux
        # redémarrages (ajoutés via la carte ou le service, pas seulement
        # synchronisés depuis Jow).
        banned = data.get("banned_ingredients", [])
        avoid = data.get("avoid_ingredients", [])
        # Ne garder que des chaînes nettoyées (défense contre un stockage
        # corrompu par une version antérieure).
        self.banned_ingredients = [
            str(b).strip().lower() for b in banned if isinstance(b, str) and b.strip()
        ]
        self.avoid_ingredients = [
            str(a).strip().lower() for a in avoid if isinstance(a, str) and a.strip()
        ]
        # Favoris mis en cache par sync_favorites — persistés pour survivre
        # aux redémarrages (la carte « favoris » reste utilisable hors ligne).
        favs = data.get("favorites", [])
        self.favorites = [f for f in favs if isinstance(f, dict)] if isinstance(favs, list) else []
        # Plats rejetés (persistés) — purgés des entrées trop anciennes
        # (au-delà de REJECT_MEMORY_DAYS jours, le plat peut revenir).
        rejects = data.get("rejected", [])
        if isinstance(rejects, list):
            cutoff_ts = time.time() - REJECT_MEMORY_DAYS * 86400
            self.rejected = [
                r for r in rejects
                if isinstance(r, dict) and r.get("id") and r.get("ts", 0) >= cutoff_ts
            ][:_MAX_REJECTED]

    async def async_save_favorites(self) -> None:
        """Persiste uniquement le cache des favoris."""
        await self._store.async_save(self._storage_dict())

    def _storage_dict(self) -> dict:
        return {
            "plan": self.plan,
            "shopping": self.shopping,
            "approved": self.approved,
            "banned_ingredients": self.banned_ingredients,
            "avoid_ingredients": self.avoid_ingredients,
            "favorites": self.favorites,
            "rejected": self.rejected,
        }

    async def async_save(self) -> None:
        await self._store.async_save(self._storage_dict())
        async_dispatcher_send(self.hass, self.update_signal)

    # ------------------------------------------------------------------
    # Appels à Jow (bloquants -> executor)
    # Note : `requests` synchrone dans l'executor est un pattern supporté
    # par HA ; une migration aiohttp est envisageable mais ces chemins
    # (auth, favoris, envoi menu) ne sont pas couverts par les tests.
    # ------------------------------------------------------------------
    async def async_search(self, query: str, limit: int = 5, start: int = 0) -> list[dict]:
        """Recherche des recettes sur Jow via l'API HTTP directe.

        `start` permet la pagination (l'API plafonne limit à 50 par page).
        """

        def _search():
            params = {
                "start": str(start),
                "availabilityZoneId": "FR",
                "query": query,
                "limit": str(max(limit, 1)),
            }
            # Pas de préflight OPTIONS : le CORS preflight est une
            # contrainte navigateur, inutile côté serveur — il doublait
            # la latence de chaque recherche.
            resp = requests.post(
                _JOW_SEARCH_URL, headers=dict(_JOW_HEADERS), params=params, data="{}", timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return data.get("content", []) if isinstance(data, dict) else []

        try:
            return await self.hass.async_add_executor_job(_search) or []
        except Exception as err:
            _LOGGER.error("Recherche Jow impossible (%s) : %s", query, err)
            return []

    async def async_get_recipe_detail(self, recipe_id: str) -> dict | None:
        """Récupère la recette complète depuis l'endpoint détail de Jow.

        Utilisé pour épingler un favori par id exact (sans recherche par
        titre, qui peut matcher une variante du même nom).
        """
        if not recipe_id or not _ID_RE.match(recipe_id):
            return None

        def _fetch():
            url = f"{_JOW_RECIPE_URL}/{recipe_id}"
            # L'endpoint détail exige x-jow-withmeta: true (et non "1")
            headers = dict(_JOW_HEADERS)
            headers["x-jow-withmeta"] = "true"
            headers["accept"] = "application/json, text/plain, */*"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json()

        try:
            data = await self.hass.async_add_executor_job(_fetch)
            return data if isinstance(data, dict) else None
        except Exception as err:
            _LOGGER.debug("Détail Jow indisponible pour %s : %s", recipe_id, err)
            return None

    async def async_fetch_calories(self, recipe_id: str) -> int | None:
        """Calories/pers depuis l'endpoint détail (nutritionalFacts ENERC).

        Délègue à async_get_recipe_detail (une seule implémentation du
        fetch détail — avant, deux requêtes partaient sur le même
        endpoint pour une même recette planifiée).
        """
        detail = await self.async_get_recipe_detail(recipe_id)
        if not detail:
            return None
        # nutritionalFacts : [{id: "ENERC", label: "Calories", unit: "kcal", amount: N}, ...]
        for fact in detail.get("nutritionalFacts", []) or []:
            if fact.get("id") == "ENERC":
                try:
                    return int(round(float(fact.get("amount", 0))))
                except (TypeError, ValueError):
                    return None
        return None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    @staticmethod
    def monday_of(day: date, week_offset: int = 0) -> date:
        return day - timedelta(days=day.weekday()) + timedelta(weeks=week_offset)

    def week_dates(self, week_offset: int = 0) -> list[date]:
        monday = self.monday_of(date.today(), week_offset)
        return [monday + timedelta(days=i) for i in range(7)]

    def get_meal(self, day: date) -> dict | None:
        return self.plan.get(day.isoformat())

    async def async_plan_meal(
        self, day: date, query: str, covers: int | None = None, choice: int = 1,
        recipe_id: str | None = None,
    ) -> dict | None:
        """Épingle une recette sur un jour.

        recipe_id (optionnel) : épingle directement la recette Jow de cet
        id (déjà connue — favori, suggestion) sans passer par la recherche
        par titre, qui peut matcher une variante du même nom.
        Sinon : cherche par query et prend le choix n° `choice`.
        """
        covers = covers or self.default_covers
        if recipe_id:
            recipe_id = _safe_id(recipe_id)
        if recipe_id:
            detail = await self.async_get_recipe_detail(recipe_id)
            if not detail:
                _LOGGER.warning("Recette Jow %s introuvable — repli sur la recherche", recipe_id)
            else:
                recipe = detail
                rid = _safe_id(recipe.get("_id") or recipe.get("id"))
                if rid:
                    calories = await self.async_fetch_calories(rid)
                    if calories is not None:
                        recipe["_calories"] = calories
                stored = _recipe_to_dict(recipe, covers)
                self.plan[day.isoformat()] = stored
                await self.async_save()
                return stored
        results = await self.async_search(query, limit=max(choice, 1))
        if not results:
            _LOGGER.warning("Aucune recette Jow trouvée pour « %s »", query)
            return None

        recipe = results[min(choice, len(results)) - 1]
        # Récupérer les calories depuis l'endpoint détail (l'API de recherche
        # ne les fournit pas).
        recipe_id = _safe_id(recipe.get("_id") or recipe.get("id"))
        if recipe_id:
            calories = await self.async_fetch_calories(recipe_id)
            if calories is not None:
                recipe["_calories"] = calories
        stored = _recipe_to_dict(recipe, covers)
        self.plan[day.isoformat()] = stored
        await self.async_save()
        return stored

    async def async_clear_meal(self, day: date) -> None:
        """Efface le repas d'un jour.

        Le plat effacé est mémorisé comme rejeté : contrairement à
        meal_done (plat mangé → anti-répétition standard de 4 semaines),
        un effacement manuel traduit un désintérêt — l'anti-répétition
        doit le respecter même hors planning pendant REJECT_MEMORY_DAYS,
        sinon la suggestion suivante le reproposait immédiatement.
        (les plats déjà présents dans la mémoire ne sont pas rejetés
        deux fois — dédupe par id)
        """
        meal = self.plan.pop(day.isoformat(), None)
        if meal and meal.get("id"):
            self._remember_rejected(meal)
        await self.async_save()

    def _remember_rejected(self, meal: dict) -> None:
        """Ajoute un plat à la mémoire des rejets (dédupe par id, bornée)."""
        rid = meal.get("id")
        if not rid:
            return
        self.rejected = [r for r in self.rejected if r.get("id") != rid]
        self.rejected.insert(0, {"id": rid, "name": meal.get("name", ""), "ts": time.time()})
        self.rejected = self.rejected[:_MAX_REJECTED]

    async def async_copy_meal(self, from_day: date, to_day: date) -> dict | None:
        """Copie un repas d'un jour vers un autre (pratique pour les restes)."""
        meal = self.get_meal(from_day)
        if not meal:
            return None
        # Copie profonde : évite que modifier un jour (set_covers, etc.)
        # modifie aussi l'autre par effet d'aliasing.
        self.plan[to_day.isoformat()] = copy.deepcopy(meal)
        await self.async_save()
        return {"copied": meal.get("name", ""), "to": to_day.isoformat()}

    async def async_set_covers(self, day: date, covers: int) -> dict | None:
        """Change le nombre de couverts d'un repas planifié.
        Recalcule les quantités d'ingrédients proportionnellement."""
        meal = self.get_meal(day)
        if not meal:
            return None
        meal["covers"] = covers
        # Recalculer les quantités depuis quantity_per_cover si disponible
        for ing in meal.get("ingredients", []):
            qpc = ing.get("quantity_per_cover")
            if qpc is not None:
                with contextlib.suppress(TypeError, ValueError):
                    ing["quantity"] = round(float(qpc) * covers, 2)
        await self.async_save()
        return {"covers": covers, "day": day.isoformat()}

    async def async_exclude_ingredient(self, ingredient: str) -> dict:
        """Retire un ingrédient de la liste de courses (déjà en stock).

        Matching par nom d'ingrédient complet (après retrait de la
        quantité/Unité) pour éviter les faux positifs (ex: "ail" ne doit
        pas retirer "aileron").
        """
        norm = self._norm(ingredient)
        removed = []
        kept = []
        for item in self.shopping:
            summary_norm = self._norm(item["summary"])
            item_name = self._ingredient_name_from_summary(item["summary"])
            if norm in (item_name, summary_norm):
                removed.append(item["summary"])
            else:
                kept.append(item)
        self.shopping = kept
        await self.async_save()
        return {"removed": removed, "count": len(removed)}

    async def async_clear_recent(self, date_iso: str) -> dict:
        """Retire un plat de l'historique d'anti-répétition.

        Le plat marque _no_exclude et devient re-proposable immédiatement
        (sans attendre la fenêtre de 4 semaines) ; il reste planifié.
        """
        meal = self.plan.get(date_iso)
        if not meal:
            return {"error": "Aucun repas à cette date"}
        meal["_no_exclude"] = True
        await self.async_save()
        return {"cleared": meal.get("name", ""), "date": date_iso}

    async def async_add_avoid_ingredient(self, ingredient: str) -> dict:
        """Ajoute un ingrédient à éviter (préférence, pas allergie)."""
        ing = ingredient.strip().lower()
        if ing and ing not in self.avoid_ingredients:
            self.avoid_ingredients.append(ing)
            await self.async_save()
        return {"avoid_ingredients": self.avoid_ingredients}

    async def async_remove_avoid_ingredient(self, ingredient: str) -> dict:
        """Retire un ingrédient à éviter."""
        ing = ingredient.strip().lower()
        self.avoid_ingredients = [e for e in self.avoid_ingredients if e != ing]
        await self.async_save()
        return {"avoid_ingredients": self.avoid_ingredients}

    async def async_add_banned_ingredient(self, ingredient: str) -> dict:
        """Ajoute un ingrédient interdit (allergie)."""
        ing = ingredient.strip().lower()
        if ing and ing not in self.banned_ingredients:
            self.banned_ingredients.append(ing)
            await self.async_save()
        return {"banned_ingredients": self.banned_ingredients}

    async def async_remove_banned_ingredient(self, ingredient: str) -> dict:
        """Retire un ingrédient interdit."""
        ing = ingredient.strip().lower()
        self.banned_ingredients = [e for e in self.banned_ingredients if e != ing]
        await self.async_save()
        return {"banned_ingredients": self.banned_ingredients}

    async def async_sync_calories(self, week_offset: int = 0) -> int:
        """Récupère les calories manquantes pour tous les repas planifiés.

        Parcourt la semaine demandée et fetch les calories depuis l'endpoint
        détail de Jow pour chaque repas qui n'en a pas encore. Retourne le
        nombre de repas mis à jour.
        """
        days = self.week_dates(week_offset)
        pending = []
        for day in days:
            meal = self.get_meal(day)
            if not meal or meal.get("calories") is not None:
                continue
            recipe_id = _safe_id(meal.get("id"))
            if not recipe_id:
                continue
            pending.append((day, meal, recipe_id))
        results = await asyncio.gather(
            *[self.async_fetch_calories(rid) for _, _, rid in pending],
            return_exceptions=True,
        )
        updated = 0
        for (_day, meal, _), calories in zip(pending, results, strict=False):
            if isinstance(calories, Exception) or calories is None:
                continue
            meal["calories"] = calories
            updated += 1
        if updated:
            await self.async_save()
        return updated

    async def async_clear_week(self, week_offset: int = 0, remember_rejects: bool = True) -> None:
        """Efface tous les repas de la semaine visée.

        remember_rejects : les plats effacés en BLOC sont mémorisés comme
        rejets (le « Renouveler la semaine » ne doit pas reproposer les
        mêmes plats que l'on veut justement changer).
        """
        for day in self.week_dates(week_offset):
            meal = self.plan.pop(day.isoformat(), None)
            if remember_rejects and meal and meal.get("id"):
                self._remember_rejected(meal)
        await self.async_save()

    async def async_renew_week(
        self,
        week_offset: int = 0,
        covers: int | None = None,
        criteria: str = "plat varié équilibré",
        weather_entity: str | None = None,
        ai_entity: str | None = None,
        ai_prompt: str = "",
        max_calories: int | None = None,
        max_total_time: int | None = None,
        day_criteria: dict[str, str] | None = None,
    ) -> dict:
        """Renouvelle toute la semaine : vide puis replanifie via l'IA.

        Un « Renouveler » = clear_week(remember_rejects=True) + remplissage
        jour par jour via async_suggest (overwrite=True : les jours sont
        vides, la garde ne sert à rien) avec la diversité inter-jours
        naturelle du pipeline (familles récentes, rejets). day_criteria
        permet un critère par jour (ex: {"vendredi": "plaisir"}).

        Retour : {"cleared": n, "planned": n, "days": {jour: plat}}.
        """
        # 1) vider en mémorisant les rejets
        cleared = 0
        for day in self.week_dates(week_offset):
            if self.plan.get(day.isoformat()):
                cleared += 1
        await self.async_clear_week(week_offset=week_offset, remember_rejects=True)

        # 2) remplir chaque jour via le pipeline suggest complet
        planned: dict[str, str] = {}
        failures: dict[str, str] = {}
        for idx, day in enumerate(self.week_dates(week_offset)):
            weekday = WEEKDAYS[idx]
            day_crit = (day_criteria or {}).get(weekday, criteria)
            try:
                await self.async_suggest(
                    criteria=day_crit,
                    covers=covers,
                    limit=5,
                    weather_entity=weather_entity,
                    ai_entity=ai_entity,
                    weekday=weekday,
                    week_offset=week_offset,
                    ai_prompt=ai_prompt,
                    overwrite=True,
                    max_calories=max_calories,
                    max_total_time=max_total_time,
                )
            except Exception as err:
                _LOGGER.warning("Renouvellement %s échoué : %s", weekday, err)
                failures[weekday] = str(err)
                continue
            meal = self.plan.get(day.isoformat())
            if meal and meal.get("name"):
                planned[weekday] = meal["name"]
            else:
                failures[weekday] = "aucune suggestion"

        await self.async_save()
        _LOGGER.info(
            "Semaine renouvelée (offset %d) : %d vidés, %d planifiés, %d échecs",
            week_offset, cleared, len(planned), len(failures),
        )
        return {"cleared": cleared, "planned": len(planned), "days": planned, "failures": failures}

    async def async_purge_old(self, keep_days: int = 30) -> None:
        """Supprime les repas trop anciens et persiste le résultat."""
        limit = (date.today() - timedelta(days=keep_days)).isoformat()
        old = [k for k in self.plan if k < limit]
        for key in old:
            self.plan.pop(key, None)
        if old:
            await self.async_save()

    # ------------------------------------------------------------------
    # Liste de courses
    # ------------------------------------------------------------------
    def aggregate_ingredients(self, week_offset: int = 0) -> list[str]:
        """Additionne les ingrédients de la semaine (même nom + même unité)."""
        totals: dict[tuple[str, str], float | None] = {}
        order: list[tuple[str, str]] = []

        for day in self.week_dates(week_offset):
            meal = self.get_meal(day)
            if not meal:
                continue
            for ing in meal.get("ingredients", []):
                if ing.get("optional"):
                    continue
                key = (ing["name"].strip().lower(), ing.get("unit", ""))
                if key not in totals:
                    totals[key] = None
                    order.append(key)
                qty = ing.get("quantity")
                try:
                    qty = float(qty)
                except (TypeError, ValueError):
                    continue
                totals[key] = (totals[key] or 0) + qty

        lines = []
        for name, unit in order:
            qty = totals[(name, unit)]
            if qty is None:
                lines.append(name.capitalize())
            else:
                qty_str = f"{qty:g}"
                lines.append(f"{qty_str} {unit} {name}".replace("  ", " ").strip())
        # Trier par rayon si activé
        return self._sort_by_aisle(lines)

    async def async_get_jow_shopping_ingredients(self) -> list[dict]:
        """Ingrédients agrégés de la liste de courses Jow (GET /shoppinglist).

        Contrairement à letscook, cette route expose les quantités
        agrégées du menu Jow (y compris conversions d'unités
        pré-calculées). API publique pour les évolutions de liste de
        courses (aucun consommateur interne aujourd'hui).
        """
        resp = await self._async_jow_get(
            JOW_SHOPPING_URL, params={"availabilityZoneId": "FR"}
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json().get("data", {})
        except ValueError:
            return []
        out = []
        for ing in data.get("recipeIngredients", []) or []:
            if not isinstance(ing, dict):
                continue
            item = ing.get("item") or ing.get("ingredient") or {}
            name = (item.get("name") or "").strip()
            if not name:
                continue
            qty = ing.get("naturalUnitAmount")
            unit = (item.get("naturalUnit") or {}).get("name", "") if isinstance(item.get("naturalUnit"), dict) else ""
            summary = f"{qty} {unit} {name}".strip() if qty else name
            out.append({"name": name, "quantity": qty, "unit": unit, "summary": summary})
        return out

    async def async_refresh_shopping_list(
        self, week_offset: int = 0, keep_checked: bool = False
    ) -> None:
        """Régénère la liste de courses à partir du planning.

        Fusionne les ingrédients agrégés du planning avec la liste approuvée
        (articles à toujours acheter, hors planning) en dédoublonnant sur le
        libellé normalisé. Les articles ajoutés manuellement via l'UI todo
        (marqués "manual") sont conservés : la régénération ne doit jamais
        effacer ce que l'utilisateur a ajouté lui-même.
        """
        done = {item["summary"] for item in self.shopping if item.get("done")} if keep_checked else set()
        auto_lines = self.aggregate_ingredients(week_offset)

        # Articles approuvés : on conserve leur ordre, on les dédoublonne
        # avec les ingrédients du planning via libellé normalisé.
        seen = {self._norm(line) for line in auto_lines}
        merged: list[dict] = []
        for line in auto_lines:
            merged.append({"uid": uuid.uuid4().hex, "summary": line, "done": line in done})
        for item in self.approved:
            summary = item.get("summary", "")
            if not summary.strip():
                continue
            if self._norm(summary) not in seen:
                seen.add(self._norm(summary))
                merged.append(
                    {"uid": uuid.uuid4().hex, "summary": summary, "done": summary in done}
                )
        # Articles ajoutés manuellement depuis l'UI todo : préservés,
        # dédoublonnés, et replacés en fin de liste (ordre d'ajout conservé).
        for item in self.shopping:
            if not item.get("manual"):
                continue
            summary = item.get("summary", "")
            if not summary.strip():
                continue
            if self._norm(summary) not in seen:
                seen.add(self._norm(summary))
                merged.append(
                    {
                        "uid": item.get("uid") or uuid.uuid4().hex,
                        "summary": summary,
                        "done": item.get("done", False),
                        "manual": True,
                    }
                )
        self.shopping = merged
        await self.async_save()

    def _ingredient_name_from_summary(self, summary: str) -> str:
        """Nom d'ingrédient depuis un summary de liste (« 200 g de riz » → « riz »).

        Unifie l'extraction historique dupliquée entre meal_done et
        exclude_ingredient : retrait de la quantité/unité de tête, puis
        split sur « de » si présent.
        """
        n = self._norm(summary)
        if " de " in n:
            return n.split(" de ", 1)[1]
        return self._strip_quantity(n)

    def _recent_plan_ids_and_names(self, weeks: int = 4) -> tuple[set, list[str]]:
        """Ids planifiés (fenêtre N semaines) + noms, unifié.

        Remplace les 5 recalculs locaux de la fenêtre « 4 dernières
        semaines » dispersés dans le code.
        """
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        ids: set = set()
        names: list[str] = []
        for day_iso, meal in self.plan.items():
            if isinstance(meal, dict) and day_iso >= cutoff:
                if meal.get("id"):
                    ids.add(meal["id"])
                if meal.get("name") and not meal.get("_no_exclude"):
                    names.append(meal["name"])
        return ids, names

    @staticmethod
    def _norm(text: str) -> str:
        """Normalise un libellé pour comparer (minuscules, espaces)."""
        return " ".join(text.lower().split())

    _QTY_PREFIX = re.compile(
        r"^\d+(?:[.,]\d+)?\s*(?:[a-zA-Zéèêëàâäîïôöûüç%.]+(?:\s+de)?\s+)?"
    )

    @classmethod
    def _strip_quantity(cls, text: str) -> str:
        """Retire la quantité/Unité de tête d'un libellé normalisé.

        "200 g riz" -> "riz", "1,5 l de lait" -> "lait", "riz" -> "riz".
        """
        return cls._QTY_PREFIX.sub("", text).strip() or text

    _STOP_WORDS_QUERY = {
        "recette", "recipe", "plat", "repas", "diner", "dîner", "souper",
        "facile", "rapide", "simple", "bon", "bonne", "avec", "sans",
        "pour", "moi", "un", "une", "des", "de", "du", "la", "le", "les",
        "a", "au", "aux", "et", "en", "me", "propose", "veux", "voudrais",
    }

    @classmethod
    def _query_keywords(cls, query: str) -> list[str]:
        """Mots-clés signifiants d'une requête (sans mots vides, >= 3 lettres)."""
        words = re.findall(r"[a-zàâäéèêëîïôöùûüçœ]+", query.lower())
        return [w for w in words if len(w) >= 3 and w not in cls._STOP_WORDS_QUERY]

    @classmethod
    def _title_keywords(cls, title: str) -> set[str]:
        """Mots-clés d'un titre de recette, génériques exclus.

        « Curry de lentilles corail » -> {curry, lentilles, corail}.
        Utilisé pour la diversité : deux plats partageant un mot-clé
        fort sont considérés comme proches.
        """
        words = re.findall(r"[a-zàâäéèêëîïôöùûüçœ]+", (title or "").lower())
        return {w for w in words if len(w) >= 4 and w not in _GENERIC_WORDS}

    @classmethod
    def _too_similar(cls, candidate_name: str, recent_names: list[str]) -> str | None:
        """Retourne le mot-clé fort partagé si la candidate ressemble trop
        à un plat récent (même curry, risotto, tajine…), sinon None."""
        cand = cls._title_keywords(candidate_name)
        if not cand:
            return None
        for recent in recent_names:
            shared = cand & cls._title_keywords(recent)
            if shared:
                return shared.pop()
        return None

    @staticmethod
    def _shelf_life(ingredient_name: str) -> int | None:
        """Durée de conservation (jours) d'un ingrédient, par mot-clé.

        Heuristique : premier mot-clé connu du _SHELF_LIFE_DAYS trouvé dans
        le nom (inclusion). None = péremption longue/inconnue (épicerie).
        """
        n = " ".join((ingredient_name or "").lower().split())
        if not n:
            return None
        # clés les plus longues d'abord (« saumon fumé » > « saumon »
        # n'existe pas encore, mais l'ordre est robuste si ajouté)
        for key in sorted(_SHELF_LIFE_DAYS, key=len, reverse=True):
            if key in n:
                return _SHELF_LIFE_DAYS[key]
        return None

    def expiring_ingredients(self, within_days: int = 3, today: date | None = None) -> list[dict]:
        """Ingrédients du planning qui expirent bientôt (rescue mode).

        Construit l'inventaire « ce que la maison a/aura » depuis les
        ingrédients des repas planifiés (aujourd'hui inclus en arrière),
        leur associe une date limite par _shelf_life, et retourne ceux
        qui expirent dans les `within_days` prochains jours — triés par
        urgence. Le plat qui les contient déjà est le consommateur idéal.
        """
        today = today or date.today()
        horizon = today + timedelta(days=within_days)
        # inventaire: ingredient -> (expiry_date, source_meal_name)
        inventory: dict[str, tuple[date, str]] = {}
        # les repas passés ET à venir portent des ingrédients achetés/à acheter ;
        # on considère les 3 jours passés (achats récents) et tous les futurs.
        for day_iso, meal in self.plan.items():
            try:
                d = date.fromisoformat(day_iso)
            except ValueError:
                continue
            if not isinstance(meal, dict):
                continue
            # date d'« entrée en cuisine » : le jour du repas, mais pour
            # les repas passés on prend aujourd'hui (les restes sont là)
            entry = max(d, today) if d >= today - timedelta(days=3) else None
            if entry is None:
                continue
            for ing in meal.get("ingredients", []):
                name = (ing.get("name") or "").strip().lower()
                if not name:
                    continue
                life = self._shelf_life(name)
                if life is None:
                    continue
                expiry = entry + timedelta(days=life)
                if expiry <= today:
                    continue  # déjà périmé : trop tard pour le rescue
                prev = inventory.get(name)
                if prev is None or expiry < prev[0]:
                    inventory[name] = (expiry, meal.get("name", ""))
        soon = [
            {"ingredient": name, "expires": expiry.isoformat(), "days_left": (expiry - today).days, "meal": source}
            for name, (expiry, source) in inventory.items()
            if expiry <= horizon
        ]
        return sorted(soon, key=lambda x: x["days_left"])

    def _recent_families(self) -> list[str]:
        """Mots-clés forts communs aux plats récents (planifiés 4 semaines
        + rejets), triés par fréquence — la famille dominante d'abord.
        Ex: deux dahls récents -> ['dahl', …]. Sert à orienter la requête
        IA loin de ces familles."""
        _, names = self._recent_plan_ids_and_names(weeks=4)
        names += [r.get("name", "") for r in self.rejected[:_SIMILAR_WINDOW]]
        freq: dict[str, int] = {}
        for n in names:
            for kw in self._title_keywords(n):
                freq[kw] = freq.get(kw, 0) + 1
        # Familles présentes chez au moins un plat récent ; les plus
        # fréquentes d'abord (elles caractérisent la répétition perçue).
        return [kw for kw, _ in sorted(freq.items(), key=lambda kv: -kv[1])]

    @classmethod
    def _diversify_intra_list(cls, recipes: list[dict], query: str = "") -> list[dict]:
        """Une seule recette par famille (mot-clé fort) dans la liste :

        « Dahl de lentilles » et « Dahl aux épinards » ne doivent pas
        sortir ensemble dans les suggestions — l'utilisateur en choisit
        une, l'autre est du bruit de même goût. La première de chaque
        famille est conservée (ordre du pipeline), les suivantes sautées.

        EXCEPTION : les mots-clés de la REQUÊTE ne comptent pas comme
        familles — si l'utilisateur demande des burgers, toute la liste
        peut être de burgers ; c'est la diversité par rapport à ses plats
        récents (gérée en amont) qui compte, pas celle qu'il a demandée.
        """
        query_kw = set(cls._query_keywords(query)) if query else set()
        seen_families: set[str] = set()
        out = []
        for r in recipes:
            kws = cls._title_keywords(r.get("name", "")) - query_kw
            fam = next((k for k in kws if k in seen_families), None)
            if fam:
                continue
            seen_families.update(kws)
            out.append(r)
        return out

    @classmethod
    def _rerank_on_query(cls, recipes: list[dict], query: str) -> list[dict]:
        """Re-trie les recettes selon la correspondance titre/description
        avec les mots-clés de la requête.

        quicksearch (Jow) fait une recherche en OU logique : « burger
        asiatique » peut retourner « Burger au poulet à la mexicaine »
        en tête parce que « burger » matche. Ici, une recette dont le
        titre contient tous les mots-clés passe devant une recette qui
        n'en contient qu'un. Égalité de score : ordre API conservé
        (son ranking reste le tie-breaker).
        """
        keywords = cls._query_keywords(query)
        if not keywords or not recipes:
            return recipes

        def _score(recipe: dict) -> tuple[int, int]:
            title = cls._norm(recipe.get("name", ""))
            desc = cls._norm(recipe.get("description") or "")
            title_hits = sum(1 for k in keywords if k in title)
            desc_hits = sum(1 for k in keywords if k in desc)
            # Le titre pèse 10x plus que la description ; un seul mot-clé
            # dans le titre vaut mieux que plusieurs en description.
            return (title_hits, desc_hits)

        # Tri stable : score décroissant, ordre d'origine à égalité.
        return sorted(recipes, key=_score, reverse=True)

    def _sort_by_aisle(self, lines: list[str]) -> list[str]:
        """Trie la liste de courses par rayon (Fruits & Légumes, Boucherie, etc.)."""
        return sorted(lines, key=lambda ligne: (_AISLE_ORDER.index(_aisle_for(ligne)), ligne.lower()))

    async def async_add_item(self, summary: str) -> None:
        if len(self.shopping) >= _MAX_ITEMS:
            _LOGGER.warning("Liste de courses Jow pleine (%d items) : ajout ignoré", _MAX_ITEMS)
            return
        clean = (summary or "")[:_MAX_SUMMARY_LEN]
        if not clean.strip():
            return
        # "manual" : ajouté par l'utilisateur via l'UI todo — conservé
        # lors des régénérations de la liste.
        self.shopping.append(
            {"uid": uuid.uuid4().hex, "summary": clean, "done": False, "manual": True}
        )
        await self.async_save()

    async def async_update_item(self, uid: str, summary: str | None, done: bool | None) -> None:
        for item in self.shopping:
            if item["uid"] == uid:
                if summary is not None:
                    item["summary"] = summary[:_MAX_SUMMARY_LEN]
                if done is not None:
                    item["done"] = done
        await self.async_save()

    async def async_remove_items(self, uids: list[str]) -> None:
        to_remove = set(uids)
        self.shopping = [i for i in self.shopping if i["uid"] not in to_remove]
        await self.async_save()

    # ------------------------------------------------------------------
    # Liste approuvée (articles hors planning, fusionnés systématiquement)
    # ------------------------------------------------------------------
    async def async_add_approved(self, summary: str) -> None:
        if len(self.approved) >= _MAX_ITEMS:
            _LOGGER.warning("Liste approuvée Jow pleine (%d items) : ajout ignoré", _MAX_ITEMS)
            return
        clean = (summary or "")[:_MAX_SUMMARY_LEN]
        if not clean.strip():
            return
        self.approved.append({"uid": uuid.uuid4().hex, "summary": clean, "done": False})
        await self.async_save()

    async def async_update_approved(self, uid: str, summary: str | None, done: bool | None) -> None:
        for item in self.approved:
            if item["uid"] == uid:
                if summary is not None:
                    item["summary"] = summary[:_MAX_SUMMARY_LEN]
                if done is not None:
                    item["done"] = done
        await self.async_save()

    async def async_remove_approved(self, uids: list[str]) -> None:
        to_remove = set(uids)
        self.approved = [i for i in self.approved if i["uid"] not in to_remove]
        await self.async_save()

    # ------------------------------------------------------------------
    # Marquer un repas comme fait + retirer les ingrédients du stock
    # ------------------------------------------------------------------
    async def _async_get_letscook(self) -> dict:
        """Lecture unifiée de /profile/letscook (route stable du site).

        Centralise les 3 lectures historiques (mark_cooked, send_menu,
        import_menu) ; met à jour le cache jow_open_meals. Retourne {}
        en cas d'échec (jamais d'exception).
        """
        resp = await self._async_jow_get(
            "https://api.jow.fr/public/profile/letscook",
            params={"availabilityZoneId": "FR", "nbMeals": 40},
        )
        if resp is None or resp.status_code != 200:
            return {}
        try:
            data = resp.json().get("data", {}) or {}
        except ValueError:
            return {}
        osl = data.get("openShoppingList") or {}
        if isinstance(osl, dict):
            meals = [m for m in (osl.get("meals") or []) if isinstance(m, dict)]
            if meals:
                self.jow_open_meals = meals  # cache pour le capteur d'état
        return data

    async def _async_rewrite_open_list(self, body_meals: list[dict]) -> int:
        """Réécriture unifiée de la liste ouverte (POST /shoppinglist/open).

        Retourne le code HTTP (0 si impossible sans token). Le caller DOIT
        avoir mergé l'existant avant — garantie anti-perte documentée.
        """
        if not self.jow_token:
            return 0

        def _post():
            headers = self._jow_auth_headers()
            headers["content-type"] = "application/json"
            headers["accept"] = "application/json, text/plain, */*"
            return requests.post(
                "https://api.jow.fr/public/shoppinglist/open",
                headers=headers,
                params={
                    "populateRecipes": "true",
                    "populateIngredients": "true",
                    "availabilityZoneId": "FR",
                },
                data=json.dumps({"meals": body_meals}),
                timeout=20,
            )

        resp = await self.hass.async_add_executor_job(_post)
        # le cache suit la liste réécrite si la réponse porte les repas
        if resp.status_code in (200, 201, 204):
            try:
                rewritten = [m for m in (resp.json().get("meals") or []) if isinstance(m, dict)]
                if rewritten:
                    self.jow_open_meals = rewritten
            except ValueError:
                pass
        return resp.status_code

    async def _async_mark_cooked_on_jow(self, recipe_id: str) -> None:
        """Marque une recette comme cuisinée dans le menu Jow (best effort).

        Mécanisme observé : la réécriture de la liste ouverte avec
        isCooked: true retire le plat de la liste active côté site —
        c'est le « repas fait » de jow.fr. Échec silencieux (l'état HA
        reste la référence ; la synchro Jow est un confort).
        """
        if not self.jow_token or not recipe_id:
            return
        try:
            data = await self._async_get_letscook()
            osl = data.get("openShoppingList") or {}
            meals = [m for m in (osl.get("meals") or []) if isinstance(m, dict)]
            if not any(
                _safe_id((m.get("recipe") or {}).get("id") or (m.get("recipe") or {}).get("_id")) == recipe_id
                for m in meals
            ):
                return  # pas dans la liste Jow : rien à marquer

            # réécrire : le plat cible avec isCooked, les autres inchangés
            body_meals = []
            for m in meals:
                r = m.get("recipe") or {}
                rid = _safe_id(r.get("id") or r.get("_id"))
                if not rid:
                    continue
                body_meals.append({
                    "recipe": rid,
                    "coversCount": m.get("coversCount") or self.default_covers,
                    "source": m.get("source") or "jow",
                    **({"isCooked": True} if rid == recipe_id else {}),
                })

            status = await self._async_rewrite_open_list(body_meals)
            if status in (200, 201, 204):
                _LOGGER.info("Recette %s marquée cuisinée sur jow.fr", recipe_id)
            else:
                _LOGGER.debug(
                    "Marquage isCooked refusé (HTTP %s) — sans conséquence", status
                )
        except Exception as err:
            _LOGGER.debug("Marquage isCooked impossible : %s", err)

    async def async_meal_done(self, day: date) -> dict | None:
        """Marque un repas comme fait et retire les ingrédients de la liste de courses.

        - Supprime le repas du planning
        - Régénère la liste de courses sans les ingrédients de ce repas
        - Conserve les items déjà cochés et les items approuvés
        """
        meal = self.get_meal(day)
        if not meal:
            _LOGGER.warning("Aucun repas planifié pour %s", day.isoformat())
            return None

        # Refléter le « repas fait » sur jow.fr (best effort, silencieux) :
        # la recette est marquée isCooked dans la liste ouverte du compte.
        if meal.get("id"):
            await self._async_mark_cooked_on_jow(meal["id"])

        # Ingrédients du repas terminé (noms normalisés)
        done_ingredients = {
            self._norm(ing.get("name", ""))
            for ing in meal.get("ingredients", [])
            if not ing.get("optional")
        }

        # Retirer les items de la liste de courses correspondant aux ingrédients
        # du repas terminé. Le summary de shopping est au format "200 g riz"
        # (ou "200 g de riz" / "3 botte de basilic") : on extrait le nom de
        # l'ingrédient en retirant la quantité et l'unité de tête, puis on
        # compare normalisé pour éviter les faux positifs (ex: "ail" dans
        # "aileron").
        removed = []
        kept = []
        for item in self.shopping:
            summary_norm = self._norm(item["summary"])
            # Extraction du nom d'ingrédient : "200 g de riz" -> "riz",
            # "200 g riz" -> "riz", "riz" -> "riz"
            item_name = self._ingredient_name_from_summary(item["summary"])
            if item_name in done_ingredients or summary_norm in done_ingredients:
                removed.append(item["summary"])
            else:
                kept.append(item)
        self.shopping = kept

        # Retirer le repas du planning. meal_done ne passe PAS par
        # _remember_rejected : le plat a été mangé (donc apprécié a
        # priori) — l'anti-répétition standard (4 semaines) suffit.
        self.plan.pop(day.isoformat(), None)
        await self.async_save()

        _LOGGER.info(
            "Repas '%s' marqué comme fait pour %s — %d ingrédients retirés de la liste",
            meal.get("name", ""),
            day.isoformat(),
            len(removed),
        )

        return {
            "meal": meal.get("name", ""),
            "date": day.isoformat(),
            "removed_from_shopping": removed,
        }

    # ------------------------------------------------------------------
    # Suggestion IA (ai_task.generate_data + jow.search)
    # ------------------------------------------------------------------
    async def _ai_generate(self, instructions: str, ai_ent: str, task_name: str = "jow_recipe_suggest") -> str:
        """Appelle ai_task.generate_data et retourne la réponse texte.

        Timeout explicite : un agent IA qui pend ne doit pas bloquer le
        service (et l'automatisation appelante) indéfiniment —
        blocking=True sans limite attendrait la réponse pour toujours.
        Retourne "" en cas d'échec (l'appelant retombe sur ses pieds).
        """
        try:
            response = await asyncio.wait_for(
                self.hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {
                        "task_name": task_name,
                        "instructions": instructions,
                        "entity_id": ai_ent,
                    },
                    blocking=True,
                    return_response=True,
                ),
                timeout=60,
            )
            # Selon la version HA, response peut être:
            # {"conversation_id": ..., "data": "..."}  (via WS)
            # ou {"ai_task.xxx": {"data": "..."}}  (via async_call interne)
            data = ""
            if isinstance(response, dict):
                data = response.get("data")
                if not data:
                    data = response.get("response", {}).get("data", "")
                if not data:
                    for _k, val in response.items():
                        if isinstance(val, dict) and "data" in val:
                            data = val["data"]
                            break
            elif isinstance(response, str):
                data = response
            return str(data or "").strip().strip('"').strip("'")
        except Exception as err:
            _LOGGER.warning("ai_task.generate_data a échoué : %s", err)
            return ""

    async def _ai_pick_recipe(
        self,
        criteria: str,
        recipes: list[dict],
        ai_ent: str,
        recent_names: list[str] | None = None,
        max_calories: int | None = None,
    ) -> dict | None:
        """Demande à l'agent IA de choisir la recette la plus adaptée.

        L'IA reçoit la demande utilisateur et la liste (titre, description,
        ingrédients principaux, temps, calories — max 30) des recettes
        filtrées, ainsi que les plats récents à varier, et retourne le
        numéro de la meilleure. Retourne None en cas d'échec (l'appelant
        garde l'ordre du re-ranking).
        """
        if not recipes or not ai_ent:
            return None
        # 30 max : au-delà la liste devient bruitée pour l'agent et le
        # prompt explose en tokens.
        candidates = recipes[:30]

        def _describe(r: dict) -> str:
            """Une ligne informative et compacte par recette."""
            parts = []
            if r.get("description"):
                parts.append(r.get("description", "")[:90])
            ings = [i.get("name", "") for i in r.get("ingredients", [])][:6]
            if ings:
                parts.append("ingr. : " + ", ".join(ings))
            times = []
            if r.get("preparation_time"):
                times.append(f"prép. {r.get('preparation_time')} min")
            if r.get("cooking_time"):
                times.append(f"cuisson {r.get('cooking_time')} min")
            if times:
                parts.append(" · ".join(times))
            if r.get("calories"):
                parts.append(f"{r.get('calories')} kcal/pers")
            return f"{r.get('name', '')}" + (f" ({' — '.join(parts)})" if parts else "")

        listing = "\n".join(f"{i + 1}. {_describe(r)}" for i, r in enumerate(candidates))
        recent = ""
        if recent_names:
            recent = (
                f"Plats déjà mangés récemment (évite de les reproposer si "
                f"possible) : {', '.join(recent_names[:8])}.\n"
            )
        cal_constraint = ""
        if max_calories is not None:
            cal_constraint = (
                f"CONTRAINTE IMPÉRATIVE : la recette choisie doit faire au "
                f"plus {max_calories} kcal par portion (les kcal sont "
                f"indiquées quand elles sont connues) ; à défaut d'info, "
                f"privilégie les plats légers.\n"
            )
        instructions = (
            f"Un utilisateur demande : « {criteria or 'un bon repas'} ». "
            f"{f'Préférences : {self.preferences}. ' if self.preferences else ''}"
            f"{recent}{cal_constraint}"
            "Voici les recettes disponibles :\n"
            f"{listing}\n\n"
            "Choisis LA recette qui correspond le mieux à la demande "
            "(style de cuisine, ingrédients, type de plat, temps dispo — "
            "un plat approchant vaut mieux qu'un plat hors-sujet, même "
            "parfait ; varie par rapport aux plats récents si tu as le "
            "choix). "
            "Réponds uniquement avec le numéro de la recette."
        )
        answer = await self._ai_generate(instructions, ai_ent, task_name="jow_recipe_pick")
        if not answer:
            return None
        match = re.search(r"\d+", answer)
        if not match:
            return None
        idx = int(match.group()) - 1
        if 0 <= idx < len(candidates):
            picked = candidates[idx]
            _LOGGER.info(
                "Sélection IA : « %s » (n°%d) pour la demande « %s »",
                picked.get("name", ""),
                idx + 1,
                criteria,
            )
            return picked
        _LOGGER.warning("Sélection IA : numéro invalide « %s », ordre conservé", answer)
        return None

    async def async_jow_recommendations(self, count: int = 10, exclude_ids: list[str] | None = None) -> list[dict]:
        """Recommandations natives du moteur Jow (/recipes/reco/more).

        Le site suggère lui-même des recettes personnalisées (userProfile :
        habitudes, goûts exclus) — utilisable comme fallback sans agent IA
        (zéro latence ai_task) ou comme source alternative de diversité.
        excludedRecipesIds reçoit plats récents + rejets pour respecter
        l'anti-répétition côté serveur.
        """
        recent_ids, _ = self._recent_plan_ids_and_names(weeks=4)
        excluded: list[str] = list(exclude_ids or []) + list(recent_ids)
        excluded.extend(r.get("id") for r in self.rejected if r.get("id"))

        body: dict = {
            "context": "cookbook-menu",
            "excludedRecipesIds": excluded[:50],
            "count": count,
        }
        # userProfile : les habitudes/goûts du compte synchronisent mieux
        # les reco que rien du tout
        if self.preferences or self.banned_ingredients:
            habits: dict = {}
            for key, label in (
                ("vegetarian", "végétarien"), ("vegan", "végétalien"),
                ("pescatarian", "pescétarien"), ("glutenFree", "sans gluten"),
                ("dairyFree", "sans lactose"), ("porkless", "sans porc"),
            ):
                if label in (self.preferences or ""):
                    habits[key] = True
            if habits:
                body["userProfile"] = {"eatingHabits": habits}

        def _post():
            headers = self._jow_auth_headers()
            headers["content-type"] = "application/json"
            headers["accept"] = "application/json, text/plain, */*"
            return requests.post(
                "https://api.jow.fr/public/recipes/reco/more",
                headers=headers,
                params={"availabilityZoneId": "FR", "count": str(count)},
                data=json.dumps(body),
                timeout=20,
            )

        if not self.jow_token:
            return []
        resp = await self.hass.async_add_executor_job(_post)
        if resp.status_code != 200:
            _LOGGER.warning("Recommandations Jow indisponibles (HTTP %s)", resp.status_code)
            return []
        try:
            data = resp.json().get("data", [])
        except ValueError:
            return []
        return [_recipe_to_dict(r, self.default_covers) for r in data if isinstance(r, dict)]

    async def async_suggest(
        self,
        criteria: str = "",
        covers: int | None = None,
        limit: int = 5,
        weather_entity: str | None = None,
        ai_entity: str | None = None,
        weekday: str | None = None,
        week_offset: int = 0,
        ai_prompt: str = "",
        overwrite: bool = True,
        max_calories: int | None = None,
        max_total_time: int | None = None,
        rescue_expiry: bool = False,
    ) -> list[dict]:
        """Génère une requête Jow via l'IA puis cherche les recettes.

        Utilise l'agent ai_task configuré (ou celui passé en paramètre) pour
        formuler une requête de recherche adaptée aux allergies/préférences
        de l'utilisateur et à la météo courante, puis interroge l'API Jow.
        """
        ai_ent = ai_entity or self.ai_entity
        weather_ent = weather_entity or self.weather_entity

        # Contexte météo
        weather_ctx = ""
        if weather_ent:
            state = self.hass.states.get(weather_ent)
            if state and state.state not in (None, "unknown", "unavailable"):
                temp = state.attributes.get("temperature", "?")
                weather_ctx = f"Météo actuelle : {state.state}, {temp}°C. "

        # Mode rescue : les ingrédients périssables du planning qui
        # expirent sous peu sont injectés en PRIORITÉ dans le contexte —
        # l'IA doit générer une requête qui les écoule avant la date
        # limite (le gaspillage coûte plus cher que la variété).
        rescue_ctx = ""
        if rescue_expiry:
            expiring = self.expiring_ingredients(within_days=3)
            if expiring:
                parts = [f"{e['ingredient']} (expire dans {e['days_left']}j)" for e in expiring[:6]]
                rescue_ctx = (
                    "INGRÉDIENTS À SAUVER EN PRIORITÉ (périssables bientôt "
                    f"expirés) : {', '.join(parts)}. La recette doit en "
                    "utiliser au moins un si possible. "
                )
                _LOGGER.info("Mode rescue : %d ingrédients expirants injectés", len(expiring))

        # Contraintes utilisateur
        constraints = ""
        if self.allergies:
            constraints += f"Allergies/interdits : {self.allergies}. "
        if self.preferences:
            constraints += f"Préférences : {self.preferences}. "
        if self.banned_ingredients:
            constraints += f"Ingrédients interdits : {', '.join(self.banned_ingredients)}. "
        if self.avoid_ingredients:
            constraints += f"Ingrédients à éviter si possible : {', '.join(self.avoid_ingredients)}. "
        if criteria:
            constraints += f"Demande : {criteria}. "

        # Recettes récentes à éviter pour la diversité (helper unifié)
        _recent_ids, recent_names = self._recent_plan_ids_and_names(weeks=4)
        # Inclure les rejets (effacés sans être mangés) dans la fenêtre
        # de variation : l'IA ne doit pas générer une requête « dahl »
        # juste après que l'utilisateur a refusé deux dahls.
        recent_names.extend(r.get("name", "") for r in self.rejected[:_SIMILAR_WINDOW] if r.get("name"))
        if recent_names:
            constraints += f"Évite ces plats déjà faits récemment : {', '.join(recent_names[:8])}. Propose quelque chose de différent. "
        # Familles récentes explicites : plus fort que la liste de plats,
        # l'IA comprend qu'elle ne doit même pas chercher dans cette
        # famille (sinon tout le résultat Jow est de la même famille et
        # la diversité en aval n'a plus de marge de manœuvre).
        familles = self._recent_families()
        if familles:
            constraints += (
                f"Varie vraiment : pas de recette de type {', '.join(familles[:6])} "
                "ni de plat trop proche. "
            )

        instructions = (
            f"{weather_ctx}{rescue_ctx}{constraints}"
            "Génère une requête de recherche de recette courte (2 à 5 mots, "
            "sans guillemets ni ponctuation) adaptée au contexte. "
            "Il s'agit d'un repas (plat principal, entrée ou dessert) — "
            "JAMAIS de boisson, cocktail ou apéritif. "
            "Varie le style de cuisine et le type de plat. "
            "Réponds uniquement avec la requête."
        )
        # Prompt IA personnalisé depuis la config de la carte
        if ai_prompt:
            instructions = (
                f"{weather_ctx}{rescue_ctx}{constraints}"
                f"{ai_prompt} "
                "Il s'agit d'un repas (plat principal, entrée ou dessert) — "
                "JAMAIS de boisson, cocktail ou apéritif. "
                "Réponds uniquement avec la requête de recherche."
            )

        # Appel ai_task.generate_data
        query = ""
        criteria_words = (criteria or "").strip().split()
        is_long_phrase = len(criteria_words) > 5

        # Si phrase longue, l'IA raisonne pour proposer une requête pertinente
        if is_long_phrase and ai_ent:
            instructions = (
                f"{weather_ctx}{rescue_ctx}{constraints}"
                f"Un utilisateur demande : « {criteria} ». "
                "Analyse cette demande et génère la meilleure requête de "
                "recherche de recette (2 à 5 mots) pour trouver ce que "
                "l'utilisateur veut vraiment. "
                "Réfléchis : si l'utilisateur dit 'plat rapide avec du poulet "
                "et des pâtes', propose 'pâtes poulet'. "
                "Si l'utilisateur dit 'repas léger pour ce soir', propose "
                "'salade composée' ou 'soupe légumes'. "
                "Il s'agit d'un repas (plat principal, entrée ou dessert) — "
                "JAMAIS de boisson, cocktail ou apéritif. "
                "Réponds uniquement avec la requête de recherche."
            )

        if ai_ent:
            query = await self._ai_generate(instructions, ai_ent)
            if not query:
                _LOGGER.warning("ai_task.generate_data a échoué (réponse vide)")

        # Fallback : utiliser criteria directement ou extraire les mots-clés
        # (_query_keywords : LA liste de mots vides, avant deux listes
        # divergentes coexistaient ici et dans le re-ranking)
        if not query:
            if is_long_phrase:
                keywords = self._query_keywords(criteria or "")[:4]
                query = " ".join(keywords) if keywords else "recette"
            else:
                query = criteria or "recette"

        _LOGGER.info("Requête Jow suggérée par l'IA : %s", query)
        # quicksearch plafonne limit à 50 par page mais pagine via start :
        # on récupère deux pages (100 résultats) pour donner de la
        # profondeur au re-ranking et aux filtres (interdits, répétitions).
        results = await self.async_search(query, limit=50)
        if len(results) == 50:
            second = await self.async_search(query, limit=50, start=50)
            # dédoublonnage id OU _id (l'API expose les deux selon les
            # endpoints — ne comparer que "id" écartait toute la page 2
            # si elle ne portait que _id)
            seen_ids = {
                _safe_id(x.get("id") or x.get("_id")) for x in results
            } - {None}
            results.extend(
                r for r in second
                if _safe_id(r.get("id") or r.get("_id")) not in seen_ids
            )
        # Repli natif : si la recherche textuelle ne trouve RIEN, le moteur
        # de recommandation Jow (/recipes/reco/more) propose des recettes
        # personnalisées — mieux qu'une liste vide, sans agent IA.
        if not results:
            _LOGGER.info("Recherche vide pour « %s » — repli sur les recommandations natives Jow", query)
            native = await self.async_jow_recommendations(count=max(limit, 10))
            results = native
        covers = covers or self.default_covers
        recipes = [_recipe_to_dict(r, covers) for r in results]

        # Re-ranking sémantique léger : quicksearch fait une recherche en
        # OU logique sur les tokens (« burger asiatique » peut retourner
        # « Burger au poulet à la mexicaine » en première position parce
        # que « burger » matche). On rescore chaque recette sur la
        # présence des mots-clés de la requête dans son titre, pour que
        # le premier résultat corresponde réellement à la demande.
        recipes = self._rerank_on_query(recipes, query)

        # Exclure les recettes déjà planifiées dans les 4 dernières semaines
        # (au lieu de tout l'historique) pour éviter les répétitions récentes
        # tout en laissant revenir les plats après un mois.
        deja_planifies, _ = self._recent_plan_ids_and_names(weeks=4)

        # Exclure les plats REJETÉS (effacés sans être marqués faits) :
        # même absents du planning, ils ne doivent pas revenir pendant
        # REJECT_MEMORY_DAYS — sinon la suggestion suivante les reproposait.
        rejected_ids = {r["id"] for r in self.rejected}
        excluded_ids = deja_planifies | rejected_ids
        if excluded_ids:
            avant = len(recipes)
            recipes = [r for r in recipes if r.get("id") not in excluded_ids]
            _LOGGER.info(
                "Recettes dédupliquées : %d exclues (%d rejets), %d restantes",
                avant - len(recipes),
                len(rejected_ids & {r.get("id") for r in recipes}) if recipes else 0,
                len(recipes),
            )

        # Diversité du CHOIX : au-delà des ids, écarter les plats trop
        # similaires aux derniers planifiés/rejetés — deux plats partageant
        # un mot-clé fort (curry, risotto, tajine, lasagnes…) sont perçus
        # comme « le même plat » par l'utilisateur. La similarité n'est
        # appliquée qu'à la marge (si des candidates différentes restent),
        # jamais au point de vider la liste.
        if recipes:
            window = [r.get("name", "") for r in self.rejected[:_SIMILAR_WINDOW]]
            window += [
                meal.get("name", "")
                for day_iso, meal in sorted(self.plan.items(), reverse=True)[:_SIMILAR_WINDOW]
                if meal
            ]
            distinct = [r for r in recipes if not self._too_similar(r.get("name", ""), window)]
            if distinct and len(distinct) < len(recipes):
                _LOGGER.info(
                    "Diversité : %d/%d recettes trop proches des plats récents écartées",
                    len(recipes) - len(distinct), len(recipes),
                )
                recipes = distinct

        # Filtrer les recettes contenant des ingrédients interdits
        # (allergies Jow + liste manuelle banned_ingredients) — AVANT le
        # slicing, sinon on retire des recettes de la liste finale au lieu
        # d'aller chercher les suivantes dans les résultats de recherche.
        banned = set()
        if self.jow_token:
            excluded = await self.async_get_excluded_ingredients()
            banned.update(e.lower().strip() for e in excluded if e)
        banned.update(e.lower().strip() for e in self.banned_ingredients if e)
        if banned:
            filtered = []
            for recipe in recipes:
                ings = [i.get("name", "").lower() for i in recipe.get("ingredients", [])]
                if not any(any(b in ing for ing in ings) for b in banned):
                    filtered.append(recipe)
            if filtered and len(filtered) < len(recipes):
                recipes = filtered
                _LOGGER.info("Recettes filtrées (interdits) : %d restantes", len(recipes))

        # Filtre dur sur le temps total : preparationTime/cookingTime sont
        # fournis par le flux de recherche, la contrainte est donc garantie.
        # Si le filtre vide tout (ex: « rapide » sur des résultats longs),
        # on le saute et on loggue plutôt que de renvoyer une liste vide.
        if max_total_time is not None:
            before = len(recipes)
            before_time = recipes

            def _total_time(r: dict) -> int | None:
                prep = r.get("preparation_time") or 0
                cook = r.get("cooking_time") or 0
                return prep + cook if (prep or cook) else None

            recipes = [r for r in recipes if (t := _total_time(r)) is not None and t <= max_total_time]
            if recipes:
                _LOGGER.info(
                    "Filtre temps total ≤ %d min : %d/%d recettes conservées",
                    max_total_time, len(recipes), before,
                )
            else:
                _LOGGER.warning(
                    "Filtre temps total ≤ %d min : aucune recette conforme, filtre ignoré",
                    max_total_time,
                )
                recipes = before_time

        # Les calories ne sont PAS dans le flux de recherche (endpoint
        # détail, une requête par recette — trop lourd pour 100 résultats) :
        # max_calories ne peut pas être un filtre dur ici. Il est passé à
        # l'agent de sélection, qui voit les kcal quand elles sont connues
        # (recettes déjà planifiées/choisies), et appliqué en dur par le
        # seul endroit où la donnée existe : au moment de planifier.

        # Filtrer les recettes contenant des ingrédients à éviter (préférence)
        if self.avoid_ingredients:
            avoid = {e.lower().strip() for e in self.avoid_ingredients if e}
            filtered = []
            for recipe in recipes:
                ings = [i.get("name", "").lower() for i in recipe.get("ingredients", [])]
                if not any(any(a in ing for ing in ings) for a in avoid):
                    filtered.append(recipe)
            # Ne pas filtrer si ça vide tous les résultats
            if filtered and len(filtered) < len(recipes):
                recipes = filtered
                _LOGGER.info("Recettes filtrées (à éviter) : %d restantes", len(recipes))

        # Diversité intra-liste : une seule recette par famille (mot-clé
        # fort) dans ce qui va être renvoyé/planifié — « Dahl de lentilles »
        # et « Dahl aux épinards » ne doivent pas coexister dans la même
        # liste de suggestions. AVANT le slicing pour laisser la place aux
        # familles suivantes dans la profondeur des résultats.
        recipes = self._diversify_intra_list(recipes, query=query)

        # Garder le nombre demandé — APRÈS les filtres, pour piocher dans
        # la profondeur des résultats plutôt que de renvoyer moins que limit.
        recipes = recipes[:limit]

        # Sélection IA : l'agent relit la demande utilisateur et choisit
        # la recette la plus adaptée parmi les candidates filtrées (le
        # re-ranking lexical ne comprend pas que « tofu croustillant
        # sauce siracha » est plus proche d'un burger asiatique demandé
        # qu'un burger mexicain). En cas d'échec IA, ordre conservé.
        # max_calories : les kcal ne sont pas dans le flux de recherche —
        # on le transmet comme contrainte forte à l'agent (il voit les
        # kcal des recettes déjà connues), et le choix final est vérifié
        # en dur après récupération des calories (endpoint détail).
        if ai_ent and len(recipes) > 1 and criteria:
            picked = await self._ai_pick_recipe(
                criteria, recipes, ai_ent,
                recent_names=recent_names,
                max_calories=max_calories,
            )
            if picked is not None and picked in recipes:
                recipes.remove(picked)
                recipes.insert(0, picked)

        # Si un jour de la semaine est fourni, planifier le premier résultat.
        # Par défaut on écrase le repas existant : le scénario nominal de
        # suggest+weekday est « Changer de recette » (l'utilisateur clique
        # délibérément sur un jour affiché). overwrite=False (automation)
        # refuse d'écraser et renvoie les suggestions sans planifier.
        if weekday and weekday in WEEKDAYS and recipes:
            day_idx = WEEKDAYS.index(weekday)
            target_date = self.week_dates(week_offset)[day_idx]
            if self.plan.get(target_date.isoformat()) and overwrite is False:
                _LOGGER.info(
                    "Suggestion IA : %s (%s) déjà planifié (overwrite=False) — planification ignorée, suggestions renvoyées seulement",
                    weekday,
                    target_date.isoformat(),
                )
                return recipes
            chosen = recipes[0]
            # Récupérer les calories depuis l'endpoint détail
            recipe_id = _safe_id(chosen.get("id"))
            if recipe_id:
                calories = await self.async_fetch_calories(recipe_id)
                if calories is not None:
                    chosen["calories"] = calories
            # Filtre calories dur, a posteriori : les kcal ne sont connues
            # qu'ici (endpoint détail). Si la choisie dépasse max_calories,
            # on prend la première conforme — en triant par kcal connues
            # croissantes quand plusieurs sont déjà renseignées.
            if max_calories is not None and (chosen.get("calories") or 0) > max_calories:
                _LOGGER.info(
                    "Choix IA (%d kcal) > %d kcal : recherche d'une alternative",
                    chosen.get("calories") or 0, max_calories,
                )
                alts = [r for r in recipes[1:] if r.get("calories") is not None and r["calories"] <= max_calories]
                if alts:
                    chosen = alts[0]
                else:
                    # Aucune alternative conforme : on énumère les suivantes
                    # (kcal inconnues) jusqu'à en trouver une sous le seuil —
                    # garde-fou : au plus 10 lookups détail supplémentaires
                    # (compteur, et non index() dans la liste : O(n) et
                    # sémantique claire).
                    lookups = 0
                    for cand in recipes[1:]:
                        if lookups >= 10:
                            break
                        cid = _safe_id(cand.get("id"))
                        if not cid:
                            continue
                        lookups += 1
                        cal = await self.async_fetch_calories(cid)
                        if cal is not None:
                            cand["calories"] = cal
                            if cal <= max_calories:
                                chosen = cand
                                break
                    else:
                        _LOGGER.warning(
                            "Aucune recette ≤ %d kcal trouvée : le choix IA est conservé",
                            max_calories,
                        )
            self.plan[target_date.isoformat()] = chosen
            await self.async_save()
            _LOGGER.info(
                "Repas '%s' planifié sur %s via suggestion IA",
                chosen.get("name", ""),
                weekday,
            )

        return recipes

    # ------------------------------------------------------------------
    # Connexion au compte Jow (token JWT + rafraîchissement automatique)
    # ------------------------------------------------------------------
    def _jow_auth_headers(self) -> dict:
        """Headers d'authentification pour l'API Jow avec le token JWT."""
        return {
            "accept": "application/json",
            "accept-language": "fr",
            "content-type": "application/json",
            "x-jow-withmeta": "1",
            "origin": "https://jow.fr",
            "authorization": f"Bearer {self.jow_token}" if self.jow_token else "",
        }

    @property
    def is_authenticated(self) -> bool:
        """True si un token Jow est configuré."""
        return bool(self.jow_token)

    async def _async_jow_get(
        self, url: str, params: dict | None = None, timeout: int = 15
    ) -> requests.Response | None:
        """GET authentifié avec refresh automatique sur 401.

        Délègue au JowClient (api.py) depuis la refonte v1.0 — la façade
        est conservée pour les appelants historiques et les tests.
        """
        if not self.jow_token:
            return None
        return await self.api_client().get(url, params=params, timeout=timeout)

    async def _async_on_token_refreshed(self, token: str, new_refresh: str | None = None) -> None:
        """Callback du client : met à jour les tokens en mémoire + entry.

        new_refresh : l'API peut faire tourner le refresh token à chaque
        refresh — on persiste les deux pour ne pas perdre la rotation.
        """
        self.jow_token = token
        if new_refresh:
            self.jow_refresh_token = new_refresh
        await self._async_persist_tokens()

    def api_client(self):
        """JowClient partagé de l'instance (créé à la demande)."""
        from .api import JowClient

        if getattr(self, "_api_client", None) is None:
            self._api_client = JowClient(
                self.hass,
                get_access_token=lambda: self.jow_token,
                get_refresh_token=lambda: self.jow_refresh_token,
                on_token_refreshed=self._async_on_token_refreshed,
            )
        return self._api_client

    async def async_refresh_jow_token(self) -> bool:
        """Rafraîchit l'access token JWT Jow via le refresh token.

        Jow utilise un refresh token (valide ~6 mois) pour générer un access
        token (valide 48h). L'endpoint POST /public/auth/refresh attend le
        refresh token dans le corps de la requête (JSON) — ne PAS envoyer
        d'en-tête Authorization, sinon l'API répond 500.
        """
        if not self.jow_refresh_token:
            return False

        def _refresh():
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": "https://jow.fr",
                "referer": "https://jow.fr/",
                "x-jow-withmeta": "true",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "accept-language": "fr",
            }
            resp = requests.post(
                JOW_AUTH_REFRESH_URL,
                headers=headers,
                params={"availabilityZoneId": "FR"},
                data=json.dumps({"refreshToken": self.jow_refresh_token}),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("accessToken"), data.get("refreshToken")

        try:
            result = await self.hass.async_add_executor_job(_refresh)
            if result:
                new_access, new_refresh = result
                if not new_access:
                    _LOGGER.warning("Rafraîchissement token Jow : access token vide renvoyé par l'API")
                    return False
                self.jow_token = new_access
                if new_refresh:
                    self.jow_refresh_token = new_refresh
                _LOGGER.info("Token Jow rafraîchi avec succès (via refresh token)")
                # Persister les nouveaux tokens dans la config entry
                await self._async_persist_tokens()
                return True
        except Exception as err:
            _LOGGER.warning("Rafraîchissement token Jow échoué : %s", err)
        return False

    async def _async_persist_tokens(self) -> None:
        """Persiste les tokens rafraîchis dans la config entry pour survivre
        aux redémarrages de Home Assistant.

        Ne touche que l'entry de cette instance (évite d'écraser les tokens
        d'autres instances en multi-compte).
        """
        try:
            if not self.entry_id:
                return
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if entry is None:
                return
            new_data = {**entry.data}
            new_data[CONF_JOW_TOKEN] = self.jow_token
            new_data[CONF_JOW_REFRESH_TOKEN] = self.jow_refresh_token
            self.hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception as err:
            _LOGGER.debug("Persistance tokens Jow impossible : %s", err)

    async def async_get_jow_profile(self) -> dict | None:
        """Récupère le profil Jow de l'utilisateur connecté.

        Retourne None si non authentifié ou si l'API échoue (refresh
        tenté au passage par _async_jow_get).
        """
        resp = await self._async_jow_get(JOW_PROFILE_URL)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json().get("data", {})
        except ValueError:
            return None

    async def _async_jow_user_id(self) -> str | None:
        """userId Jow du compte connecté (payload du token, repli profil)."""
        try:
            payload = self.jow_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            uid = data.get("userId")
            if uid:
                return uid
        except Exception:
            pass
        profile = await self.async_get_jow_profile()
        return (profile or {}).get("userId") or (profile or {}).get("_id")

    # ------------------------------------------------------------------
    # Collections de recettes Jow (v1.2)
    # ------------------------------------------------------------------
    async def async_list_collections(self) -> dict:
        """Liste les collections du compte (avec recettes si fournies)."""
        if not self.jow_token:
            return {"collections": [], "error": "token_jow_absent"}
        uid = await self._async_jow_user_id()
        if not uid:
            return {"collections": [], "error": "user_id_indisponible"}
        colls = await self.api_client().get_collections(uid)
        return {"collections": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "type": c.get("type"),
                "is_private": c.get("isPrivate"),
                "recettes": len(c.get("recipes") or []),
            }
            for c in colls if isinstance(c, dict)
        ]}

    async def async_create_collection(self, title: str, is_private: bool = True) -> dict:
        """Crée une collection dans le compte Jow."""
        if not self.jow_token:
            return {"error": "token_jow_absent"}
        uid = await self._async_jow_user_id()
        if not uid:
            return {"error": "user_id_indisponible"}
        coll = await self.api_client().create_collection(uid, title, is_private)
        if coll.get("error"):
            return coll
        _LOGGER.info("Collection Jow créée : %s (%s)", coll.get("title"), coll.get("id"))
        return {"id": coll.get("id"), "title": coll.get("title")}

    async def async_collection_add_recipe(
        self, collections_ids: list[str], recipe_id: str | None = None,
        weekday: str | None = None, week_offset: int = 0,
    ) -> dict:
        """Ajoute une recette à des collections Jow.

        La recette vient soit de recipe_id, soit du jour du planning HA
        (weekday/week_offset) — « mets le plat de mardi dans mes
        collections » sans connaître l'id.
        """
        if not self.jow_token:
            return {"error": "token_jow_absent"}
        rid = recipe_id
        if not rid and weekday and weekday in WEEKDAYS:
            day = self.week_dates(week_offset)[WEEKDAYS.index(weekday)]
            meal = self.get_meal(day)
            rid = (meal or {}).get("id")
        if not rid:
            return {"error": "recette_introuvable",
                    "aide": "recipe_id ou weekday (jour planifié) requis"}
        uid = await self._async_jow_user_id()
        if not uid:
            return {"error": "user_id_indisponible"}
        result = await self.api_client().populate_collection(uid, rid, list(collections_ids))
        if result.get("error"):
            return result
        return {"added": True, "recipe_id": rid, "collections": list(collections_ids)}

    async def async_import_collection(self, collection_id: str, week_offset: int = 0) -> dict:
        """Importe les recettes d'une collection Jow sur les jours vides.

        Même garantie que l'import de menu : dédoublonnage global contre
        tout le planning HA + les rejets, jamais d'écrasement, les
        recettes excédentaires restent disponibles (remaining).
        """
        if not self.jow_token:
            return {"imported": 0, "error": "token_jow_absent"}
        coll = await self.api_client().get_collection(collection_id)
        recipes = [r for r in (coll.get("recipes") or []) if isinstance(r, dict)]
        if not recipes:
            return {"imported": 0, "error": None, "note": "collection_vide"}

        already_planned = {
            meal.get("id") for meal in self.plan.values()
            if isinstance(meal, dict) and meal.get("id")
        }
        rejected_ids = {r.get("id") for r in self.rejected}
        eligible = [
            r for r in recipes
            if _safe_id(r.get("id") or r.get("_id")) not in already_planned | rejected_ids
        ]

        imported = 0
        for day in self.week_dates(week_offset):
            if not eligible:
                break
            if self.plan.get(day.isoformat()):
                continue
            r = eligible.pop(0)
            rid = _safe_id(r.get("id") or r.get("_id"))
            if not rid:
                continue
            stored = _recipe_to_dict(r, self.default_covers)
            if stored:
                self.plan[day.isoformat()] = stored
                imported += 1
                # kcal : absentes du flux collection — fetch détail immédiat
                calories = await self.async_fetch_calories(rid)
                if calories is not None:
                    stored["calories"] = calories
                # kcal : absentes du flux letscook — fetch détail immédiat
                # (sinon « calories inconnues » jusqu'à un sync_calories)
                calories = await self.async_fetch_calories(rid)
                if calories is not None:
                    stored["calories"] = calories

        if imported:
            await self.async_save()
        from datetime import datetime as _dt
        self.last_import = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "imported": imported,
            "source": f"collection:{coll.get('title', collection_id)}",
        }
        return {
            "imported": imported,
            "skipped": len(recipes) - len(eligible) - imported,
            "remaining": len(eligible),
            "collection": coll.get("title"),
            "error": None,
        }

    async def async_get_uploaded_recipes(self) -> list[dict]:
        """Recettes maison du compte (créées via l'app mobile Jow)."""
        if not self.jow_token:
            return []
        raw = await self.api_client().get_uploaded_recipes()
        return [_recipe_to_dict(r, self.default_covers) for r in raw if isinstance(r, dict)]

    async def async_sync_preferences_from_jow(self) -> None:
        """Synchronise allergies et préférences depuis le compte Jow.

        Remplace les champs manuels par les données du profil Jow :
        - eatingHabits → préférences (végétarien, sans gluten, etc.)
        - excludedIngredientTastes → allergies/interdits (ingrédients exclus)

        Source : /profile/unified (une seule requête, jowProfile complet —
        plus riche que /profile qui n'expose pas toujours les goûts) avec
        repli sur /profile.
        """
        jow_profile = None
        resp = await self._async_jow_get(
            "https://api.jow.fr/public/profile/unified"
        )
        if resp is not None and resp.status_code == 200:
            try:
                jow_profile = resp.json().get("data", {}).get("jowProfile") or {}
            except ValueError:
                jow_profile = None
        if not jow_profile:
            jow_profile = await self.async_get_jow_profile() or {}

        if not jow_profile:
            _LOGGER.warning("Synchronisation des préférences : profil Jow illisible")
            return

        # Eating habits → preferences
        habits = jow_profile.get("eatingHabits", {})
        pref_labels = []
        habit_map = {
            "vegetarian": "végétarien",
            "vegan": "végétalien",
            "pescatarian": "pescétarien",
            "glutenFree": "sans gluten",
            "dairyFree": "sans lactose",
            "porkless": "sans porc",
        }
        for key, label in habit_map.items():
            if habits.get(key):
                pref_labels.append(label)
        if pref_labels:
            self.preferences = ", ".join(pref_labels)
            _LOGGER.debug("Préférences synchronisées depuis Jow : %s", self.preferences)

        # Excluded ingredients → allergies/interdits
        excluded = jow_profile.get("excludedIngredientTastes", [])
        if excluded:
            allergy_names = [e.get("name", "") for e in excluded if e.get("name")]
            if allergy_names:
                self.allergies = ", ".join(allergy_names)
                _LOGGER.debug("Allergies synchronisées depuis Jow : %s", self.allergies)

    async def async_get_excluded_ingredients(self) -> list[str]:
        """Retourne la liste des ingrédients exclus du compte Jow."""
        profile = await self.async_get_jow_profile()
        if not profile:
            return []
        excluded = profile.get("excludedIngredientTastes", [])
        return [e.get("name", "") for e in excluded if e.get("name")]

    async def async_get_jow_favorites(self) -> list[dict]:
        """Récupère les recettes favorites du compte Jow (avec refresh 401)."""
        resp = await self._async_jow_get(
            JOW_FAVORITES_URL,
            params={"availabilityZoneId": "FR", "limit": 20},
        )
        if resp is None or resp.status_code != 200:
            _LOGGER.warning(
                "Favoris Jow indisponibles (HTTP %s) — token expiré ?",
                resp.status_code if resp is not None else "sans token",
            )
            return []
        try:
            return resp.json().get("data", {}).get("recipes", []) or []
        except ValueError:
            return []

    async def async_send_menu_to_jow(self, week_offset: int = 0) -> int:
        """Envoie le planning de la semaine au menu du compte Jow.

        Mécanisme découvert dans le code du site : le menu jow.fr EST la
        « liste ouverte » (openShoppingList), réécrite par POST
        /shoppinglist/open avec {meals: [{recipe, coversCount, source}]}.
        Le gol des versions antérieures n'était qu'un tracker d'events —
        les plats n'arrivaient jamais dans le menu.

        Pour ne pas écraser les plats ajoutés à la main sur jow.fr : on
        lit la liste ouverte, on y AJOUTE les plats HA manquants (dédupe
        par id de recette), puis on re-POSTe l'union complète. Les
        couverts des plats déjà présents sont conservés.
        """
        if not self.jow_token:
            _LOGGER.warning("Envoi menu Jow impossible : non authentifié")
            return 0

        # 1) plats planifiés en HA pour la semaine visée
        ha_meals = []
        for day in self.week_dates(week_offset):
            meal = self.get_meal(day)
            if meal and meal.get("id"):
                ha_meals.append(meal)
        if not ha_meals:
            _LOGGER.warning("Aucun repas planifié à envoyer à Jow")
            return 0

        # 2) lire la liste ouverte actuelle (route stable du site)
        data = await self._async_get_letscook()
        osl = data.get("openShoppingList") or {}
        existing_meals = [m for m in (osl.get("meals") or []) if isinstance(m, dict)]

        # 3) merger : plats existants conservés, plats HA ajoutés si absents
        existing_ids = {
            _safe_id((m.get("recipe") or {}).get("id") or (m.get("recipe") or {}).get("_id"))
            for m in existing_meals
        }
        body_meals = []
        for m in existing_meals:
            r = m.get("recipe") or {}
            rid = _safe_id(r.get("id") or r.get("_id"))
            if not rid:
                continue
            body_meals.append({
                "recipe": rid,
                "coversCount": m.get("coversCount") or self.default_covers,
                "source": m.get("source") or "jow",
            })
        added = 0
        for meal in ha_meals:
            if meal["id"] in existing_ids:
                continue
            body_meals.append({
                "recipe": meal["id"],
                "coversCount": meal.get("covers") or self.default_covers,
                "source": "jow",
            })
            added += 1

        if not added:
            _LOGGER.info("Menu Jow déjà à jour (aucun plat HA à ajouter)")
            return 0

        # 4) réécrire la liste ouverte avec l'union (cache mis à jour par le helper)
        status = await self._async_rewrite_open_list(body_meals)
        if status in (200, 201, 204):
            from datetime import datetime as _dt
            self.last_send = {
                "ts": _dt.now().isoformat(timespec="seconds"),
                "added": added,
                "total_jow": len(body_meals),
            }
            _LOGGER.info(
                "Menu envoyé à Jow : %d plats ajoutés (liste réécrite avec %d au total)",
                added, len(body_meals),
            )
            # réveiller les capteurs d'état (cache jow_open_meals à jour)
            async_dispatcher_send(self.hass, self.update_signal)
            return added
        _LOGGER.warning(
            "Réécriture de la liste Jow échouée (HTTP %s) — la liste existante est préservée",
            status,
        )
        return 0

    async def async_import_menu_from_jow(self, week_offset: int = 0) -> dict:
        """Importe le menu du compte Jow (plats ajoutés sur jow.fr/l'app).

        Sens Jow → HA de la synchro bidirectionnelle.

        Source : GET /profile/letscook → data.openShoppingList.meals (+
        pendingMenu en secours) — c'est la route que le site utilise pour
        remonter les recettes ajoutées au menu ; l'endpoint /menu reste
        tenté en premier mais il est instable (500 observé sur des menus
        serveur en état inattendu) — parsing défensif, aucune exception
        ne remonte, un rapport structuré est retourné.

        Les repas importés n'ont pas de date Jow (la liste ouverte est
        un panier de recettes) : ils remplissent les jours vides de la
        semaine visée, dans l'ordre de la liste.
        """
        if not self.jow_token:
            return {"imported": 0, "error": "token_jow_absent"}

        # 1) endpoint /menu (historique) — souvent 500, on tente puis on replie
        resp = await self._async_jow_get(
            JOW_MENU_URL, params={"availabilityZoneId": "FR"}
        )
        if resp is not None and resp.status_code == 200:
            try:
                data = resp.json().get("data", {})
                result = self._import_from_menu_data(data, week_offset)
                if result["imported"]:
                    await self.async_save()
                    return result
            except ValueError:
                pass  # réponse illisible : on replie sur letscook

        # 2) source de repli : profile/letscook (route du site, stable)
        if not self.jow_token:
            return {"imported": 0, "error": "token_jow_absent"}
        data = await self._async_get_letscook()
        if not data:
            return {"imported": 0, "error": "lecture_letscook_impossible"}

        meals = []
        if isinstance(data, dict):
            # liste ouverte : les recettes ajoutées au menu (non encore cuisinées)
            osl = data.get("openShoppingList") or {}
            if isinstance(osl, dict):
                meals = [m for m in (osl.get("meals") or []) if isinstance(m, dict)]
            # pendingMenu (si présent) en complément, dédupe par id de recette
            pm = data.get("pendingMenu")
            if isinstance(pm, dict):
                seen = {(m.get("recipe") or {}).get("id") for m in meals}
                for m in (pm.get("meals") or []):
                    if isinstance(m, dict) and (m.get("recipe") or {}).get("id") not in seen:
                        meals.append(m)

        if not meals:
            return {"imported": 0, "skipped": 0, "error": None, "note": "menu_jow_vide"}

        # Dédoublonnage : écarter les plats déjà planifiés SUR LA SEMAINE
        # VISÉE et les rejets (un plat refusé dans HA ne revient pas).
        # Un plat présent sur une AUTRE semaine de HA est importable :
        # « importer mon menu jow » remplit les jours vides de la semaine
        # affichée même si le plat a déjà servi ailleurs — le doublon
        # interdit est dans la MÊME semaine, pas à travers l'historique
        # (retour utilisateur : 23 plats de la liste restaient « skipped »
        # alors que la semaine avait des jours vides).
        week_ids = {
            (meal or {}).get("id")
            for day in self.week_dates(week_offset)
            if isinstance((meal := self.plan.get(day.isoformat())), dict)
        } - {None}
        rejected_ids = {r.get("id") for r in self.rejected}
        eligible = []
        skipped_deja = 0
        for m in meals:
            rid = _safe_id((m.get("recipe") or {}).get("id") or (m.get("recipe") or {}).get("_id"))
            if rid in week_ids or rid in rejected_ids:
                skipped_deja += 1
                continue
            eligible.append(m)

        # Les repas n'ont pas de date : remplir les jours vides de la semaine
        # visée, dans l'ordre. Couverts : coversCount du repas Jow s'il est
        # présent. Les plats non placés ne sont PAS perdus : ils restent dans
        # la liste Jow, un import ultérieur (autre semaine, jours libérés)
        # les reprendra — puisqu'on ne marque rien côté Jow.
        imported = 0
        for day in self.week_dates(week_offset):
            if not eligible:
                break
            if self.plan.get(day.isoformat()):
                continue
            m = eligible.pop(0)
            recipe = m.get("recipe") or {}
            rid = _safe_id(recipe.get("id") or recipe.get("_id"))
            if not rid:
                continue
            covers = m.get("coversCount") or self.default_covers
            stored = _recipe_to_dict(recipe, covers)
            if stored:
                self.plan[day.isoformat()] = stored
                imported += 1

        if imported:
            await self.async_save()
        from datetime import datetime as _dt
        self.last_import = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "imported": imported,
            "skipped": skipped_deja,
            "remaining": len(eligible),
        }
        _LOGGER.info(
            "Import menu Jow (letscook) : %d importés, %d écartés (déjà planifiés/rejetés), %d restants pour plus tard",
            imported, skipped_deja, len(eligible),
        )
        return {
            "imported": imported,
            "skipped": skipped_deja,
            "remaining": len(eligible),
            "error": None,
        }

    def _import_from_menu_data(self, data: Any, week_offset: int = 0) -> dict:
        """Cœur d'import du menu Jow : parsing pur, testable sans HTTP.

        Formats tolérés : {"recipes": [...]} (date par recette : date /
        day / plannedDate) ou dict {date_iso: recette}. Ne JAMAIS écraser
        un jour déjà planifié en HA.
        """
        raw = data.get("recipes") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict):
            raw = data  # peut-être {date: recette} directement

        imported = 0
        skipped = 0
        week_keys = {d.isoformat(): d for d in self.week_dates(week_offset)}
        # dédoublonnage global : tout le planning HA + les rejets
        already_planned_ids = {
            meal.get("id") for meal in self.plan.values()
            if isinstance(meal, dict) and meal.get("id")
        }
        rejected_ids = {r.get("id") for r in self.rejected}

        def _try_import(date_iso: str | None, recipe: dict) -> None:
            nonlocal imported, skipped
            if not isinstance(recipe, dict):
                skipped += 1
                return
            rid = _safe_id(recipe.get("id") or recipe.get("_id"))
            if not rid or not date_iso or date_iso not in week_keys:
                skipped += 1
                return
            # Ne pas écraser un repas déjà planifié ce jour-là en HA
            if self.plan.get(date_iso):
                skipped += 1
                return
            # Dédoublonnage global (même garantie que le chemin letscook) :
            # un plat déjà planifié ailleurs dans HA ou rejeté n'entre pas
            if rid in already_planned_ids or rid in rejected_ids:
                skipped += 1
                return
            stored = _recipe_to_dict(recipe, self.default_covers)
            if stored:
                self.plan[date_iso] = stored
                imported += 1
            else:
                skipped += 1

        if isinstance(raw, list):
            for recipe in raw:
                # date au niveau recette (champs observés : date / day / plannedDate)
                d = recipe.get("date") or recipe.get("day") or recipe.get("plannedDate")
                _try_import(str(d)[:10] if d else None, recipe)
        elif isinstance(raw, dict):
            for date_iso, recipe in raw.items():
                if isinstance(date_iso, str) and re.match(r"^\d{4}-\d{2}-\d{2}", date_iso):
                    _try_import(date_iso[:10], recipe)
                else:
                    skipped += 1

        if imported or skipped:
            _LOGGER.info("Import menu Jow : %d importés, %d ignorés", imported, skipped)
        return {"imported": imported, "skipped": skipped, "error": None}

    async def async_start_token_refresh(self) -> None:
        """Démarre le rafraîchissement périodique du token Jow.

        Le token Jow n'est pas requis pour le fonctionnement de base
        (recherche publique de recettes). Il sert à synchroniser les
        allergènes et préférences du compte Jow. On rafraîchit
        automatiquement toutes les 24h via le refresh token (valide ~6 mois).
        """
        if not self.jow_token:
            return

        from homeassistant.helpers.event import async_track_time_interval

        if self._token_refresh_cancel:
            self._token_refresh_cancel()
        # Rafraîchir toutes les 24h (access token valide 48h)
        self._token_refresh_cancel = async_track_time_interval(
            self.hass,
            self._async_check_token_callback,
            timedelta(seconds=JOW_TOKEN_REFRESH_INTERVAL),
        )

    def async_start_purge(self) -> None:
        """Purge hebdomadaire du planning : les repas de plus de 30 jours
        ne servent plus (l'anti-répétition n'a besoin que de 4 semaines)
        et gonfleraient le stockage indéfiniment sur un HA qui tourne
        des mois sans redémarrer. Indépendant du token Jow."""
        from homeassistant.helpers.event import async_track_time_interval

        if getattr(self, "_purge_cancel", None):
            self._purge_cancel()
        self._purge_cancel = async_track_time_interval(
            self.hass,
            self._async_purge_callback,
            timedelta(days=7),
        )

    async def _async_purge_callback(self, now=None) -> None:
        """Purge périodique du planning (repas de plus de 30 jours)."""
        await self.async_purge_old()

    async def _async_check_token_callback(self, now=None) -> None:
        """Rafraîchit périodiquement le token Jow.

        Tente d'abord un refresh via le refresh token. Si le refresh
        échoue (token expiré/révoqué), vérifie la validité du access token
        restant et notifie l'utilisateur si nécessaire. Les recettes
        publiques continuent de fonctionner dans tous les cas.
        """
        if not self.jow_token:
            return
        # 1) Tenter un refresh proactif
        if self.jow_refresh_token:
            refreshed = await self.async_refresh_jow_token()
            if refreshed:
                # Re-sync des préférences (allergènes, habitudes) depuis le
                # compte Jow pour rester à jour si l'utilisateur les modifie.
                await self.async_sync_preferences_from_jow()
                return
        # 2) Si le refresh a échoué, vérifier si le access token est encore
        # valide (il peut rester jusqu'à 48h de marge).
        valid = await self.async_check_token_validity()
        if not valid:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass,
                "Le refresh token Jow a expiré. Les recettes publiques continuent de fonctionner, "
                "mais les allergènes ne sont plus synchronisés. Récupérez un nouveau refresh token "
                "sur jow.fr (F12 → localStorage → jow_store) et mettez-le à jour dans la "
                "configuration de l'intégration Jow.",
                "Jow - Token expiré",
                "jow_token_expired",
            )
            # Vider le refresh token pour arrêter les tentatives inutiles
            self.jow_refresh_token = ""
            await self._async_persist_tokens()

    async def async_check_token_validity(self) -> bool:
        """Vérifie si le token Jow est encore valide.

        Un profil vide (data: {}) est un compte valide : seul un échec
        HTTP (401/403…) signifie un token expiré.
        """
        if not self.jow_token:
            return False
        profile = await self.async_get_jow_profile()
        if profile is not None:
            return True
        _LOGGER.warning("Token Jow expiré ou invalide")
        return False
