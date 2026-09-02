"""Client HTTP Jow — primitives et routes en cours de centralisation.

Introduit à la refonte v1.0 : gère les headers, le refresh 401→retry
et les POST authentifiés. Le manager reste la façade métier et
conserve certaines implémentations historiques (recherche, détail,
letscook…) qui migrent ici progressivement — les méthodes présentes
ici non encore consommées servent aux prochaines étapes (commande,
courses enrichies). Les pièges de l'API sont documentés dans
docs/jow-api.md.

**Cookie JowSession (sticky session)** — découverte déterminante :
l'API pose un cookie `JowSession` à chaque refresh (Set-Cookie), qui
route les requêtes vers LE nœud serveur qui connaît la session. Les
sessions magasin vivent sur le nœud du cookie posé lors du login
enseigne : pour qu'une intégration puisse utiliser la session
magasin, elle doit (a) conserver son propre cookie (jar persistant)
et (b) l'utilisateur connecte l'enseigne dans un navigateur auquel on
a injecté CE cookie (devtools → Application → Cookies) — la session
enseigne s'attache alors au même nœud et devient visible de HA.

`requests` synchrone dans l'executor : choix documenté (pattern HA
supporté ; couvert par les tests qui mockent requests au niveau module).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Bases et headers
# ------------------------------------------------------------------
JOW_API_BASE = "https://api.jow.fr/public"
JOW_STATIC_URL = "https://static.jow.fr/"

JOW_SEARCH_URL = f"{JOW_API_BASE}/recipe/quicksearch"
JOW_RECIPE_URL = f"{JOW_API_BASE}/recipe"
JOW_LETSCOOK_URL = f"{JOW_API_BASE}/profile/letscook"
JOW_SHOPPINGLIST_OPEN_URL = f"{JOW_API_BASE}/shoppinglist/open"
JOW_SHOPPINGLIST_URL = f"{JOW_API_BASE}/shoppinglist"
JOW_PROFILE_URL = f"{JOW_API_BASE}/profile"
JOW_PROFILE_UNIFIED_URL = f"{JOW_API_BASE}/profile/unified"
JOW_FAVORITES_URL = f"{JOW_API_BASE}/recipes/favorites"
JOW_MENU_URL = f"{JOW_API_BASE}/menu/week"
JOW_AUTH_REFRESH_URL = f"{JOW_API_BASE}/auth/refresh"
JOW_RECO_MORE_URL = f"{JOW_API_BASE}/recipes/reco/more"
JOW_RECO_MAIN_URL = f"{JOW_API_BASE}/recipes/reco/main"
JOW_FILTERED_SEARCH_URL = f"{JOW_API_BASE}/recipes/filtered-search"
JOW_PROVIDERS_URL = f"{JOW_API_BASE}/provider_public"
JOW_STORES_PUBLIC_URL = f"{JOW_API_BASE}/stores_public/all"

_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "fr",
    "content-type": "application/json",
    "x-jow-withmeta": "1",
    "origin": "https://jow.fr",
    "referer": "https://jow.fr/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}


class JowClient:
    """Client HTTP Jow pour une instance (token + refresh + cookie jar).

    La `requests.Session` persiste le cookie JowSession posé par les
    réponses (sticky session) : toutes les requêtes de l'instance routent
    vers le même nœud serveur — condition pour voir la session magasin
    (cf. docstring du module).
    """

    def __init__(self, hass, get_access_token, get_refresh_token, on_token_refreshed=None):
        """hass : pour l'executor ; get_*_token : callbacks vers le manager ;
        on_token_refreshed(token, new_refresh) : persiste les tokens."""
        self._hass = hass
        self._get_access = get_access_token
        self._get_refresh = get_refresh_token
        self._on_refreshed = on_token_refreshed
        # cookie jar persistant (JowSession) — partagé par toutes les
        # requêtes de cette instance, y compris les fonctions module-level
        # (elles l'utilisent via self._session ci-dessous)
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------
    @property
    def access_token(self) -> str | None:
        return self._get_access()

    @property
    def is_authenticated(self) -> bool:
        return bool(self._get_access())

    def _auth_headers(self) -> dict:
        headers = dict(_BASE_HEADERS)
        token = self._get_access()
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    async def _exec(self, fn, *args):
        """Exécute une fonction bloquante dans l'executor."""
        return await self._hass.async_add_executor_job(fn, *args)

    async def get(self, url: str, params: dict | None = None, timeout: int = 15,
                  withmeta: str = "1") -> requests.Response | None:
        """GET authentifié avec refresh automatique sur 401 (une retry)."""
        if not self._get_access():
            return None

        def _get():
            headers = self._auth_headers()
            headers["x-jow-withmeta"] = withmeta
            return self._session.get(url, headers=headers, params=params or {}, timeout=timeout)

        resp = await self._exec(_get)
        if resp.status_code == 401 and self._get_refresh():
            _LOGGER.info("401 sur %s — rafraîchissement du token Jow", url)
            new_token = await self.refresh_token()
            if new_token:
                resp = await self._exec(_get)
        return resp

    async def post(self, url: str, body: dict | Any, params: dict | None = None,
                   timeout: int = 20, auth: bool = True,
                   withmeta: str = "1") -> requests.Response | None:
        """POST (authentifié par défaut) — sans retry 401 automatique pour
        les écritures (le caller décide ; réessayer un POST aveuglément
        peut dupliquer une action)."""
        if auth and not self._get_access():
            return None

        def _post():
            headers = self._auth_headers() if auth else dict(_BASE_HEADERS)
            headers["x-jow-withmeta"] = withmeta
            return self._session.post(
                url, headers=headers, params=params or {},
                data=body if isinstance(body, str) else json.dumps(body),
                timeout=timeout,
            )

        return await self._exec(_post)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def refresh_token(self) -> str | None:
        """POST /auth/refresh — SANS authorization header (piège connu).
        Retourne le nouvel access token, persisté via callback."""
        refresh = self._get_refresh()
        if not refresh:
            return None

        def _refresh():
            # IMPORTANT : aucun header authorization ici (sinon 401).
            # La Session conserve le JowSession posé par cette réponse
            # (Set-Cookie) : c'est LE cookie de routage de l'instance.
            return self._session.post(
                JOW_AUTH_REFRESH_URL,
                headers=dict(_BASE_HEADERS),
                params={"availabilityZoneId": "FR"},
                json={"refreshToken": refresh},
                timeout=20,
            )

        try:
            resp = await self._exec(_refresh)
            if resp.status_code != 200:
                _LOGGER.warning("Refresh Jow refusé (HTTP %s)", resp.status_code)
                return None
            # L'API renvoie les tokens À LA RACINE (pas de wrapper "data" —
            # vérifié sur la réponse réelle : {accessToken, refreshToken, …}).
            data = resp.json()
            new_access = data.get("accessToken")
            new_refresh = data.get("refreshToken")
            # rotation du refresh token : le serveur peut le faire tourner,
            # on persiste les deux via le callback (le manager décide)
            if new_access and self._on_refreshed:
                await self._on_refreshed(new_access, new_refresh)
            return new_access
        except Exception as err:
            _LOGGER.warning("Refresh Jow échoué : %s", err)
            return None

    # ------------------------------------------------------------------
    # Recettes (lecture)
    # ------------------------------------------------------------------
    async def search_recipes(self, query: str, limit: int = 50, start: int = 0) -> list[dict]:
        """quicksearch : publique, OU logique (voir pièges doc), 50/page."""
        def _search():
            resp = self._session.post(
                JOW_SEARCH_URL,
                headers=dict(_BASE_HEADERS),
                params={"start": str(start), "availabilityZoneId": "FR",
                        "query": query, "limit": str(max(limit, 1))},
                data="{}",
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("data", {}).get("content", []) or []

        try:
            return await self._exec(_search)
        except Exception as err:
            _LOGGER.warning("Recherche Jow « %s » échouée : %s", query, err)
            return []

    async def get_recipe_detail(self, recipe_id: str) -> dict | None:
        """GET /recipe/{id} — exige x-jow-withmeta: true (piège)."""
        if not recipe_id:
            return None

        def _fetch():
            headers = dict(_BASE_HEADERS)
            headers["x-jow-withmeta"] = "true"
            resp = self._session.get(
                f"{JOW_RECIPE_URL}/{recipe_id}", headers=headers, timeout=10
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await self._exec(_fetch)
            return data if isinstance(data, dict) else None
        except Exception as err:
            _LOGGER.debug("Détail Jow %s indisponible : %s", recipe_id, err)
            return None

    # ------------------------------------------------------------------
    # Compte
    # ------------------------------------------------------------------
    async def get_profile(self) -> dict | None:
        resp = await self.get(JOW_PROFILE_URL)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json().get("data", {})
        except ValueError:
            return None

    async def get_unified_profile(self) -> dict | None:
        resp = await self.get(JOW_PROFILE_UNIFIED_URL)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json().get("data", {})
        except ValueError:
            return None

    async def get_favorites(self, limit: int = 20) -> list[dict]:
        resp = await self.get(JOW_FAVORITES_URL, params={"availabilityZoneId": "FR", "limit": str(limit)})
        if resp is None or resp.status_code != 200:
            return []
        try:
            return resp.json().get("data", {}).get("recipes", []) or []
        except ValueError:
            return []

    async def get_letscook(self, nb_meals: int = 40) -> dict:
        """profile/letscook : LA route du menu (openShoppingList…)."""
        resp = await self.get(
            JOW_LETSCOOK_URL,
            params={"availabilityZoneId": "FR", "nbMeals": str(nb_meals)},
        )
        if resp is None or resp.status_code != 200:
            return {}
        try:
            return resp.json().get("data", {}) or {}
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # Recommandations
    # ------------------------------------------------------------------
    async def reco_more(self, body: dict, count: int = 10) -> list[dict]:
        """POST /recipes/reco/more — suggestions natives du moteur Jow."""
        resp = await self.post(
            JOW_RECO_MORE_URL, body,
            params={"availabilityZoneId": "FR", "count": str(count)},
        )
        if resp is None or resp.status_code != 200:
            _LOGGER.warning("Recommandations Jow indisponibles (HTTP %s)",
                            resp.status_code if resp is not None else "sans token")
            return []
        try:
            data = resp.json().get("data", [])
        except ValueError:
            return []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Menu / liste ouverte
    # ------------------------------------------------------------------
    async def rewrite_open_list(self, body_meals: list[dict],
                                populate: bool = True) -> requests.Response | None:
        """POST /shoppinglist/open — RÉÉCRIT la liste ouverte (écrase !).

        Le caller DOIT avoir lu l'existant et mergé avant (garantie
        anti-perte documentée dans docs/jow-api.md).
        """
        params = {"availabilityZoneId": "FR"}
        if populate:
            params["populateRecipes"] = "true"
            params["populateIngredients"] = "true"
        return await self.post(
            JOW_SHOPPINGLIST_OPEN_URL, {"meals": body_meals}, params=params
        )

    async def get_menu_week(self) -> requests.Response | None:
        """GET /menu/week — instable (500 observés), tentative seulement."""
        return await self.get(JOW_MENU_URL, params={"availabilityZoneId": "FR"})

    async def get_shoppinglist(self) -> dict | None:
        """GET /shoppinglist — liste avec ingrédients agrégés. None si vide/erreur."""
        resp = await self.get(JOW_SHOPPINGLIST_URL, params={"availabilityZoneId": "FR"})
        if resp is None:
            return None
        if resp.status_code == 204:
            return {}
        if resp.status_code != 200:
            return None
        try:
            return resp.json().get("data", {})
        except ValueError:
            return None

    @property
    def jow_session_cookie(self) -> str | None:
        """Cookie JowSession courant de l'instance (pour injection navigateur).

        L'utilisateur peut l'ajouter à son navigateur jow.fr (devtools →
        Application → Cookies → api.jow.fr → JowSession) avant de connecter
        son enseigne : la session magasin s'attachera au même nœud serveur
        que HA, et les services order_* verront le magasin.
        """
        try:
            return self._session.cookies.get("JowSession", domain=".jow.fr") or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Commande (lecture)
    # ------------------------------------------------------------------
    async def get_providers(self) -> list[dict]:
        """Providers disponibles (Intermarché, Carrefour…) selon la zone."""
        resp = await self.get(JOW_PROVIDERS_URL, params={"availabilityZoneId": "FR"})
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json().get("data", {})
            return data.get("availableProviders", []) or []
        except ValueError:
            return []

    # ------------------------------------------------------------------
    # Collections de recettes (vérifié sur l'API réelle, sept. 2026)
    # ------------------------------------------------------------------
    async def get_collections(self, user_id: str) -> list[dict]:
        """GET /users/{id}/collections — les collections du compte.

        Réponse : data.content = [{id, title, type (favorites/try-later/
        default), isPrivate, permissions, recipes?}].
        """
        resp = await self.get(
            f"{JOW_API_BASE}/users/{user_id}/collections",
            params={"availabilityZoneId": "FR"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json().get("data", {})
            return data.get("content", []) or []
        except ValueError:
            return []

    async def create_collection(self, user_id: str, title: str, is_private: bool = True) -> dict:
        """POST /users/{id}/collections — crée une collection.

        Corps (piège) : {collection: {title, isPrivate}} — un title à la
        racine renvoie 500. Retourne la collection créée (avec id).
        """
        resp = await self.post(
            f"{JOW_API_BASE}/users/{user_id}/collections",
            body={"collection": {"title": title, "isPrivate": is_private}},
            params={"availabilityZoneId": "FR"},
        )
        if resp is None or resp.status_code != 200:
            return {"error": f"http_{resp.status_code if resp is not None else 'sans_token'}"}
        try:
            data = resp.json().get("data", {})
            coll = data.get("collection") or data
            return coll if isinstance(coll, dict) else {"error": "reponse_illisible"}
        except ValueError:
            return {"error": "reponse_illisible"}

    async def populate_collection(self, user_id: str, recipe_id: str,
                                  collections_ids: list[str], source: str = "jow") -> dict:
        """POST /users/{id}/collections/populate — recette dans des collections.

        Corps : {recipeId, source, collectionsIds}.
        ⚠️ PIÈGE VÉRIFIÉ : ne PAS passer availabilityZoneId en query string —
        avec le param, l'API répond 200 mais n'écrit que les collections
        système (favoris) en ignorant les collections custom.
        """
        resp = await self.post(
            f"{JOW_API_BASE}/users/{user_id}/collections/populate",
            body={"recipeId": recipe_id, "source": source,
                  "collectionsIds": collections_ids},
        )
        if resp is None or resp.status_code != 200:
            return {"error": f"http_{resp.status_code if resp is not None else 'sans_token'}"}
        try:
            return resp.json().get("data", {}) or {}
        except ValueError:
            return {"error": "reponse_illisible"}

    async def get_collection(self, collection_id: str) -> dict:
        """GET /collections/{id} — une collection avec ses recettes.

        Réponse : data.content.collection = {…, recipes: [...]}.
        """
        resp = await self.get(
            f"{JOW_API_BASE}/collections/{collection_id}",
            params={"availabilityZoneId": "FR"},
        )
        if resp is None or resp.status_code != 200:
            return {}
        try:
            content = resp.json().get("data", {}).get("content", {})
            return content.get("collection", {}) or {}
        except ValueError:
            return {}

    async def get_uploaded_recipes(self) -> list[dict]:
        """GET /recipes/uploaded — ses recettes maison (créées via l'app).

        La CRÉATION n'est pas exposée dans l'API web (feature app mobile :
        import par scan/photo/URL) — lecture/édition/suppression seulement.
        """
        resp = await self.get(
            f"{JOW_API_BASE}/recipes/uploaded",
            params={"availabilityZoneId": "FR"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json().get("data", {})
            return data.get("recipes", []) or []
        except ValueError:
            return []

    async def add_recipe_to_favorites(self, user_id: str, recipe_id: str,
                                      source: str = "jow", selected_from: str = "cookbook") -> dict:
        """POST /users/{id}/collections/favorites — ajoute un favori.

        Corps observé sur le site : {recipeId, source, selectedFrom}.
        C'est LA porte d'entrée qui référence une recette quelconque
        dans le compte — le populate (collections) n'accepte ensuite
        que des recettes déjà connues (favori) : on ajoute donc au
        favori PUIS on populate la collection cible.
        """
        resp = await self.post(
            f"{JOW_API_BASE}/users/{user_id}/collections/favorites",
            body={"recipeId": recipe_id, "source": source,
                  "selectedFrom": selected_from},
        )
        if resp is None or resp.status_code != 200:
            return {"error": f"http_{resp.status_code if resp is not None else 'sans_token'}"}
        try:
            return resp.json().get("data", {}) or {}
        except ValueError:
            return {"error": "reponse_illisible"}
