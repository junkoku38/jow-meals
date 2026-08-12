# Bookmarklet Jow → Home Assistant

## Installation

### 1. Configurer HA_URL et HA_TOKEN

Éditez le bookmarklet ci-dessous et remplacez :
- `HA_URL` : l'URL de votre Home Assistant (ex: `http://VOTRE_IP:8123`)
- `HA_TOKEN` : un jeton d'accès longue durée HA (Paramètres → Compte → Jetons d'accès longue durée → Créer)

> ⚠️ **Ne committez jamais votre URL Nabu Casa ou votre token HA**. Gardez-les
> uniquement dans votre bookmarklet local.

### 2. Ajouter le bookmarklet

1. Affichez la barre de favoris dans votre navigateur
2. Clic droit sur la barre → "Ajouter une page" / "Ajouter aux favoris"
3. Nom : `Jow → HA`
4. URL : collez le code ci-dessous (en une seule ligne)

```
javascript:(function(){var HA_URL='http://VOTRE_IP:8123';var HA_TOKEN='VOTRE_TOKEN_HA';function findTokens(){var refreshToken=null;var accessToken=null;try{for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);var v=localStorage.getItem(k);if(v){try{var p=JSON.parse(v);if(p&&typeof p==='object'){if(p.refreshToken&&p.refreshToken.startsWith('eyJ'))refreshToken=p.refreshToken;if(p.accessToken&&p.accessToken.startsWith('eyJ'))accessToken=p.accessToken;}}catch(e){}if(v.startsWith('eyJ')){try{var parts=v.split('.');var payload=parts[1];payload+='='.repeat(4-payload.length%4);var claims=JSON.parse(atob(payload));if(claims.type==='refresh')refreshToken=v;else if(claims.type==='access')accessToken=v;}catch(e){}}}}}catch(e){}return{refreshToken:refreshToken,accessToken:accessToken};}function sendTokens(tokens){if(!tokens.refreshToken&&!tokens.accessToken){alert('Aucun token trouve. Connectez-vous sur jow.fr puis re-cliquez.');return;}var xhr=new XMLHttpRequest();xhr.open('POST',HA_URL+'/api/jow/token',true);xhr.setRequestHeader('Content-Type','application/json');xhr.setRequestHeader('Authorization','Bearer '+HA_TOKEN);xhr.onload=function(){try{var r=JSON.parse(xhr.responseText);if(r.status==='ok'){alert('Token Jow envoye a HA avec succes !');}else{alert('Erreur: '+(r.error||'inconnue'));}}catch(e){alert('Reponse: '+xhr.responseText);}};xhr.onerror=function(){alert('Erreur reseau. HA accessible ?');};var body={};if(tokens.accessToken)body.token=tokens.accessToken;if(tokens.refreshToken)body.refresh_token=tokens.refreshToken;if(!body.token&&tokens.refreshToken)body.token=tokens.refreshToken;xhr.send(JSON.stringify(body));}var tokens=findTokens();if(tokens.refreshToken||tokens.accessToken){sendTokens(tokens);return;}alert('Aucun token trouve dans localStorage. Connectez-vous sur jow.fr avec Google puis re-cliquez.');})();
```

## Utilisation

1. Allez sur [jow.fr](https://jow.fr)
2. Connectez-vous à votre compte (Courses U, Carrefour, etc.)
3. Une fois connecté, cliquez sur le bookmarklet **Jow → HA**
4. Le message "Token Jow envoyé à HA avec succès !" confirme la connexion
5. Le refresh token est valide ~6 mois et rafraîchi automatiquement toutes les 24h

## Renouvellement

Quand le refresh token expire (après ~6 mois) :
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