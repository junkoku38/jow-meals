"""Page web d'authentification guidée jow (sans script python).

Enregistrée sur /api/jow/auth par l'intégration. Remplace le script
scripts/jow_marchand.py pour l'usage courant : l'utilisateur s'authentifie
sur jow.fr dans SON navigateur (MFA inclus), colle son refresh token dans
la page, et l'intégration importe la session (service jow.import_token).

La page fournit aussi le cookie JowSession de HA (option cookie partagé
pour activer les services order_*).
"""

from __future__ import annotations

import logging

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jow — authentification magasin</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111418;color:#e8e8e8;margin:0;padding:2rem}
 .box{max-width:680px;margin:0 auto}
 h1{font-size:1.3rem} h2{font-size:1rem;margin-top:1.6rem;color:#9fb3c8}
 .card{background:#1a1f26;border:1px solid #2a3038;border-radius:12px;padding:1.2rem;margin:1rem 0}
 ol{line-height:1.7} code{background:#0c0f12;padding:2px 6px;border-radius:4px;font-size:.85em}
 .btn{display:inline-block;background:#d8a25a;color:#111;border:none;border-radius:8px;padding:.6rem 1.1rem;font-size:.95rem;cursor:pointer;text-decoration:none;font-weight:600}
 .btn.ghost{background:#2a3038;color:#e8e8e8}
 input[type=text]{width:100%;box-sizing:border-box;background:#0c0f12;color:#e8e8e8;border:1px solid #2a3038;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.8rem}
 .ok{color:#7ec77e} .ko{color:#e07070} .muted{color:#8a95a0;font-size:.85rem}
 #res{margin-top:.8rem;font-weight:600;min-height:1.2rem}
 textarea{width:100%;box-sizing:border-box;background:#0c0f12;color:#e8e8e8;border:1px solid #2a3038;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.75rem}
</style></head><body><div class="box">
<h1>🛒 Jow — connecter mon magasin (Auchan / Courses U…)</h1>
<p class="muted">Session magasin pour la commande d'ingrédients depuis Home Assistant.</p>

<div class="card">
<h2>1. Connectez votre enseigne sur jow.fr</h2>
<ol>
 <li>Ouvrez <a href="https://jow.fr" target="_blank" rel="noopener">jow.fr ↗</a> et connectez-vous</li>
 <li>Menu <b>Commander / Courses</b> → choisissez votre enseigne, connectez-vous (votre code MFA vous sera demandé), choisissez votre magasin</li>
</ol>
</div>

<div class="card">
<h2>2. Copiez votre refresh token</h2>
<p>Sur jow.fr, ouvrez la console (F12 → Console), collez ceci et Entrée — le token est copié dans le presse-papiers :</p>
<code>copy(JSON.parse(localStorage.jow_store).data.auth.refreshToken)</code>
</div>

<div class="card">
<h2>3. Importez la session dans Home Assistant</h2>
<input type="text" id="rt" placeholder="Collez ici le refreshToken (eyJhbGci…)">
<button class="btn" onclick="imp()">Importer la session</button>
<div id="res"></div>
<p class="muted">Alternatif (commande directe) : <code>jow.import_token</code> dans Outils de développement.</p>
</div>

<div class="card">
<h2>Option — activer la commande 100 % depuis HA (cookie partagé)</h2>
<p class="muted">Les sessions magasin vivent sur un nœud serveur précis (sticky session). Pour que HA voie le vôtre, injectez le cookie ci-dessous dans votre navigateur :</p>
<ol>
 <li id="ck-li">Cookie HA : <code id="ck">chargement…</code></li>
 <li>Sur jow.fr : F12 → Application → Cookies → <code>api.jow.fr</code> → créez/modifiez <b>JowSession</b> avec cette valeur, puis rechargez et reconnectez l'enseigne</li>
</ol>
</div>

<script>
async function imp(){
 const rt=document.getElementById('rt').value.trim();
 const res=document.getElementById('res');
 if(!rt){res.className='ko';res.textContent='Collez le token d'abord';return}
 res.className='';res.textContent='Import en cours…';
 try{
  const r=await fetch('/api/services/jow/import_token',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({refresh_token:rt})});
  res.className=r.ok?'ok':'ko';
  res.textContent=r.ok?'✓ Session importée — retournez sur votre dashboard et testez 🛒 Préparer la commande':'✕ '+(await r.text()).slice(0,120);
 }catch(e){res.className='ko';res.textContent='✕ '+e}
}
(async()=>{
 try{
  const r=await fetch('/api/jow/auth/cookie');const d=await r.json();
  document.getElementById('ck').textContent=d.cookie||'(pas encore de cookie — appelez un service jow puis rechargez)';
 }catch(e){document.getElementById('ck').textContent='(indisponible)'}
})();
</script>
</div></body></html>"""


class JowAuthView(web.View):
    """Page d'authentification guidée (+ endpoint cookie)."""

    url = "/api/jow/auth"
    name = "api:jow:auth"
    requires_auth = True  # session HA requise (l'utilisateur est connecté à HA)

    async def get(self, request: web.Request) -> web.Response:
        if request.path.endswith("/cookie"):
            hass = request.app["hass"]
            entry_id = request.query.get("entry_id")
            manager = None
            if entry_id and entry_id in hass.data.get("jow", {}):
                manager = hass.data["jow"][entry_id]
            elif hass.data.get("jow"):
                manager = next(iter(hass.data["jow"].values()))
            cookie = None
            if manager:
                try:
                    cookie = manager.api_client().jow_session_cookie
                except Exception:
                    cookie = None
            return web.json_response({"cookie": cookie})
        return web.Response(text=PAGE, content_type="text/html")


async def async_setup_page(hass) -> None:
    """Enregistre la page /api/jow/auth."""
    hass.http.app.router.add_get(JowAuthView.url, JowAuthView)
    hass.http.app.router.add_get(JowAuthView.url + "/cookie", JowAuthView)
