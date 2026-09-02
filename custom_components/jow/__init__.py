"""Intégration Jow (non officielle) pour Home Assistant."""

from __future__ import annotations

from datetime import date, datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_CHOICE,
    ATTR_COVERS,
    ATTR_CRITERIA,
    ATTR_DATE,
    ATTR_ENTRY_NAME,
    ATTR_INGREDIENT,
    ATTR_LIMIT,
    ATTR_QUERY,
    ATTR_TO_DATE,
    ATTR_TO_WEEK_OFFSET,
    ATTR_TO_WEEKDAY,
    ATTR_WEEK_OFFSET,
    ATTR_WEEKDAY,
    CONF_AI_ENTITY,
    CONF_ALLERGIES,
    CONF_JOW_REFRESH_TOKEN,
    CONF_JOW_TOKEN,
    CONF_PREFERENCES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COVERS,
    DOMAIN,
    SERVICE_ADD_AVOID,
    SERVICE_ADD_BANNED,
    SERVICE_CLEAR_MEAL,
    SERVICE_CLEAR_RECENT,
    SERVICE_CLEAR_WEEK,
    SERVICE_COLLECTION_ADD_RECIPE,
    SERVICE_COLLECTION_CREATE,
    SERVICE_COLLECTION_IMPORT,
    SERVICE_COLLECTIONS_LIST,
    SERVICE_COPY_MEAL,
    SERVICE_EXCLUDE_INGREDIENT,
    SERVICE_EXPIRING,
    SERVICE_EXPORT_WEEK,
    SERVICE_GET_CONTEXT,
    SERVICE_IMPORT_MENU,
    SERVICE_IMPORT_TOKEN,
    SERVICE_MEAL_DONE,
    SERVICE_ORDER_CART,
    SERVICE_ORDER_CREATE,
    SERVICE_ORDER_PAY,
    SERVICE_ORDER_PROVIDERS,
    SERVICE_ORDER_SLOTS,
    SERVICE_PLAN_MEAL,
    SERVICE_RECOMMENDATIONS,
    SERVICE_REFRESH_SHOPPING_LIST,
    SERVICE_RENEW_WEEK,
    SERVICE_RESET_REJECTS,
    SERVICE_SEARCH,
    SERVICE_SEND_MENU,
    SERVICE_SET_COVERS,
    SERVICE_SUGGEST,
    SERVICE_SYNC_CALORIES,
    SERVICE_SYNC_FAVORITES,
    SERVICE_SYNC_PREFERENCES,
    SERVICE_SYNC_PROFILE,
    SERVICE_UPLOADED_RECIPES,
    WEEKDAYS,
)
from .manager import JowManager, _recipe_to_dict

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO, Platform.CALENDAR]
# state.py expose des sensors supplémentaires via la plateforme sensor :
# enregistrés par sensor.py via un import (pas une plateforme séparée).


def _get_manager(hass: HomeAssistant, call: ServiceCall, default_manager: JowManager) -> JowManager:
    """Résout le bon manager selon le paramètre entry_name.

    Si entry_name est fourni, on cherche l'instance correspondante.
    Sinon, on retombe sur la première instance configurée (ordre des
    config entries, stable d'un redémarrage à l'autre) — et non sur
    celle capturée par la closure du dernier setup, dont l'ordre
    dépendrait de l'ordre de chargement des entrées.
    """
    entry_name = call.data.get("entry_name")
    instances = hass.data.get(DOMAIN, {})
    if not entry_name:
        for entry_id in instances:
            return instances[entry_id]
        return default_manager
    for entry_id, manager in instances.items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and (
            entry.title == entry_name
            or entry.data.get("name") == entry_name
            or entry.options.get("name") == entry_name
        ):
            return manager
    _LOGGER.warning("Instance Jow « %s » introuvable, utilisation de l'instance par défaut", entry_name)
    for entry_id in instances:
        return instances[entry_id]
    return default_manager


def _resolve_date(manager: JowManager, call: ServiceCall) -> date:
    """Accepte soit une date explicite, soit un jour de la semaine."""
    if raw := call.data.get(ATTR_DATE):
        if isinstance(raw, date):
            return raw
        try:
            return datetime.fromisoformat(str(raw)).date()
        except ValueError:
            _LOGGER.warning("Date invalide reçue « %s », utilisation d'aujourd'hui", raw)
            return date.today()

    weekday = call.data.get(ATTR_WEEKDAY)
    offset = call.data.get(ATTR_WEEK_OFFSET, 0)
    if weekday:
        return manager.week_dates(offset)[WEEKDAYS.index(weekday)]
    return date.today()


def _resolve_to_date(manager: JowManager, call: ServiceCall) -> date:
    """Résout la date cible pour copy_meal (to_date ou to_weekday)."""
    if raw := call.data.get(ATTR_TO_DATE):
        if isinstance(raw, date):
            return raw
        try:
            return datetime.fromisoformat(str(raw)).date()
        except ValueError:
            _LOGGER.warning("Date invalide reçue « %s », utilisation d'aujourd'hui", raw)
            return date.today()
    weekday = call.data.get(ATTR_TO_WEEKDAY)
    # week_offset accepté comme alias de to_week_offset (le schema de
    # copy_meal injecte un default pour to_week_offset, donc la fallback
    # classique ne s'applique jamais : on compare explicitement).
    offset = call.data.get(ATTR_TO_WEEK_OFFSET)
    if offset is None:
        offset = call.data.get(ATTR_WEEK_OFFSET, 0)
    if weekday:
        return manager.week_dates(offset)[WEEKDAYS.index(weekday)]
    return date.today()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configure l'intégration depuis l'UI."""
    opts = entry.options
    # L'access token (jow_token) est persisté par le refresh automatique,
    # mais l'utilisateur ne saisit que le refresh token. On le récupère
    # depuis les options ; l'access token vient de la config entry (si
    # un refresh a déjà eu lieu) ou sera généré au démarrage.
    jow_token = opts.get(CONF_JOW_TOKEN, "") or entry.data.get(CONF_JOW_TOKEN, "")
    manager = JowManager(
        hass,
        opts.get("covers", DEFAULT_COVERS),
        allergies=opts.get(CONF_ALLERGIES, ""),
        preferences=opts.get(CONF_PREFERENCES, ""),
        ai_entity=opts.get(CONF_AI_ENTITY, ""),
        weather_entity=opts.get(CONF_WEATHER_ENTITY, ""),
        jow_token=jow_token,
        # repli data : la rotation du refresh est persistée dans entry.data
        # par _async_persist_tokens — sans ce repli, un reboot ressuscitait
        # l'ancien token (révoqué par la rotation) → boucle « token expiré »
        jow_refresh_token=opts.get(CONF_JOW_REFRESH_TOKEN, "")
        or entry.data.get(CONF_JOW_REFRESH_TOKEN, ""),
        entry_id=entry.entry_id,
    )
    await manager.async_load()
    await manager.async_purge_old()
    # Purge hebdomadaire du planning (indépendante du token Jow)
    manager.async_start_purge()

    # Si on a un refresh token mais pas d'access token valide, on en
    # génère un immédiatement au démarrage.
    if manager.jow_refresh_token and not manager.is_authenticated:
        await manager.async_refresh_jow_token()
    # Vérifier le token Jow et synchroniser les préférences si valide
    if manager.is_authenticated:
        if await manager.async_check_token_validity():
            await manager.async_sync_preferences_from_jow()
        # Démarrer le rafraîchissement périodique du token
        await manager.async_start_token_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------
    async def handle_plan_meal(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        await mgr.async_plan_meal(
            day,
            call.data[ATTR_QUERY],
            covers=call.data.get(ATTR_COVERS),
            choice=call.data.get(ATTR_CHOICE, 1),
            recipe_id=call.data.get("recipe_id"),
        )

    async def handle_clear_meal(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        await mgr.async_clear_meal(_resolve_date(mgr, call))

    async def handle_clear_week(call: ServiceCall) -> ServiceResponse:
        """Vide la semaine (les plats effacés nourrissent la mémoire des rejets)."""
        mgr = _get_manager(hass, call, manager)
        await mgr.async_clear_week(
            call.data.get(ATTR_WEEK_OFFSET, 0),
            remember_rejects=call.data.get("remember_rejects", True),
        )
        return {"message": "Semaine vidée"}

    async def handle_renew_week(call: ServiceCall) -> ServiceResponse:
        """Renouvelle la semaine : vide puis replanifie 7 jours via l'IA."""
        mgr = _get_manager(hass, call, manager)
        result = await mgr.async_renew_week(
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
            covers=call.data.get(ATTR_COVERS),
            criteria=call.data.get(ATTR_CRITERIA, "plat varié équilibré"),
            weather_entity=call.data.get(CONF_WEATHER_ENTITY),
            ai_entity=call.data.get(CONF_AI_ENTITY),
            ai_prompt=call.data.get("ai_prompt", ""),
            max_calories=call.data.get("max_calories"),
            max_total_time=call.data.get("max_total_time"),
            day_criteria=call.data.get("day_criteria"),
        )
        return result

    async def handle_refresh_list(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        await mgr.async_refresh_shopping_list(
            call.data.get(ATTR_WEEK_OFFSET, 0),
            keep_checked=call.data.get("keep_checked", True),
        )

    async def handle_search(call: ServiceCall) -> ServiceResponse:
        """Renvoie les résultats : utile pour un agent conversationnel."""
        mgr = _get_manager(hass, call, manager)
        results = await mgr.async_search(
            call.data[ATTR_QUERY], limit=call.data.get(ATTR_LIMIT, 5)
        )
        covers = call.data.get(ATTR_COVERS) or mgr.default_covers
        return {"recipes": [_recipe_to_dict(r, covers) for r in results]}

    async def handle_suggest(call: ServiceCall) -> ServiceResponse:
        """Suggère des recettes via l'IA (ai_task) puis recherche sur Jow."""
        mgr = _get_manager(hass, call, manager)
        results = await mgr.async_suggest(
            criteria=call.data.get(ATTR_CRITERIA, ""),
            covers=call.data.get(ATTR_COVERS),
            limit=call.data.get(ATTR_LIMIT, 5),
            weather_entity=call.data.get(CONF_WEATHER_ENTITY),
            ai_entity=call.data.get(CONF_AI_ENTITY),
            weekday=call.data.get(ATTR_WEEKDAY),
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
            ai_prompt=call.data.get("ai_prompt", ""),
            overwrite=call.data.get("overwrite", True),
            max_calories=call.data.get("max_calories"),
            max_total_time=call.data.get("max_total_time"),
            rescue_expiry=call.data.get("rescue_expiry", False),
        )
        return {"recipes": results}

    async def handle_sync_profile(call: ServiceCall) -> ServiceResponse:
        """Récupère le profil Jow de l'utilisateur connecté."""
        mgr = _get_manager(hass, call, manager)
        profile = await mgr.async_get_jow_profile()
        if profile is None:
            return {"error": "Non authentifié ou token invalide"}
        return {"profile": profile}

    async def handle_sync_favorites(call: ServiceCall) -> ServiceResponse:
        """Récupère les recettes favorites du compte Jow et les met en cache.

        Un token absent/invalide (après refresh tenté) est signalé dans la
        réponse — la carte peut afficher « token Jow requis » au lieu de
        « aucun favori », deux diagnostics très différents.
        """
        mgr = _get_manager(hass, call, manager)
        if not mgr.is_authenticated:
            return {"recipes": [], "count": 0, "error": "token_jow_absent"}
        favorites = await mgr.async_get_jow_favorites()
        if not favorites and mgr.jow_token:
            # refresh tenté par _async_jow_get ; re-tester le profil pour
            # distinguer « compte sans favoris » de « auth toujours KO »
            profile = await mgr.async_get_jow_profile()
            if profile is None:
                return {"recipes": [], "count": 0, "error": "auth_echouee"}
        mgr.favorites = favorites
        await mgr.async_save_favorites()
        # Emettre un signal pour mettre a jour les capteurs
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        async_dispatcher_send(hass, mgr.update_signal)
        return {"recipes": favorites, "count": len(favorites)}

    async def handle_sync_preferences(call: ServiceCall) -> ServiceResponse:
        """Synchronise allergies et préférences depuis le compte Jow."""
        mgr = _get_manager(hass, call, manager)
        await mgr.async_sync_preferences_from_jow()
        return {
            "allergies": mgr.allergies,
            "preferences": mgr.preferences,
        }

    async def handle_meal_done(call: ServiceCall) -> ServiceResponse:
        """Marque un repas comme fait et retire les ingrédients de la liste."""
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        result = await mgr.async_meal_done(day)
        if result is None:
            return {"error": "Aucun repas planifié pour cette date"}
        return result

    async def handle_copy_meal(call: ServiceCall) -> ServiceResponse:
        """Copie un repas d'un jour vers un autre."""
        mgr = _get_manager(hass, call, manager)
        from_day = _resolve_date(mgr, call)
        to_day = _resolve_to_date(mgr, call)
        result = await mgr.async_copy_meal(from_day, to_day)
        if result is None:
            return {"error": "Aucun repas planifié sur la date source"}
        return result

    async def handle_set_covers(call: ServiceCall) -> ServiceResponse:
        """Change le nombre de couverts d'un repas planifié."""
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        covers = call.data.get(ATTR_COVERS, 2)
        result = await mgr.async_set_covers(day, covers)
        if result is None:
            return {"error": "Aucun repas planifié pour cette date"}
        return result

    async def handle_exclude_ingredient(call: ServiceCall) -> ServiceResponse:
        """Retire un ingrédient de la liste de courses (déjà en stock)."""
        mgr = _get_manager(hass, call, manager)
        ingredient = call.data.get(ATTR_INGREDIENT, "")
        if not ingredient:
            return {"error": "Aucun ingrédient spécifié"}
        result = await mgr.async_exclude_ingredient(ingredient)
        return result

    async def handle_get_context(call: ServiceCall) -> ServiceResponse:
        """Retourne le contexte IA complet (allergies, préférences, plats récents)."""
        mgr = _get_manager(hass, call, manager)
        from datetime import date as _date
        from datetime import timedelta as _td
        cutoff = (_date.today() - _td(weeks=4)).isoformat()
        recent = []
        for day_iso, meal in mgr.plan.items():
            if meal and meal.get("name") and day_iso >= cutoff:
                recent.append({
                    "name": meal["name"],
                    "date": day_iso,
                    "excluded": not meal.get("_no_exclude", False),
                })
        excluded = []
        if mgr.jow_token:
            excluded = await mgr.async_get_excluded_ingredients()
        return {
            "allergies": mgr.allergies or "",
            "preferences": mgr.preferences or "",
            "excluded_ingredients": excluded,
            "banned_ingredients": mgr.banned_ingredients,
            "avoid_ingredients": mgr.avoid_ingredients,
            "recent_meals": recent,
            "jow_connected": bool(mgr.jow_token),
            "default_covers": mgr.default_covers,
        }

    async def handle_reset_rejects(call: ServiceCall) -> ServiceResponse:
        """Vide la mémoire des rejets (l'import et les suggestions repartent propres)."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_reset_rejects()

    async def handle_clear_recent(call: ServiceCall) -> ServiceResponse:
        """Retire un plat de l'anti-répétition (pourra être re-proposé)."""
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        result = await mgr.async_clear_recent(day.isoformat())
        return result

    async def handle_add_avoid(call: ServiceCall) -> ServiceResponse:
        """Ajoute ou retire un ingrédient à éviter (préférence)."""
        mgr = _get_manager(hass, call, manager)
        ingredient = call.data.get("ingredient", "")
        action = call.data.get("action", "add")
        if action == "remove":
            return await mgr.async_remove_avoid_ingredient(ingredient)
        return await mgr.async_add_avoid_ingredient(ingredient)

    async def handle_add_banned(call: ServiceCall) -> ServiceResponse:
        """Ajoute ou retire un ingrédient interdit (allergie)."""
        mgr = _get_manager(hass, call, manager)
        ingredient = call.data.get("ingredient", "")
        action = call.data.get("action", "add")
        if action == "remove":
            return await mgr.async_remove_banned_ingredient(ingredient)
        return await mgr.async_add_banned_ingredient(ingredient)

    async def handle_sync_calories(call: ServiceCall) -> ServiceResponse:
        """Récupère les calories manquantes pour tous les repas planifiés."""
        mgr = _get_manager(hass, call, manager)
        updated = await mgr.async_sync_calories(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        return {"updated": updated}

    async def handle_send_menu(call: ServiceCall) -> ServiceResponse:
        """Envoie le planning au menu du compte Jow (fusion, sans écraser)."""
        mgr = _get_manager(hass, call, manager)
        sent = await mgr.async_send_menu_to_jow(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        if sent:
            return {"sent": sent, "message": f"{sent} plats ajoutés au menu Jow"}
        return {"sent": 0, "message": "Menu Jow déjà à jour (aucun plat à ajouter)"}

    async def handle_expiring(call: ServiceCall) -> ServiceResponse:
        """Liste les ingrédients périssables du planning qui expirent bientôt."""
        mgr = _get_manager(hass, call, manager)
        within = call.data.get("within_days", 3)
        return {"expiring": mgr.expiring_ingredients(within_days=within)}

    async def handle_recommendations(call: ServiceCall) -> ServiceResponse:
        """Recommandations natives du moteur Jow (sans agent IA)."""
        mgr = _get_manager(hass, call, manager)
        if not mgr.is_authenticated:
            return {"recipes": [], "count": 0, "error": "token_jow_absent"}
        recipes = await mgr.async_jow_recommendations(
            count=call.data.get(ATTR_LIMIT, 10),
        )
        return {"recipes": recipes, "count": len(recipes)}

    async def handle_import_token(call: ServiceCall) -> ServiceResponse:
        """Importe un refresh token (et son cookie) obtenus par le script
        jow-marchand (login enseigne en navigateur réel, MFA saisi main).

        Met à jour les options de l'entry et force le refresh : la session
        magasin capturée par le script devient la session de HA.
        """
        mgr = _get_manager(hass, call, manager)
        rt = call.data.get("refresh_token", "").strip()
        if not rt:
            return {"error": "refresh_token_requis"}
        mgr.jow_refresh_token = rt
        # cookie éventuel : l'injecter dans le jar du client
        cookie = call.data.get("session_cookie")
        if cookie:
            client = mgr.api_client()
            client._session.cookies.set("JowSession", cookie, domain=".jow.fr")
        # refresh immédiat : la session devient active dans HA
        ok = await mgr.async_refresh_jow_token()
        await mgr._async_persist_tokens()
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        async_dispatcher_send(hass, mgr.update_signal)
        return {"imported": bool(ok), "session_magasin": bool(cookie)}

    async def handle_order_providers(call: ServiceCall) -> ServiceResponse:
        """Liste les fournisseurs de courses partenaires (Intermarché…)."""
        mgr = _get_manager(hass, call, manager)
        from .order import JowOrderManager

        om = JowOrderManager(mgr.api_client())
        providers = await om.get_providers()
        return {"providers": [
            {k: p.get(k) for k in ("id", "name", "deliverySubtitle", "disabled")}
            for p in providers
        ]}

    async def handle_order_slots(call: ServiceCall) -> ServiceResponse:
        """Créneaux de livraison du magasin configuré."""
        mgr = _get_manager(hass, call, manager)
        from .order import JowOrderManager

        om = JowOrderManager(mgr.api_client())
        return await om.get_delivery_slots()

    async def handle_order_cart(call: ServiceCall) -> ServiceResponse:
        """Prépare le panier fournisseur depuis la liste ouverte (sans paiement)."""
        mgr = _get_manager(hass, call, manager)
        from .order import JowOrderManager

        om = JowOrderManager(mgr.api_client())
        return await om.prepare_cart_from_menu()

    async def handle_order_create(call: ServiceCall) -> ServiceResponse:
        """Crée la commande (non payée) — visible sur jow.fr."""
        mgr = _get_manager(hass, call, manager)
        from .order import JowOrderManager

        om = JowOrderManager(mgr.api_client())
        return await om.create_order()

    async def handle_order_pay(call: ServiceCall) -> ServiceResponse:
        """PAIEMENT RÉEL — exige confirm: true explicite (aucune automatisation possible sans)."""
        mgr = _get_manager(hass, call, manager)
        from .order import JowOrderManager

        om = JowOrderManager(mgr.api_client())
        return await om.pay_order(
            order_id=call.data.get("order_id", ""),
            confirm=call.data.get("confirm", False),
        )

    async def handle_collections_list(call: ServiceCall) -> ServiceResponse:
        """Liste les collections de recettes du compte Jow."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_list_collections()

    async def handle_collection_create(call: ServiceCall) -> ServiceResponse:
        """Crée une collection dans le compte Jow."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_create_collection(
            title=call.data["title"],
            is_private=call.data.get("is_private", True),
        )

    async def handle_collection_add_recipe(call: ServiceCall) -> ServiceResponse:
        """Ajoute une recette (par id, ou le plat d'un jour du planning) à des collections."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_collection_add_recipe(
            collections_ids=call.data["collections"],
            recipe_id=call.data.get("recipe_id"),
            weekday=call.data.get(ATTR_WEEKDAY),
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
        )

    async def handle_export_week(call: ServiceCall) -> ServiceResponse:
        """Livre le planning de la semaine dans une collection jow.fr."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_export_week(
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
            title=call.data.get("title"),
            is_private=call.data.get("is_private", True),
        )

    async def handle_collection_import(call: ServiceCall) -> ServiceResponse:
        """Importe une collection Jow sur les jours vides du planning HA."""
        mgr = _get_manager(hass, call, manager)
        return await mgr.async_import_collection(
            collection_id=call.data["collection_id"],
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
        )

    async def handle_uploaded_recipes(call: ServiceCall) -> ServiceResponse:
        """Liste les recettes maison du compte (créées via l'app mobile)."""
        mgr = _get_manager(hass, call, manager)
        recipes = await mgr.async_get_uploaded_recipes()
        return {"recipes": [
            {"id": r.get("id"), "name": r.get("name"), "url": r.get("url")}
            for r in recipes
        ], "count": len(recipes)}

    async def handle_import_menu(call: ServiceCall) -> ServiceResponse:
        """Importe le menu de la semaine depuis le compte Jow (app/mobile)."""
        mgr = _get_manager(hass, call, manager)
        result = await mgr.async_import_menu_from_jow(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        return result

    # Enregistrement des services (toujours ré-enregistrer pour que les
    # handlers pointent vers le manager courant après reload ; _get_manager
    # résout l'instance via entry_name).
    hass.services.async_register(
        DOMAIN, SERVICE_PLAN_MEAL, handle_plan_meal,
        schema=vol.Schema({
            vol.Required(ATTR_QUERY): cv.string,
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(ATTR_CHOICE, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Optional("recipe_id"): cv.string,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_MEAL, handle_clear_meal,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_WEEK, handle_clear_week,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional("remember_rejects", default=True): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ORDER_PROVIDERS, handle_order_providers,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ORDER_SLOTS, handle_order_slots,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ORDER_CART, handle_order_cart,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ORDER_CREATE, handle_order_create,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_REJECTS, handle_reset_rejects,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_TOKEN, handle_import_token,
        schema=vol.Schema({
            vol.Required("refresh_token"): cv.string,
            vol.Optional("session_cookie"): cv.string,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COLLECTIONS_LIST, handle_collections_list,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COLLECTION_CREATE, handle_collection_create,
        schema=vol.Schema({
            vol.Required("title"): cv.string,
            vol.Optional("is_private", default=True): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COLLECTION_ADD_RECIPE, handle_collection_add_recipe,
        schema=vol.Schema({
            vol.Required("collections"): vol.All(cv.ensure_list_csv, [cv.string]),
            vol.Optional("recipe_id"): cv.string,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_WEEK, handle_export_week,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional("title"): cv.string,
            vol.Optional("is_private", default=True): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COLLECTION_IMPORT, handle_collection_import,
        schema=vol.Schema({
            vol.Required("collection_id"): cv.string,
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPLOADED_RECIPES, handle_uploaded_recipes,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ORDER_PAY, handle_order_pay,
        schema=vol.Schema({
            vol.Required("order_id"): cv.string,
            vol.Required("confirm", default=False): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECOMMENDATIONS, handle_recommendations,
        schema=vol.Schema({
            vol.Optional(ATTR_LIMIT, default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RENEW_WEEK, handle_renew_week,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_CRITERIA): cv.string,
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(CONF_WEATHER_ENTITY): cv.string,
            vol.Optional(CONF_AI_ENTITY): cv.string,
            vol.Optional("ai_prompt"): cv.string,
            vol.Optional("max_calories"): vol.All(vol.Coerce(int), vol.Range(min=100, max=2000)),
            vol.Optional("max_total_time"): vol.All(vol.Coerce(int), vol.Range(min=5, max=240)),
            vol.Optional("day_criteria"): {vol.In(WEEKDAYS): cv.string},
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_SHOPPING_LIST, handle_refresh_list,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional("keep_checked", default=True): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, handle_search,
        schema=vol.Schema({
            vol.Required(ATTR_QUERY): cv.string,
            vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SUGGEST, handle_suggest,
        schema=vol.Schema({
            vol.Optional(ATTR_CRITERIA): cv.string,
            vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(CONF_WEATHER_ENTITY): cv.string,
            vol.Optional(CONF_AI_ENTITY): cv.string,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
            vol.Optional("ai_prompt"): cv.string,
            vol.Optional("overwrite", default=True): cv.boolean,
            vol.Optional("max_calories"): vol.All(vol.Coerce(int), vol.Range(min=100, max=2000)),
            vol.Optional("max_total_time"): vol.All(vol.Coerce(int), vol.Range(min=5, max=240)),
            vol.Optional("rescue_expiry", default=False): cv.boolean,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PROFILE, handle_sync_profile,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_FAVORITES, handle_sync_favorites,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PREFERENCES, handle_sync_preferences,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MEAL_DONE, handle_meal_done,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COPY_MEAL, handle_copy_meal,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_TO_DATE): cv.date,
            vol.Optional(ATTR_TO_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_TO_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_COVERS, handle_set_covers,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Required(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXCLUDE_INGREDIENT, handle_exclude_ingredient,
        schema=vol.Schema({
            vol.Required(ATTR_INGREDIENT): cv.string,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_CONTEXT, handle_get_context,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_RECENT, handle_clear_recent,
        schema=vol.Schema({
            vol.Optional("date"): cv.string,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_AVOID, handle_add_avoid,
        schema=vol.Schema({
            vol.Required("ingredient"): cv.string,
            vol.Optional("action", default="add"): vol.In(["add", "remove"]),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_BANNED, handle_add_banned,
        schema=vol.Schema({
            vol.Required("ingredient"): cv.string,
            vol.Optional("action", default="add"): vol.In(["add", "remove"]),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_CALORIES, handle_sync_calories,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MENU, handle_send_menu,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_MENU, handle_import_menu,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXPIRING, handle_expiring,
        schema=vol.Schema({
            vol.Optional("within_days", default=3): vol.All(vol.Coerce(int), vol.Range(min=1, max=14)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge l'intégration."""
    manager: JowManager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if manager and manager._token_refresh_cancel:
            manager._token_refresh_cancel()
        if manager and getattr(manager, "_purge_cancel", None):
            manager._purge_cancel()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_PLAN_MEAL,
                SERVICE_CLEAR_MEAL,
                SERVICE_CLEAR_WEEK,
                SERVICE_REFRESH_SHOPPING_LIST,
                SERVICE_SEARCH,
                SERVICE_SUGGEST,
                SERVICE_SYNC_PROFILE,
                SERVICE_SYNC_FAVORITES,
                SERVICE_SYNC_PREFERENCES,
                SERVICE_MEAL_DONE,
                SERVICE_SYNC_CALORIES,
                SERVICE_SEND_MENU,
                SERVICE_IMPORT_MENU,
                SERVICE_EXPIRING,
                SERVICE_RENEW_WEEK,
                SERVICE_RECOMMENDATIONS,
                SERVICE_ORDER_PROVIDERS,
                SERVICE_ORDER_SLOTS,
                SERVICE_ORDER_CART,
                SERVICE_ORDER_CREATE,
                SERVICE_ORDER_PAY,
                            SERVICE_IMPORT_TOKEN,
                SERVICE_RESET_REJECTS,
                SERVICE_COLLECTIONS_LIST,
                SERVICE_COLLECTION_CREATE,
                SERVICE_COLLECTION_ADD_RECIPE,
                SERVICE_COLLECTION_IMPORT,
                SERVICE_EXPORT_WEEK,
                SERVICE_UPLOADED_RECIPES,
                SERVICE_COPY_MEAL,
                SERVICE_SET_COVERS,
                SERVICE_EXCLUDE_INGREDIENT,
                SERVICE_GET_CONTEXT,
                SERVICE_CLEAR_RECENT,
                SERVICE_ADD_AVOID,
                SERVICE_ADD_BANNED,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


