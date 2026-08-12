# Bookmarklet Jow pour Home Assistant

## Installation

1. Ajoutez cette URL à vos favoris (clic droit sur la barre de favoris → "Ajouter une page") :

```
javascript:(function(){var token='';var origFetch=window.fetch;window.fetch=function(url,opts){if(typeof url==='string'&&url.includes('api.jow.fr')&&opts&&opts.headers){var auth=opts.headers['authorization']||opts.headers['Authorization'];if(auth&&auth.startsWith('Bearer eyJ')){token=auth.replace('Bearer ','');window.fetch=origFetch;}}return origFetch.apply(this,arguments);};var xhr=XMLHttpRequest.prototype.open;var origSet=XMLHttpRequest.prototype.setRequestHeader;XMLHttpRequest.prototype.setRequestHeader=function(name,value){if(name.toLowerCase()==='authorization'&&value.startsWith('Bearer eyJ')){token=value.replace('Bearer ','');}return origSet.apply(this,arguments);};fetch('https://api.jow.fr/public/profile',{headers:{'authorization':'Bearer '+getToken()}}).then(r=>r.json()).then(d=>{if(d.data){fetch('http://HA_URL:8123/api/jow/token',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+getToken()},body:JSON.stringify({token:token})}).then(r=>r.json()).then(r=>{alert(r.status==='ok'?'Token envoyé à HA avec succès !':'Erreur: '+r.error);}).catch(e=>alert('Erreur envoi: '+e));}else{alert('Non connecté à Jow. Connectez-vous d\'abord.');}});function getToken(){var s=document.cookie.match(/JowSession=([^;]+)/);return s?s[1]:'';};function scanReqs(){var performance=window.performance;if(performance){var entries=performance.getEntriesByType('resource');for(var i=0;i<entries.length;i++){if(entries[i].name.includes('api.jow.fr')){break;}}}};scanReqs();})();
```

2. Remplacez `HA_URL` par l'IP de votre Home Assistant (ex: `VOTRE_IP`)

## Utilisation

1. Allez sur [jow.fr](https://jow.fr) et connectez-vous à votre compte Courses U
2. Une fois connecté, cliquez sur le bookmarklet "Jow HA"
3. Le token JWT est envoyé automatiquement à Home Assistant
4. Une notification confirme la réception du token

Le token est valide 48h et rafraîchi automatiquement par le plugin.

## Alternative : capture manuelle

Si le bookmarklet ne fonctionne pas :

1. Sur jow.fr → **F12 → Network**
2. Cliquez sur une requête vers `api.jow.fr`
3. Copiez le header `Authorization: Bearer eyJ...`
4. Dans HA : **Paramètres → Appareils et services → Jow → Configurer → Token Jow JWT**
5. Collez le token

## Version simplifiée du bookmarklet

```
javascript:(function(){var auth='';for(var i=0;i<performance.getEntriesByType('resource').length;i++){var e=performance.getEntriesByType('resource')[i];if(e.name.includes('api.jow.fr/public/edito')){break;}}fetch('https://api.jow.fr/public/profile',{headers:{'authorization':'Bearer '+document.cookie.match(/JowSession=([^;]+)/)[1]}}).then(r=>r.json()).then(d=>{if(d.data){alert('Connecté: '+d.data.firstName);}});fetch('/api/jow/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:prompt('Token JWT Jow:','')})}).then(r=>r.json()).then(r=>alert(r.status==='ok'?'OK!':'Erreur'));})();
```