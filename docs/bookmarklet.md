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
javascript:(function(){var HA_URL='http://VOTRE_IP:8123';var HA_TOKEN='VOTRE_TOKEN_HA';var captured=null;var origSet=XMLHttpRequest.prototype.setRequestHeader;XMLHttpRequest.prototype.setRequestHeader=function(name,value){if(name.toLowerCase()==='authorization'&&value.startsWith('Bearer eyJ')){captured=value.replace('Bearer ','');}return origSet.apply(this,arguments);};var origFetch=window.fetch;window.fetch=function(url,opts){if(url&&url.toString().includes('api.jow.fr')&&opts&&opts.headers){var auth=opts.headers['authorization']||(opts.headers.get&&opts.headers.get('authorization'))||'';if(auth&&auth.startsWith('Bearer eyJ')){captured=auth.replace('Bearer ','');}}return origFetch.apply(this,arguments);};fetch('https://api.jow.fr/public/edito?context=launch').then(function(){setTimeout(function(){XMLHttpRequest.prototype.setRequestHeader=origSet;window.fetch=origFetch;if(!captured){alert('Token non trouve. Etes-vous connecte sur jow.fr ?');return;}fetch(HA_URL+'/api/jow/token',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+HA_TOKEN},body:JSON.stringify({token:captured})}).then(function(r){return r.json();}).then(function(r){if(r.status==='ok'){alert('Token Jow envoye a HA avec succes !');}else{alert('Erreur: '+(r.error||'inconnue'));}}).catch(function(e){alert('Erreur envoi: '+e.message);});},2000);});})();
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