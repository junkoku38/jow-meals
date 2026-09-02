"""Commande d'ingrédients Jow — partenaires (Intermarché, Carrefour…).

ATTENTION CONCEPTION SÉCURITÉ : ces routes déclenchent de vrais achats.
La philosophie de l'intégration :
- LECTURE (providers, créneaux, panier, prix) : libre
- PRÉPARATION (créer le panier externe) : service explicite
- PAIEMENT : service DÉDIÉ (jow.pay_order) avec confirmation requise
  `confirm: true` — JAMAIS d'automatisation possible sans ce flag
  explicite (schéma vol : confirm default False).

Flux documenté (bundle du site) :
1. provider/stores → choisir le magasin (setStore)
2. provider/cart/initial → créer le panier avec les produits
3. provider/delivery/slot/all → créneaux
4. provider/delivery/slot (setSlot) → réserver
5. order (createOrder) → commande en attente de paiement
6. order/{id}/pay → paiement (psp + carte)
7. order/{id}/validate → validation finale

Les routes 3-6 exigent un compte avec magasin configuré : sur le
compte de développement elles répondent 500 (état serveur). Toutes les
méthodes retournent un rapport structuré sans jamais lever.
"""

from __future__ import annotations

import logging

from .api import JOW_API_BASE, JowClient

_LOGGER = logging.getLogger(__name__)


class JowOrderManager:
    """Orchestration de commande côté intégration."""

    def __init__(self, client: JowClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------
    async def get_providers(self) -> list[dict]:
        """Providers de livraison disponibles (nom, id, type)."""
        return await self._client.get_providers()

    async def get_delivery_slots(self) -> dict:
        """Créneaux de livraison (GET /provider/delivery/slot/all).

        Nécessite un magasin configuré sur le compte jow.fr — sinon
        l'API répond 500 : retour {"error": …} plutôt qu'une exception.
        """
        resp = await self._client.get(
            f"{JOW_API_BASE}/provider/delivery/slot/all",
            params={"availabilityZoneId": "FR"},
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}",
                    "aide": "Session marchande requise : connectez l'enseigne "
                            "sur jow.fr. NOTE : les sessions magasin jow sont "
                            "liées au cookie navigateur (sticky session) — la "
                            "commande complète (panier → paiement) doit être "
                            "finalisée sur jow.fr ; HA pilote le menu, pas le "
                            "paiement."}
        try:
            return {"slots": resp.json().get("data", []) or []}
        except ValueError:
            return {"error": "reponse_illisible"}

    # ------------------------------------------------------------------
    # Panier / commande
    # ------------------------------------------------------------------
    async def prepare_cart_from_menu(self) -> dict:
        """Crée le panier fournisseur à partir de la liste ouverte Jow.

        POST /provider/cart/initial — « ajouter les ingrédients de mon
        menu au panier du magasin ». Étape sans engagement de paiement.
        """
        resp = await self._client.post(
            f"{JOW_API_BASE}/provider/cart/initial",
            body={},
            params={"availabilityZoneId": "FR"},
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}"}
        try:
            data = resp.json().get("data", {})
            items = data.get("items", []) or []
            return {
                "items": len(items),
                "total": data.get("totalPrice"),
                "id": data.get("id") or data.get("externalId"),
            }
        except ValueError:
            return {"error": "reponse_illisible"}

    async def get_cart(self) -> dict:
        """Panier fournisseur courant (GET /provider/cart)."""
        resp = await self._client.get(
            f"{JOW_API_BASE}/provider/cart",
            params={"availabilityZoneId": "FR"},
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}"}
        try:
            return resp.json().get("data", {}) or {}
        except ValueError:
            return {"error": "reponse_illisible"}

    async def create_order(self) -> dict:
        """Crée la commande (POST /order) — PAS encore payée.

        Body observé dans le site : meals (depuis le menu sélectionné),
        providerOrderParams, manuallyAddedItems, missingIngredients.
        Sans paiement : la commande reste en attente sur jow.fr.
        """
        # meals : la liste ouverte est la source (cohérence avec le menu)
        letscook = await self._client.get_letscook()
        osl = (letscook or {}).get("openShoppingList") or {}
        meals = [m for m in (osl.get("meals") or []) if isinstance(m, dict)]
        if not meals:
            return {"error": "menu_jow_vide",
                    "aide": "Envoyez d'abord le menu (jow.send_menu)"}
        body_meals = []
        for m in meals:
            r = m.get("recipe") or {}
            rid = r.get("id") or r.get("_id")
            if rid:
                body_meals.append({
                    "recipe": rid,
                    "source": m.get("source") or "jow",
                    "coversCount": m.get("coversCount") or 2,
                })
        body = {"meals": body_meals, "providerOrderParams": {"enableSubstitution": True}}
        resp = await self._client.post(
            f"{JOW_API_BASE}/order", body=body, params={"availabilityZoneId": "FR"}
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}"}
        try:
            data = resp.json().get("data", {})
            return {"order_id": data.get("id") or data.get("_id"),
                    "total": data.get("totalPrice") or data.get("price"),
                    "state": data.get("state") or data.get("status")}
        except ValueError:
            return {"error": "reponse_illisible"}

    async def pay_order(self, order_id: str, confirm: bool = False) -> dict:
        """PAIEMENT RÉEL — service jow.order_pay avec confirm explicite.

        Le schéma vol du service exige confirm: true : aucune
        automatisation ne peut payer sans ce flag écrit noir sur blanc.

        Flux du site : pay → validate (la validation confirme la
        commande côté jow.fr après le paiement). Elle est tentée
        automatiquement après un pay 200 — si elle échoue, la réponse
        le signale (order_id retourné pour rejouer validate).
        """
        if not confirm:
            return {"error": "confirmation_requise",
                    "aide": "Passez confirm: true — ceci déclenche un vrai paiement"}
        if not order_id:
            return {"error": "order_id_manquant"}
        resp = await self._client.post(
            f"{JOW_API_BASE}/order/{order_id}/pay",
            body={},
            params={"availabilityZoneId": "FR"},
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}"}
        try:
            data = resp.json().get("data", {}) or {}
        except ValueError:
            return {"error": "reponse_illisible"}

        # Validation post-paiement (best effort — signalée si refusée)
        validation = await self.validate_order(order_id)
        result = dict(data)
        result["order_id"] = order_id
        if validation.get("error"):
            result["validation"] = f"à faire ({validation['error']})"
        else:
            result["validation"] = "ok"
        return result

    async def validate_order(self, order_id: str) -> dict:
        """POST /order/{id}/validate — confirme la commande après paiement."""
        if not order_id:
            return {"error": "order_id_manquant"}
        resp = await self._client.post(
            f"{JOW_API_BASE}/order/{order_id}/validate",
            body={},
            params={"availabilityZoneId": "FR"},
        )
        if resp is None:
            return {"error": "token_jow_absent"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}"}
        try:
            return resp.json().get("data", {}) or {}
        except ValueError:
            return {"error": "reponse_illisible"}
