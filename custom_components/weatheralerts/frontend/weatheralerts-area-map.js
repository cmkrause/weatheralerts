/* Local cartographic context. No remote tiles, geocoder, or Home coordinates. */
let contextPromise;
function contextMap() {
  if (!contextPromise) {
    const url = new URL("./map-context.json", import.meta.url);
    contextPromise = fetch(url).then(response => {
      if (!response.ok) throw new Error("Map context unavailable");
      return response.json();
    }).catch(error => { contextPromise = undefined; throw error; });
  }
  return contextPromise;
}

function wrap(lon) {
  return ((lon + 540) % 360) - 180;
}

class AlertAreaMap extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        dialog { box-sizing:border-box; width:min(720px,95vw); max-height:90vh; overflow:auto;
          padding:18px; background:var(--card-background-color,#fff); color:var(--primary-text-color,#222);
          border:1px solid #849693; border-radius:12px; font:inherit }
        dialog::backdrop { background:#12232288 } header,footer { display:flex; align-items:center; gap:8px }
        h2 { flex:1; margin:0; font-size:1.2rem } button { font:inherit; color:inherit; background:transparent;
          border:1px solid #879593; border-radius:6px; padding:5px 12px; cursor:pointer }
        canvas { width:100%; height:clamp(220px,52vh,450px); display:block; touch-action:none; cursor:grab }
        canvas[hidden], footer[hidden] { display:none } p { margin:10px 0 }
        small { margin-inline-start:auto } footer { margin-top:8px; flex-wrap:wrap }
      </style>
      <dialog aria-labelledby="title"><header><h2 id="title"></h2><button id="close">Close</button></header>
      <p role="status"></p><canvas hidden role="img" aria-label="Map of the affected area"></canvas>
      <footer hidden><button id="out" aria-label="Zoom out">−</button><button id="in" aria-label="Zoom in">+</button>
      <button id="fit">Fit area</button><small>NWS alert area · Natural Earth basemap</small></footer></dialog>`;
    this.dialog = root.querySelector("dialog");
    this.canvas = root.querySelector("canvas");
    this.status = root.querySelector("p");
    root.querySelector("#close").onclick = () => this.dialog.close();
    this.dialog.onclose = () => this.remove();
    this.zoom = 1;
    this.pan = [0, 0];
    root.querySelector("#in").onclick = () => this.setZoom(this.zoom * 1.5);
    root.querySelector("#out").onclick = () => this.setZoom(this.zoom / 1.5);
    root.querySelector("#fit").onclick = () => { this.pan = [0, 0]; this.setZoom(1); };
    this.canvas.onpointerdown = event => {
      this.canvas.setPointerCapture(event.pointerId);
      this.drag = [event.clientX, event.clientY];
    };
    this.canvas.onpointermove = event => {
      if (!this.drag) return;
      this.pan[0] += event.clientX - this.drag[0];
      this.pan[1] += event.clientY - this.drag[1];
      this.drag = [event.clientX, event.clientY];
      this.draw();
    };
    this.canvas.onlostpointercapture = () => { this.drag = null; };
    this.resize = new ResizeObserver(() => this.draw());
  }

  setZoom(value) {
    this.zoom = Math.min(16, Math.max(0.5, value));
    this.draw();
  }

  disconnectedCallback() { this.resize.disconnect(); }

  async open(hass, entity, alert) {
    this.shadowRoot.querySelector("h2").textContent = alert.event || "Affected area";
    this.status.textContent = "Loading affected area…";
    this.dialog.showModal();
    try {
      const area = await hass.callWS({type:"weatheralerts/area", entity_id:entity, alert_id:alert.id});
      if (!this.isConnected) return;
      if (!area.geometry) {
        this.status.textContent = "The affected area could not be mapped.";
        return;
      }
      this.polys = area.geometry.type === "Polygon" ? [area.geometry.coordinates] : area.geometry.coordinates;
      // Unwrap around the alert, including MultiPolygons on both sides of 180°.
      this.origin = this.polys[0][0][0][0];
      this.bounds = [Infinity, Infinity, -Infinity, -Infinity];
      for (const poly of this.polys) for (const [lon, lat] of poly[0]) {
        const x = wrap(lon - this.origin);
        this.bounds[0] = Math.min(this.bounds[0], x);
        this.bounds[1] = Math.min(this.bounds[1], lat);
        this.bounds[2] = Math.max(this.bounds[2], x);
        this.bounds[3] = Math.max(this.bounds[3], lat);
      }
      this.status.textContent = area.resolved < area.total
        ? `Partial coverage · Showing ${area.resolved} of ${area.total} affected zones.`
        : (alert.area && alert.area !== "null" ? alert.area : "Shading shows the NWS alert area.");
      this.canvas.hidden = false;
      this.shadowRoot.querySelector("footer").hidden = false;
      this.resize.observe(this.canvas);
      this.draw();
      try { this.context = await contextMap(); }
      catch { this.status.textContent += " Basemap unavailable."; }
      if (this.isConnected) this.draw();
    } catch {
      this.status.textContent = "The area could not be loaded. The alert may have changed.";
    }
  }

  draw() {
    if (!this.bounds || !this.isConnected) return;
    const canvas = this.canvas;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const [west,south,east,north] = this.bounds;
    const middle = (north+south)/2, midX = (east+west)/2;
    const cos = Math.max(0.05, Math.cos(middle*Math.PI/180));
    const scale = this.zoom * Math.min(w*0.84 / Math.max(0.03, (east-west)*cos),
      h*0.84 / Math.max(0.03, north-south));
    const x = lon => w/2 + this.pan[0] + (wrap(lon-this.origin)-midX)*cos*scale;
    const y = lat => h/2 + this.pan[1] - (lat-middle)*scale;
    const path = ring => {
      // Keep contiguous rings on one side of the projection seam.
      let last = ring[0][0], px = x(last);
      ctx.moveTo(px,y(ring[0][1]));
      for (let i=1; i<ring.length; i++) {
        const [lon,lat] = ring[i];
        px += wrap(lon-last)*cos*scale;
        ctx.lineTo(px,y(lat));
        last=lon;
      }
      ctx.closePath();
    };
    ctx.fillStyle="#dceaf0"; ctx.fillRect(0,0,w,h);
    ctx.fillStyle="#eff0df"; ctx.strokeStyle="#90a8a1"; ctx.lineWidth=0.8;
    for (const [box,ring] of this.context?.land || []) {
      if (y(box[3])>h || y(box[1])<0) continue;
      ctx.beginPath(); path(ring); ctx.fill(); ctx.stroke();
    }
    ctx.fillStyle="rgba(210,104,18,.36)"; ctx.strokeStyle="#854518"; ctx.lineWidth=2;
    for (const polygon of this.polys) {
      ctx.beginPath();
      for (const ring of polygon) path(ring);
      ctx.fill("evenodd"); ctx.stroke();
    }
    const occupied=[];
    ctx.font="12px system-ui"; ctx.textBaseline="middle";
    for (const [,town,lon,lat] of this.context?.towns || []) {
      const px=x(lon), py=y(lat);
      if (px<4 || py<10 || px>w-15 || py>h-12) continue;
      const end=px+ctx.measureText(town).width+8;
      if (end>w || occupied.some(([a,b,c]) => px<c+8 && end>a-8 && Math.abs(py-b)<17)) continue;
      occupied.push([px,py,end]); ctx.fillStyle="#243a38"; ctx.fillRect(px-1,py-1,3,3);
      ctx.strokeStyle="#fffff3"; ctx.lineWidth=3; ctx.strokeText(town,px+5,py);
      ctx.fillText(town,px+5,py);
    }
  }
}

customElements.define("weatheralerts-area-map", AlertAreaMap);
