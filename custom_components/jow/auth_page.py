"""Page web d'authentification guidée jow (sans script python).

Enregistrée sur /api/jow/auth par l'intégration (HomeAssistantView :
auth HA requise). Remplace le script scripts/jow_marchand.py pour
l'usage courant : l'utilisateur s'authentifie sur jow.fr dans SON
navigateur (MFA inclus), colle son refresh token dans la page, et
l'intégration importe la session (service jow.import_token).
"""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

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
 input[type=text]{width:100%;box-sizing:border-box;background:#0c0f12;color:#e8e8e8;border:1px solid #2a3038;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.8rem}
 .ok{color:#7ec77e} .ko{color:#e07070} .muted{color:#8a95a0;font-size:.85rem}
 #res{margin-top:.8rem;font-weight:600;min-height:1.2rem}
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
<p class="muted">Alternatif : <code>jow.import_token</code> dans Outils de développement.</p>
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
  res.textContent=r.ok?'✓ Session importée — testez 🛒 Préparer la commande sur votre dashboard':'✕ échec — vérifiez le token';
 }catch(e){res.className='ko';res.textContent='✕ '+e}
}
</script>
</div></body></html>"""


class JowAuthView(HomeAssistantView):
    """Page d'authentification guidée (auth Home Assistant requise)."""

    url = "/api/jow/auth"
    name = "api:jow:auth"

    async def get(self, request):
        return web.Response(text=PAGE, content_type="text/html")


async def async_setup_page(hass) -> None:
    """Enregistre la vue via le pipeline HTTP officiel de HA."""
    hass.http.register_view(JowAuthView())
