/**
 * Jow Card — carte Lovelace custom pour l'intégration Jow.
 * Affiche le menu de la semaine avec vignettes, liens vers jow.fr,
 * et un bouton pour planifier un repas.
 *
 * Configuration:
 *   type: custom:jow-card
 *   title: Menu de la semaine (optionnel)
 *   show_images: true (optionnel, défaut true)
 *   show_ingredients: false (optionnel, défaut false)
 *   today_entity: sensor.jow_repas_du_jour (optionnel)
 */

class JowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._recipes = [];
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Configuration invalide");
    }
    this._config = {
      title: "Menu de la semaine",
      show_images: true,
      show_ingredients: false,
      ...config,
    };
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  static getStubConfig() {
    return {
      title: "Menu de la semaine",
      show_images: true,
    };
  }

  getCardSize() {
    return 4;
  }

  _getDays() {
    return ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
  }

  _getMeal(day) {
    if (!this._hass) return null;
    const entity = `sensor.jow_${day}`;
    const state = this._hass.states[entity];
    if (!state || !state.attributes || !state.attributes.planned) return null;
    return state;
  }

  _getTodayMeal() {
    if (!this._hass) return null;
    const entity = this._config.today_entity || "sensor.jow_repas_du_jour";
    const state = this._hass.states[entity];
    if (!state || !state.attributes || !state.attributes.planned) return null;
    return state;
  }

  _formatIngredients(ingredients) {
    if (!ingredients || !ingredients.length) return "";
    return ingredients
      .map((ing) => {
        const qty = ing.quantity ? `${ing.quantity} ${ing.unit || ""} ` : "";
        return `• ${qty}${ing.name}`;
      })
      .join("\n");
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data);
  }

  _planMeal(day) {
    const query = prompt(`Recherche Jow pour ${day} :`, "poulet curry coco");
    if (!query) return;
    this._callService("jow", "plan_meal", {
      query: query,
      weekday: day,
      covers: 2,
    });
  }

  _clearMeal(day) {
    this._callService("jow", "clear_meal", { weekday: day });
  }

  _refreshShopping() {
    this._callService("jow", "refresh_shopping_list", {
      week_offset: 0,
      keep_checked: true,
    });
  }

  render() {
    if (!this._hass) return;

    const days = this._getDays();
    const today = new Date().toLocaleDateString("fr-FR", { weekday: "long" }).toLowerCase();
    const showImages = this._config.show_images;
    const showIngredients = this._config.show_ingredients;

    let html = `
      <style>
        :host {
          display: block;
        }
        .card {
          background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,0.1));
          padding: 16px;
          overflow: hidden;
        }
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 12px;
        }
        .title {
          font-size: 1.3em;
          font-weight: 600;
          color: var(--primary-text-color);
        }
        .icon {
          --mdc-icon-size: 24px;
          color: var(--primary-color, #ff8c32);
        }
        .refresh-btn {
          background: none;
          border: none;
          cursor: pointer;
          padding: 4px;
          color: var(--primary-color, #ff8c32);
          border-radius: 6px;
        }
        .refresh-btn:hover {
          background: var(--secondary-background-color, #f0f0f0);
        }
        .day-row {
          display: flex;
          align-items: flex-start;
          padding: 8px 0;
          border-bottom: 1px solid var(--divider-color, #eee);
          gap: 12px;
        }
        .day-row:last-child {
          border-bottom: none;
        }
        .day-row.today {
          background: var(--secondary-background-color, rgba(255,140,50,0.05));
          border-radius: 8px;
          padding: 8px;
          margin: 0 -8px;
        }
        .day-label {
          flex-shrink: 0;
          width: 80px;
          font-weight: 600;
          text-transform: capitalize;
          color: var(--primary-text-color);
          padding-top: 2px;
        }
        .day-label .today-badge {
          display: inline-block;
          font-size: 0.65em;
          background: var(--primary-color, #ff8c32);
          color: #fff;
          padding: 1px 6px;
          border-radius: 8px;
          margin-left: 4px;
          vertical-align: middle;
        }
        .meal-content {
          flex-grow: 1;
          min-width: 0;
        }
        .meal-name {
          font-weight: 500;
          color: var(--primary-text-color);
          margin-bottom: 2px;
        }
        .meal-name a {
          color: var(--primary-color, #ff8c32);
          text-decoration: none;
        }
        .meal-name a:hover {
          text-decoration: underline;
        }
        .meal-meta {
          font-size: 0.85em;
          color: var(--secondary-text-color);
        }
        .meal-image {
          flex-shrink: 0;
          width: 48px;
          height: 48px;
          border-radius: 8px;
          object-fit: cover;
          background: var(--secondary-background-color, #f0f0f0);
        }
        .empty {
          color: var(--secondary-text-color);
          font-style: italic;
          font-size: 0.9em;
        }
        .ingredients {
          font-size: 0.8em;
          color: var(--secondary-text-color);
          white-space: pre-line;
          margin-top: 4px;
          padding: 6px 8px;
          background: var(--secondary-background-color, rgba(0,0,0,0.03));
          border-radius: 6px;
        }
        .actions {
          display: flex;
          gap: 6px;
          margin-top: 4px;
        }
        .btn {
          font-size: 0.75em;
          padding: 2px 8px;
          border: 1px solid var(--divider-color, #ddd);
          border-radius: 6px;
          background: none;
          cursor: pointer;
          color: var(--secondary-text-color);
        }
        .btn:hover {
          background: var(--secondary-background-color, #f0f0f0);
        }
        .btn-plan {
          color: var(--primary-color, #ff8c32);
          border-color: var(--primary-color, #ff8c32);
        }
        .footer {
          margin-top: 12px;
          display: flex;
          gap: 8px;
        }
        .footer .btn {
          font-size: 0.85em;
          padding: 6px 12px;
        }
      </style>
      <ha-card class="card">
        <div class="header">
          <div class="title">${this._config.title}</div>
          <button class="refresh-btn" title="Rafraîchir la liste de courses" onclick="this.getRootNode().host._refreshShopping()">
            <ha-icon icon="mdi:cart" class="icon"></ha-icon>
          </button>
        </div>
    `;

    for (const day of days) {
      const meal = this._getMeal(day);
      const isToday = day === today;

      html += `<div class="day-row ${isToday ? "today" : ""}">`;

      // Day label
      html += `<div class="day-label">${day}${isToday ? '<span class="today-badge">AUJOURD\'HUI</span>' : ""}</div>`;

      // Meal content
      if (meal) {
        const attrs = meal.attributes;
        const name = meal.state || "Recette";
        const url = attrs.url || "";
        const image = attrs.image || "";
        const prep = attrs.preparation_time;
        const cook = attrs.cooking_time;
        const covers = attrs.covers;

        if (showImages && image) {
          html += `<img class="meal-image" src="${image}" alt="${name}" onerror="this.style.display='none'"/>`;
        } else {
          html += `<div class="meal-image"></div>`;
        }

        html += `<div class="meal-content">`;
        html += `<div class="meal-name">`;
        if (url) {
          html += `<a href="${url}" target="_blank" rel="noopener">${name}</a>`;
        } else {
          html += name;
        }
        html += `</div>`;

        const metaParts = [];
        if (prep) metaParts.push(`⏱ ${prep} min`);
        if (cook) metaParts.push(`🔥 ${cook} min`);
        if (covers) metaParts.push(`🍽 ${covers} couverts`);
        if (metaParts.length) {
          html += `<div class="meal-meta">${metaParts.join(" · ")}</div>`;
        }

        if (showIngredients && attrs.ingredients) {
          const ing = this._formatIngredients(attrs.ingredients);
          if (ing) html += `<div class="ingredients">${ing}</div>`;
        }

        html += `<div class="actions">`;
        html += `<button class="btn btn-plan" onclick="this.getRootNode().host._planMeal('${day}')">Changer</button>`;
        html += `<button class="btn" onclick="this.getRootNode().host._clearMeal('${day}')">Effacer</button>`;
        html += `</div>`;

        html += `</div>`;
      } else {
        html += `<div class="meal-image"></div>`;
        html += `<div class="meal-content">`;
        html += `<div class="empty">Rien de prévu</div>`;
        html += `<div class="actions">`;
        html += `<button class="btn btn-plan" onclick="this.getRootNode().host._planMeal('${day}')">Planifier</button>`;
        html += `</div>`;
        html += `</div>`;
      }

      html += `</div>`;
    }

    html += `
        <div class="footer">
          <button class="btn btn-plan" onclick="this.getRootNode().host._refreshShopping()">Rafraîchir la liste de courses</button>
        </div>
      </ha-card>
    `;

    this.shadowRoot.innerHTML = html;
  }
}

customElements.define("jow-card", JowCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "jow-card",
  name: "Jow Card",
  description: "Menu de la semaine Jow avec vignettes, liens et planification",
  preview: false,
});

console.info("%c JOW-CARD %c v1.0.0 ", "background:#ff8c32;color:#fff;border-radius:3px 0 0 3px;padding:2px 6px", "background:#333;color:#aaa;border-radius:0 3px 3px 0;padding:2px 6px");