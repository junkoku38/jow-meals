# Bookmarklet Jow → Home Assistant

## Installation

### 1. Configurer HA_URL et HA_TOKEN

Éditez le bookmarklet ci-dessous et remplacez :
- `HA_URL` : l'URL de votre Home Assistant (ex: `http://VOTRE_IP:8123`)
- `HA_TOKEN` : un jeton d'accès longue durée HA (Paramètres → Compte → Jetons d'accès longue durée → Créer)

### 2. Ajouter le bookmarklet

1. Affichez la barre de favoris dans votre navigateur
2. Clic droit sur la barre → "Ajouter une page" / "Ajouter aux favoris"
3. Nom : `Jow → HA`
4. URL : collez le code ci-dessous (en une seule ligne)

```
javascript:(function(){var HA_URL='https://VOTRE_NABU_CASA.ui.nabu.casa';var GOOGLE_CLIENT_ID='908774541818-9tvsq3p6lggasvdnc3iq6i4mckf9e3ru.apps.googleusercontent.com';var d=document.createElement('div');d.style.cssText='position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:999999;background:#1a1816;color:#f2efe9;padding:30px;border-radius:14px;box-shadow:0 8px 32px rgba(0,0,0,0.5);font-family:system-ui,sans-serif;min-width:320px;text-align:center';d.innerHTML='<h2 style="margin:0 0 10px;font-size:1.3rem">Connexion Jow → HA</h2><p style="color:#a39d93;font-size:0.85rem;margin-bottom:20px">Cliquez ci-dessous pour vous connecter avec Google et envoyer le token Jow à Home Assistant.</p><div id="g_id_onload" data-client_id="'+GOOGLE_CLIENT_ID+'" data-callback="jowHaHandleCredential" data-auto_prompt="false"></div><div id="g_idsignin" class="g_id_signin" data-type="standard" data-size="large" data-theme="outline" data-text="continue_with" data-shape="rectangular" data-locale="fr"></div><div id="jowHaStatus" style="margin-top:15px;font-size:0.85rem;color:#a39d93;min-height:1.5em"></div><button onclick="this.parentElement.remove()" style="margin-top:15px;background:none;border:1px solid #4a443c;color:#a39d93;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:0.8rem">Fermer</button>';document.body.appendChild(d);var s=document.createElement('script');s.src='https://accounts.google.com/gsi/client';s.async=true;s.defer=true;document.head.appendChild(s);window.jowHaHandleCredential=function(response){var st=document.getElementById('jowHaStatus');st.textContent='Redirection vers Home Assistant...';st.style.color='#a39d93';window.location.href=HA_URL+'/api/jow/google_callback?credential='+encodeURIComponent(response.credential);};})();
```

## Utilisation

1. Allez sur [jow.fr](https://jow.fr)
2. Connectez-vous à votre compte (Courses U, Carrefour, etc.)
3. Une fois connecté, cliquez sur le bookmarklet **Jow → HA**
4. Le message "Token Jow envoyé à HA avec succès !" confirme la connexion
5. Le token est valide 48h et rafraîchi automatiquement

## Renouvellement

Quand le token expire (après 48h ou si la session provider est expirée) :
1. Retournez sur jow.fr
2. Reconnectez-vous si nécessaire
3. Cliquez à nouveau sur le bookmarklet

## Alternative manuelle

Si le bookmarklet ne fonctionne pas :

1. Sur jow.fr → **F12 → Network** (onglet Réseau)
2. Rechargez la page
3. Cliquez sur une requête vers `api.jow.fr`
4. Dans **Request Headers**, copiez le header `Authorization: Bearer eyJ...`
5. Envoyez-le à HA :

```bash
curl -X POST http://VOTRE_HA:8123/api/jow/token \
  -H "Authorization: Bearer VOTRE_TOKEN_HA" \
  -H "Content-Type: application/json" \
  -d '{"token": "eyJ..."}'
```