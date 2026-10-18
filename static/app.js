(() => {
  "use strict";

  const cfg = window.APP_CONFIG || {};
  const COLORS = { missile: "#e5484d", uav: "#f2762e", predicted: "#f5c24c" };
  const LABELS = { missile: "Ракетная опасность", uav: "Угроза БПЛА", predicted: "Вероятный подлёт" };
  const SEVERITY = { missile: 3, uav: 2, predicted: 1 };
  const BASIS = { target: "по заявленному курсу", observed: "по фиксациям", hint: "по направлению" };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const ago = (t, now) => {
    const m = Math.max(0, Math.round((now - t) / 60));
    return m < 1 ? "только что" : m < 60 ? `${m} мин назад` : `${Math.floor(m / 60)} ч ${m % 60} мин назад`;
  };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } },
  };

  const styleUrl = cfg.maptilerKey
    ? `https://api.maptiler.com/maps/${encodeURIComponent(cfg.maptilerStyle || "dataviz-dark")}/style.json?key=${encodeURIComponent(cfg.maptilerKey)}`
    : {
      version: 8, sources: {},
      glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
      layers: [{ id: "bg", type: "background", paint: { "background-color": "#0e1116" } }],
    };
  const FONT = cfg.maptilerKey ? "Noto Sans Regular" : "Open Sans Semibold";
  if (!cfg.maptilerKey) console.warn("MAPTILER_KEY не задан — показываем только границы регионов");

  const map = new maplibregl.Map({
    container: "map",
    style: styleUrl,
    center: window.innerWidth < 720 ? [37.5, 51.5] : [40, 54],
    zoom: window.innerWidth < 720 ? 4.2 : 4.6,
    minZoom: 2,
    maxZoom: 12,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

  let regionNames = {};
  let regionGeoms = {};
  let lastState = null;
  let shownRegionStatus = {};

  const empty = () => ({ type: "FeatureCollection", features: [] });

  function droneIcon() {
    const size = 48, c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    g.translate(size / 2, size / 2);
    g.beginPath();
    g.moveTo(0, -18); g.lineTo(13, 14); g.lineTo(0, 7); g.lineTo(-13, 14); g.closePath();
    g.fillStyle = "#ff5a5f"; g.strokeStyle = "#fff"; g.lineWidth = 2.5;
    g.fill(); g.stroke();
    return g.getImageData(0, 0, size, size);
  }

  function firstSymbolLayer() {
    const layer = map.getStyle().layers.find((l) => l.type === "symbol");
    return layer && layer.id;
  }

  map.on("load", async () => {
    map.addImage("drone", droneIcon(), { pixelRatio: 2 });
    const regions = await (await fetch("api/regions")).json();
    regions.features.forEach((f) => { regionNames[f.properties.id] = f.properties.name; regionGeoms[f.properties.id] = f.geometry; });
    fillRegionSelect();

    const before = firstSymbolLayer();
    map.addSource("regions", { type: "geojson", data: regions, promoteId: "id" });
    map.addLayer({
      id: "regions-fill", type: "fill", source: "regions",
      paint: {
        "fill-color": ["match", ["coalesce", ["feature-state", "status"], "none"],
          "missile", COLORS.missile, "uav", COLORS.uav, "predicted", COLORS.predicted, "#000"],
        "fill-opacity": ["match", ["coalesce", ["feature-state", "status"], "none"],
          "missile", 0.42, "uav", 0.36, "predicted", 0.26, 0],
      },
    }, before);
    map.addLayer({
      id: "regions-line", type: "line", source: "regions",
      paint: { "line-color": "#6b7686", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.4, 8, 1.2], "line-opacity": 0.7 },
    }, before);

    map.addSource("cones", { type: "geojson", data: empty() });
    map.addSource("predicted", { type: "geojson", data: empty() });
    map.addSource("observed", { type: "geojson", data: empty() });
    map.addSource("fixes", { type: "geojson", data: empty() });
    map.addSource("drones", { type: "geojson", data: empty() });

    map.addLayer({ id: "cones", type: "fill", source: "cones", paint: { "fill-color": COLORS.predicted, "fill-opacity": 0.13 } });
    map.addLayer({ id: "cones-edge", type: "line", source: "cones", paint: { "line-color": COLORS.predicted, "line-width": 1, "line-opacity": 0.35 } });
    map.addLayer({
      id: "predicted", type: "line", source: "predicted",
      paint: { "line-color": COLORS.predicted, "line-width": 2.5, "line-dasharray": [2, 1.5] },
    });
    map.addLayer({
      id: "observed", type: "line", source: "observed",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#ff5a5f", "line-width": 3, "line-opacity": ["case", ["==", ["get", "status"], "active"], 0.95, 0.35] },
    });
    map.addLayer({
      id: "fixes", type: "circle", source: "fixes",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 3, 8, 6],
        "circle-color": "#ff5a5f", "circle-stroke-color": "#fff", "circle-stroke-width": 1.2,
        "circle-opacity": ["case", ["==", ["get", "status"], "active"], 1, 0.4],
        "circle-stroke-opacity": ["case", ["==", ["get", "status"], "active"], 1, 0.4],
      },
    });
    map.addLayer({
      id: "drones", type: "symbol", source: "drones",
      layout: {
        "icon-image": "drone", "icon-size": ["interpolate", ["linear"], ["zoom"], 3, 0.7, 8, 1.1],
        "icon-rotate": ["get", "bearing"], "icon-rotation-alignment": "map", "icon-allow-overlap": true,
        "text-field": ["get", "label"], "text-size": 12, "text-offset": [0, 1.6], "text-anchor": "top",
        "text-allow-overlap": false, "text-font": [FONT],
      },
      paint: { "text-color": "#fff", "text-halo-color": "#000", "text-halo-width": 1.4 },
    });

    map.on("click", "regions-fill", (e) => regionPopup(e));
    map.on("click", "drones", (e) => {
      const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(p.popup).addTo(map);
    });
    for (const id of ["drones", "regions-fill"]) {
      map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
    }

    connect();
  });

  function regionPopup(e) {
    const id = e.features[0].properties.id;
    const info = lastState && lastState.regions[id];
    let html = `<b>${esc(regionNames[id] || id)}</b><br>`;
    if (!info) html += `<span style="color:#3dd68c">Угроз не зафиксировано</span>`;
    else if (info.status === "predicted") html += `<span style="color:${COLORS.predicted}">Вероятный подлёт через ~${info.eta_min} мин</span>`;
    else html += `<span style="color:${COLORS[info.status]}">${LABELS[info.status]}</span><br><span class="sub">с ${hhmm(info.since)}, обновлено ${ago(info.updated, lastState.now)}</span>`;
    new maplibregl.Popup({ maxWidth: "260px" }).setLngLat(e.lngLat).setHTML(html).addTo(map);
  }

  // ---------- данные ----------

  function render(state) {
    lastState = state;
    if (!map.getSource("regions")) return;

    for (const id of Object.keys(regionNames)) {
      const st = state.regions[id] ? state.regions[id].status : "none";
      if (shownRegionStatus[id] !== st) {
        map.setFeatureState({ source: "regions", id }, { status: st });
        shownRegionStatus[id] = st;
      }
    }

    const cones = [], predicted = [], observed = [], fixes = [], drones = [];
    for (const tr of state.tracks) {
      const coords = tr.points.map((p) => [p.lon, p.lat]);
      if (coords.length > 1) observed.push(line(coords, { status: tr.status }));
      tr.points.forEach((p) => fixes.push(point([p.lon, p.lat], { status: tr.status })));
      if (tr.status !== "active") continue;
      const pr = tr.prediction;
      const last = tr.points[tr.points.length - 1];
      const pos = pr ? [pr.path[0][1], pr.path[0][0]] : [last.lon, last.lat];
      if (pr) {
        predicted.push(line(pr.path.map(([la, lo]) => [lo, la]), {}));
        cones.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pr.cone.map(([la, lo]) => [lo, la])] } });
      }
      drones.push(point(pos, {
        bearing: pr ? pr.bearing : 0,
        label: tr.count > 1 ? `×${tr.count}` : "",
        popup: trackPopup(tr, state.now),
      }));
    }
    map.getSource("cones").setData(fc(cones));
    map.getSource("predicted").setData(fc(predicted));
    map.getSource("observed").setData(fc(observed));
    map.getSource("fixes").setData(fc(fixes));
    map.getSource("drones").setData(fc(drones));

    renderPanel(state);
    checkMyRegion(state);
  }

  const fc = (features) => ({ type: "FeatureCollection", features });
  const point = (c, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: c } });
  const line = (c, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: c } });

  function trackPopup(tr, now) {
    const last = tr.points[tr.points.length - 1];
    const pr = tr.prediction;
    let html = `<b>Цель #${tr.id}${tr.count > 1 ? ` · ${tr.count} БПЛА` : ""}</b><br>`;
    html += `Последняя фиксация: ${esc(last.place)}, ${ago(last.t, now)}<br>`;
    html += `Источников: ${new Set(tr.points.flatMap((p) => p.sources)).size}, точек: ${tr.points.length}`;
    if (pr) {
      html += `<br>Курс ${pr.bearing}°, ~${pr.speed} км/ч (${BASIS[pr.basis]})`;
      if (tr.target) html += `<br>Заявленная цель: ${esc(tr.target)}`;
      if (pr.regions_eta.length) html += `<br>Может войти: ${pr.regions_eta.slice(0, 3).map((r) => `${esc(r.name)} ~${r.eta_min} мин`).join(", ")}`;
    }
    return html;
  }

  function renderPanel(state) {
    const regions = Object.entries(state.regions).sort((a, b) =>
      (SEVERITY[b[1].status] - SEVERITY[a[1].status]) || ((a[1].eta_min ?? 0) - (b[1].eta_min ?? 0)));
    const active = state.tracks.filter((t) => t.status === "active");
    const counts = { alert: regions.filter(([, r]) => r.status !== "predicted").length, pred: regions.filter(([, r]) => r.status === "predicted").length };

    $("summary").innerHTML = `
      <div class="stat"><b>${counts.alert}</b><span>регионов с тревогой</span></div>
      <div class="stat"><b>${active.reduce((s, t) => s + t.count, 0)}</b><span>целей в воздухе</span></div>
      <div class="stat"><b>${counts.pred}</b><span>на пути подлёта</span></div>`;

    $("alert-list").innerHTML = regions.length ? regions.map(([id, r]) => `
      <li data-region="${esc(id)}">
        <div class="row"><span>${esc(r.name)}</span><span class="badge ${r.status}">${r.status === "predicted" ? `~${r.eta_min} мин` : LABELS[r.status]}</span></div>
        <div class="sub">${r.status === "predicted" ? "по прогнозу маршрута" : `с ${hhmm(r.since)} · обновлено ${ago(r.updated, state.now)}`}</div>
      </li>`).join("") : `<li class="empty">Активных тревог нет</li>`;

    $("track-list").innerHTML = active.length ? active.map((tr) => {
      const last = tr.points[tr.points.length - 1];
      const pr = tr.prediction;
      const next = pr && pr.regions_eta.length ? `→ ${esc(pr.regions_eta[0].name)} ~${pr.regions_eta[0].eta_min} мин` : (tr.target ? `→ ${esc(tr.target)}` : "курс не определён");
      return `<li data-track="${tr.id}">
        <div class="row"><span>#${tr.id} · ${esc(last.place)}${tr.count > 1 ? ` · ×${tr.count}` : ""}</span><span class="sub">${ago(last.t, state.now)}</span></div>
        <div class="sub">${next}</div></li>`;
    }).join("") : `<li class="empty">Целей не зафиксировано</li>`;

    $("feed").innerHTML = state.feed.length ? state.feed.slice(0, 25).map((m) => `
      <li><div class="row"><span class="sub">@${esc(m.channel)}</span><span class="sub">${hhmm(m.t)}</span></div>
      <div class="text">${esc(m.text)}</div></li>`).join("") : `<li class="empty">Сообщений пока нет</li>`;

    $("status-line").textContent = `${cfg.demo ? "ДЕМО · " : ""}обновлено ${hhmm(state.now)}`;
  }

  $("alert-list").addEventListener("click", (e) => {
    const li = e.target.closest("[data-region]");
    if (li) flyToRegion(li.dataset.region);
  });
  $("track-list").addEventListener("click", (e) => {
    const li = e.target.closest("[data-track]");
    const tr = li && lastState.tracks.find((t) => String(t.id) === li.dataset.track);
    if (!tr) return;
    const pts = tr.points.map((p) => [p.lon, p.lat]).concat(tr.prediction ? tr.prediction.path.map(([la, lo]) => [lo, la]) : []);
    fitTo(pts);
  });

  function flyToRegion(id) {
    const g = regionGeoms[id];
    if (!g) return;
    const coords = g.type === "Polygon" ? g.coordinates.flat() : g.coordinates.flat(2);
    fitTo(coords);
  }

  function fitTo(coords) {
    if (!coords.length) return;
    const b = coords.reduce((acc, c) => acc.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
    const wide = window.innerWidth >= 720;
    map.fitBounds(b, { padding: wide ? { top: 60, bottom: 60, left: 400, right: 60 } : 40, maxZoom: 8, duration: 800 });
  }

  // ---------- «моя область» и уведомления ----------

  function fillRegionSelect() {
    const sel = $("my-region");
    Object.entries(regionNames).sort((a, b) => a[1].localeCompare(b[1], "ru"))
      .forEach(([id, name]) => sel.add(new Option(name, id)));
    sel.value = store.get("myRegion") || "";
    sel.addEventListener("change", () => { store.set("myRegion", sel.value); myLast = undefined; if (lastState) checkMyRegion(lastState); });
    if ("Notification" in window && Notification.permission === "default") $("notify-btn").hidden = false;
    $("notify-btn").addEventListener("click", async () => {
      await Notification.requestPermission();
      $("notify-btn").hidden = Notification.permission !== "default";
    });
  }

  let myLast;
  function checkMyRegion(state) {
    const id = $("my-region").value;
    const box = $("my-region-status");
    if (!id) { box.hidden = true; return; }
    const info = state.regions[id];
    const st = info ? info.status : "none";
    box.hidden = false;
    box.className = `my-status ${st}`;
    box.textContent = !info ? "Угроз не зафиксировано" :
      st === "predicted" ? `Возможен подлёт БПЛА через ~${info.eta_min} мин` : LABELS[st];
    if (myLast !== undefined && (SEVERITY[st] || 0) > (SEVERITY[myLast] || 0)) alertUser(regionNames[id], box.textContent);
    myLast = st;
  }

  function alertUser(region, text) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.15, ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 1.2);
      o.start(); o.stop(ctx.currentTime + 1.2);
    } catch { /* звук недоступен */ }
    if ("Notification" in window && Notification.permission === "granted") new Notification(region, { body: text });
  }

  // ---------- транспорт ----------

  let retry = 1000, pollTimer = null;
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${location.pathname.replace(/[^/]*$/, "")}ws`);
    ws.onopen = () => { retry = 1000; clearInterval(pollTimer); pollTimer = null; };
    ws.onmessage = (e) => render(JSON.parse(e.data));
    ws.onclose = () => {
      if (!pollTimer) { poll(); pollTimer = setInterval(poll, 20000); }
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 30000);
    };
  }
  async function poll() {
    try { render(await (await fetch("api/state")).json()); } catch { $("status-line").textContent = "Нет связи с сервером"; }
  }

  $("panel-toggle").addEventListener("click", () => {
    const p = $("panel");
    p.classList.toggle("collapsed");
    $("panel-toggle").setAttribute("aria-expanded", String(!p.classList.contains("collapsed")));
    $("panel-toggle").textContent = p.classList.contains("collapsed") ? "▴" : "▾";
  });
})();
