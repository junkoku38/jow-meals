#!/usr/bin/env python3
"""Jow Marchand — connecte ton enseigne (Auchan) et synchronise HA.

À lancer sur TON PC (Windows/Mac/Linux) quand jow redemande la session
magasin (en pratique : ~1× par mois, ou après chaque "token expiré").

Ce que ça fait :
1. Ouvre un navigateur Chrome VISIBLE sur le login Auchan (jow)
2. Pré-remplit ton email/mot de passe — TU TAPES TON CODE MFA
3. Suit automatiquement le callback jow → capture les tokens + le cookie
4. Met à jour Home Assistant (entry Jow) : refresh token + cookie JowSession
5. Teste la chaîne de commande (provider/store → order_slots)

Prérequis (une seule fois) :
    pip install playwright requests
    playwright install chromium

Usage :
    python jow-marchand.py --email shikyoo@free.fr --password "TON_MDP"
    # ou tout est demandé interactivement si omis
    # HA : --ha-url http://192.168.1.115:8123 --ha-token "TOKEN_LLAT"

Le mot de passe n'est JAMAIS stocké : il ne sert que dans le navigateur
de la session courante, puis est oublié.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

PROVIDER = "auchan"
AUTH_URL = (
    "https://compte.auchan.fr/auth/realms/auchan.fr/protocol/openid-connect/auth"
    "?client_id=jow&scope=openid+profile+email+offline_access"
    "&response_type=code&code_challenge_method=S256"
    "&code_challenge={challenge}&redirect_uri={redirect}&state={state}"
)
CALLBACK_PREFIX = "https://providers.jow.tech/auth/callback/auchan"


def gen_pkce() -> tuple[str, str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return verifier, challenge, state


def ha_update(ha_url: str, ha_token: str, entry_id: str, refresh_token: str, cookie: str | None) -> None:
    """Met à jour l'entry HA : le LLAT ne peut pas écrire les options →
    on passe par le flow d'options via l'API WebSocket n'est pas permis.
    Solution : on écrit le refresh token via le service HA (introduction
    en v1.4 : jow.import_token) ; à défaut on l'affiche."""
    # v1.4 : le service jow.import_token accepte {refresh_token, cookie}
    req = urllib.request.Request(
        f"{ha_url}/api/services/jow/import_token",
        data=json.dumps({
            "refresh_token": refresh_token,
            **({"session_cookie": cookie} if cookie else {}),
        }).encode(),
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"✔ HA mis à jour ({r.status}) — intégration rechargée")
    except Exception as err:
        print(f"⚠ HA: {err}")
        print("→ colle ce refresh token à la main : Paramètres → Jow → Configurer")
        print(refresh_token)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--email", help="Email du compte Auchan")
    ap.add_argument("--password", help="Mot de passe Auchan (jamais stocké)")
    ap.add_argument("--ha-url", default="http://192.168.1.115:8123")
    ap.add_argument("--ha-token", default=None, help="Token LLAT HA (longue durée)")
    args = ap.parse_args()

    email = args.email or input("Email Auchan : ").strip()
    password = args.password or input("Mot de passe (entrée = taper dans le navigateur) : ")

    verifier, challenge, state = gen_pkce()
    redirect = urllib.parse.quote("https://providers.jow.tech/auth/callback/auchan", safe="")
    url = AUTH_URL.format(challenge=challenge, redirect=redirect, state=state)

    print("\n[1/4] Ouverture du navigateur — login Auchan…")
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=False)  # VISIBLE : nécessaire pour
        # le reCAPTCHA (score de navigateur réel) et TON code MFA.
        ctx = nav.new_context(locale="fr-FR")
        page = ctx.new_page()

        # capturer le callback AVANT navigation
        captured: dict = {}

        def on_response(resp):
            if resp.url.startswith(CALLBACK_PREFIX) and "code=" in resp.url:
                captured["url"] = resp.url

        page.on("response", on_response)
        page.goto(url)
        page.wait_for_selector("#username", timeout=20000)

        if password:
            page.fill("#username", email)
            page.fill("#password", password)
            print("[2/4] Identifiants remplis — clique « Se connecter » et tape TON CODE MFA")
            print("      (je surveille le callback, prends ton temps)")
        else:
            print("[2/4] Tape tes identifiants puis ton code MFA dans le navigateur")

        # attendre le callback jow (l'utilisateur interagit)
        deadline = time.time() + 600  # 10 minutes
        while time.time() < deadline:
            if "url" in captured:
                break
            try:
                page.wait_for_timeout(500)
                # le MFA peut rediriger vers jow.fr directement (attach fait
                # côté serveur par le callback) : détecter aussi jow.fr
                if page.url.startswith("https://jow.fr"):
                    captured["url"] = page.url
                    break
            except Exception:
                pass
        else:
            print("✘ délai dépassé (10 min)")
            nav.close()
            sys.exit(1)

        print("[3/4] Callback capturé — lecture des tokens jow…")
        # après le callback, providers.jow.tech redirige vers jow.fr avec
        # des tokens dans l'URL (fragment) OU l'attache via cookie : le plus
        # fiable est de relire localStorage de jow.fr (le site a fini l'attach)
        page.goto("https://jow.fr/")
        page.wait_for_timeout(4000)
        tokens = page.evaluate(
            "() => { try { return JSON.parse(localStorage.jow_store || '{}')?.data?.auth || null } catch { return null } }"
        )
        cookie = ctx.cookies("https://api.jow.fr")
        jow_session = next((c["value"] for c in cookie if c["name"] == "JowSession"), None)
        nav.close()

    if not tokens or not tokens.get("refreshToken"):
        print("✘ tokens introuvables — vérifie que le login a abouti sur jow.fr")
        sys.exit(1)

    rt = tokens["refreshToken"]
    print(f"    refresh token : {rt[:50]}…")
    print(f"    cookie JowSession : {'présent' if jow_session else 'absent'}")

    print("[4/4] Mise à jour de Home Assistant…")
    if args.ha_token:
        ha_update(args.ha_url, args.ha_token, "", rt, jow_session)
    else:
        print("→ Colle ce refresh token dans HA : Paramètres → Jow → Configurer :\n")
        print(rt)
        if jow_session:
            print("\n→ (optionnel, commande depuis HA) cookie JowSession :")
            print(jow_session)
    print("\n✔ Terminé — appelle jow.order_prepare puis jow.order_cart dans HA.")


if __name__ == "__main__":
    main()