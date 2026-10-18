(() => {
  "use strict";

  const cfg = window.APP_CONFIG || {};
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.initData) { tg.ready(); tg.expand(); document.documentElement.classList.add("tg"); }
  const COLORS = { missile: "#e5484d", uav: "#f2762e", predicted: "#f5c24c", launch: "#a99cf0" };
  const LABELS = { missile: "Ракетная опасность", uav: "Угроза БПЛА", predicted: "Вероятный подлёт" };
  const SEVERITY = { missile: 3, uav: 2, predicted: 1 };
  const BASIS = { target: "по заявленному курсу", observed: "по фиксациям", hint: "по направлению" };

  const $ = (id) => document.getElementById(id);
  const eta = (m) => (m <= 1 ? "< 1 мин" : `~${m} мин`);
  const dur = (m) => (m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч${m % 60 ? ` ${String(m % 60).padStart(2, "0")} мин` : ""}`);
  // «через ~2 ч 10 – 2 ч 40»; если окно уже прошло — первые могли долететь
  const etaRange = ([lo, hi]) => (hi <= 0 ? "могли уже долететь" : lo <= 0 ? `в ближайшие ${dur(hi)}` : `через ~${dur(lo)} – ${dur(hi)}`);
  const launchCount = (l) => (l.count ? (l.count[0] === l.count[1] ? `${l.count[0]} БПЛА` : `группа ${l.count[0]}–${l.count[1]}`) : l.massive ? "массовый пуск" : "число неизвестно");
  const tgLink = (ch, id) => `https://t.me/${encodeURIComponent(ch)}${id ? `/${id}` : ""}`;
  const channelLinks = (list) => (list || []).map((c) => `<a href="${tgLink(c)}" target="_blank" rel="noopener">@${esc(c)}</a>`).join(", ");
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
      layers: [{ id: "bg", type: "background", paint: { "background-color": "#0a0d12" } }],
    };

  const map = new maplibregl.Map({
    container: "map",
    style: styleUrl,
    center: window.innerWidth < 720 ? [37.5, 52.5] : [40, 54],
    zoom: window.innerWidth < 720 ? 4.2 : 4.6,
    minZoom: 2,
    maxZoom: 12,
    attributionControl: { compact: true, customAttribution: "Границы: © участники OpenStreetMap" },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  // на телефоне нижнюю часть карты закрывает панель — центрируем по видимой области
  const isPhone = () => window.innerWidth < 720;
  // нижнюю часть карты (на телефоне) или левую (на компьютере) закрывает панель — центрируем по видимой области
  function fitPadding() {
    const collapsed = $("panel").classList.contains("collapsed");
    const panel = $("panel").getBoundingClientRect();
    map.setPadding(isPhone()
      ? { top: 60, bottom: Math.round(collapsed ? panel.height : window.innerHeight * 0.45) + 20, left: 0, right: 0 }
      : { top: 0, bottom: 0, left: collapsed ? 0 : Math.round(panel.right), right: 0 });
  }

  let regionNames = {};
  let regionGeoms = {};
  let districtInfo = {};   // id -> {name, region, geometry}
  let lastState = null;
  let shownRegionStatus = {};
  let shownDistrictStatus = {};
  const STATUS_FILL = (opacity) => ["match", ["coalesce", ["feature-state", "status"], "none"],
    "missile", opacity.missile, "uav", opacity.uav, "predicted", opacity.predicted, 0];
  const STATUS_COLOR = ["match", ["coalesce", ["feature-state", "status"], "none"],
    "missile", COLORS.missile, "uav", COLORS.uav, "predicted", COLORS.predicted, "#000"];

  const empty = () => ({ type: "FeatureCollection", features: [] });

  function firstSymbolLayer() {
    const layer = map.getStyle().layers.find((l) => l.type === "symbol");
    return layer && layer.id;
  }

  map.on("load", async () => {
    const [regions, districts, places] = await Promise.all([
      fetch(cfg.regionsUrl || "api/regions").then((r) => r.json()),
      fetch(cfg.districtsUrl || "api/districts").then((r) => r.json()).catch(() => empty()),
      fetch(cfg.placesUrl || "api/places").then((r) => r.json()).catch(() => []),
    ]);
    regions.features.forEach((f) => { regionNames[f.properties.id] = f.properties.name; regionGeoms[f.properties.id] = f.geometry; });
    districts.features.forEach((f) => { districtInfo[f.properties.id] = { ...f.properties, geometry: f.geometry }; });
    fillRegionSelect();

    const before = firstSymbolLayer();
    map.addSource("regions", { type: "geojson", data: regions, promoteId: "id" });
    map.addSource("districts", { type: "geojson", data: districts, promoteId: "id" });
    if (!cfg.maptilerKey) {
      // своя подложка: суша чуть светлее фона, без внешних тайлов
      map.addLayer({ id: "land", type: "fill", source: "regions", paint: { "fill-color": "#151a22" } }, before);
    }
    map.addLayer({
      id: "regions-fill", type: "fill", source: "regions",
      // при приближении область бледнеет, чтобы были видны районы с фиксациями
      paint: {
        "fill-color": STATUS_COLOR,
        "fill-opacity": ["interpolate", ["linear"], ["zoom"],
          5, STATUS_FILL({ missile: 0.42, uav: 0.3, predicted: 0.22 }),
          7, STATUS_FILL({ missile: 0.25, uav: 0.12, predicted: 0.1 })],
      },
    }, before);
    // районы приграничных областей: подсвечиваются точнее, чем вся область
    map.addLayer({
      id: "districts-fill", type: "fill", source: "districts",
      paint: { "fill-color": STATUS_COLOR, "fill-opacity": STATUS_FILL({ missile: 0.6, uav: 0.55, predicted: 0.3 }) },
    }, before);
    map.addLayer({
      id: "districts-line", type: "line", source: "districts", minzoom: 5,
      paint: { "line-color": "#4a5463", "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 9, 1], "line-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 7, 0.8] },
    }, before);
    map.addLayer({
      id: "regions-line", type: "line", source: "regions",
      paint: { "line-color": "#7a8697", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 8, 1.6], "line-opacity": 0.8 },
    }, before);
    if (!cfg.maptilerKey) addCityLabels(places);

    // пуски: тонкий пунктир от района пуска к вероятной цели — ниже всех слоёв целей
    map.addSource("launches", { type: "geojson", data: empty() });
    map.addLayer({
      id: "launch-routes", type: "line", source: "launches",
      layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.launch, "line-width": 1.6, "line-opacity": ["case", ["get", "arrived"], 0.25, ["get", "named"], 0.75, 0.45], "line-dasharray": [1, 2.5] },
    });
    // тепловая карта пролётов за период — под всеми слоями целей
    map.addSource("heat", { type: "geojson", data: empty() });
    map.addLayer({
      id: "heat", type: "heatmap", source: "heat", layout: { visibility: "none" },
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "count"], 1, 0.25, 20, 1],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 3, 0.6, 8, 2],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 3, 10, 6, 22, 9, 40],
        "heatmap-opacity": 0.8,
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(0,0,0,0)", 0.15, "#3b2a6b", 0.4, "#8e3b8f", 0.65, "#e0663a", 0.9, "#f5c24c", 1, "#fff3c4"],
      },
    }, "launch-routes");
    map.addSource("cones", { type: "geojson", data: empty() });
    map.addSource("predicted", { type: "geojson", data: empty() });
    map.addSource("observed", { type: "geojson", data: empty() });
    map.addSource("fixes", { type: "geojson", data: empty() });

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

    map.on("click", (e) => {
      const hit = map.queryRenderedFeatures(e.point, { layers: ["districts-fill", "regions-fill"] });
      const d = hit.find((f) => f.layer.id === "districts-fill");
      const r = hit.find((f) => f.layer.id === "regions-fill");
      if (d || r) areaPopup(e.lngLat, d && d.properties.id, (r && r.properties.id) || (d && d.properties.region));
    });
    map.on("mousemove", (e) => {
      const hit = map.queryRenderedFeatures(e.point, { layers: ["regions-fill"] });
      map.getCanvas().style.cursor = hit.length ? "pointer" : "";
    });

    if (cfg.timelineUrl) playTimeline(cfg.timelineUrl); else connect();
  });

  function statusHtml(info) {
    if (!info) return `<span style="color:#3dd68c">Угроз не зафиксировано</span>`;
    if (info.status === "predicted") return `<span style="color:${COLORS.predicted}">Вероятный подлёт: ${eta(info.eta_min)}</span>`;
    return `<span style="color:${COLORS[info.status]}">${LABELS[info.status]}</span><br><span class="sub">с ${hhmm(info.since)}, обновлено ${ago(info.updated, lastState.now)}</span>`
      + (info.channels && info.channels.length ? `<br><span class="sub">Источник: ${channelLinks(info.channels)}</span>` : "");
  }

  function areaPopup(lngLat, districtId, regionId) {
    if (!lastState) return;
    let html = "";
    if (districtId) html += `<b>${esc(districtInfo[districtId].name)}</b><br>${statusHtml(lastState.districts[districtId])}<hr class="sep">`;
    html += `<b>${esc(regionNames[regionId] || regionId)}</b><br>${statusHtml(lastState.regions[regionId])}`;
    new maplibregl.Popup({ maxWidth: "260px" }).setLngLat(lngLat).setHTML(html).addTo(map);
  }

  // Подписи городов без внешних шрифтов: DOM-метки, областные центры видны раньше остальных
  function addCityLabels(places) {
    for (const p of places) {
      const el = document.createElement("div");
      el.className = `city${p.capital ? " capital" : ""}`;
      el.innerHTML = `<i></i><span>${esc(p.name)}</span>`;
      new maplibregl.Marker({ element: el, anchor: "left", offset: [-4, 0] }).setLngLat([p.lon, p.lat]).addTo(map);
    }
    const zoomClass = () => {
      const z = map.getZoom(), c = map.getContainer().classList;
      c.toggle("z-capitals", z >= 4.6);
      c.toggle("z-towns", z >= 6.4);
    };
    map.on("zoom", zoomClass);
    zoomClass();
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
    const dstates = state.districts || {};
    for (const id of Object.keys(districtInfo)) {
      const st = dstates[id] ? dstates[id].status : "none";
      if (shownDistrictStatus[id] !== st) {
        map.setFeatureState({ source: "districts", id }, { status: st });
        shownDistrictStatus[id] = st;
      }
    }

    const cones = [], predicted = [], observed = [], fixes = [], seen = new Set();
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
      seen.add(tr.id);
      placeDrone(tr, pos, pr ? pr.bearing : null, state.now);
    }
    map.getSource("cones").setData(fc(cones));
    map.getSource("predicted").setData(fc(predicted));
    map.getSource("observed").setData(fc(observed));
    map.getSource("fixes").setData(fc(fixes));
    for (const [id, m] of droneMarkers) if (!seen.has(id)) { m.remove(); droneMarkers.delete(id); }
    const launches = state.launches || [];
    map.getSource("launches").setData(fc(launches.flatMap((l) => l.routes.map((r) =>
      line(r.path.map(([la, lo]) => [lo, la]), { named: r.named, arrived: !!r.arrived })))));
    syncMarkers(launchMarkers, launches.filter((l) => l.origin_pos && !l.planned),
      (l) => `${l.origin}/${l.first_t}`, launchMarkEl, (el, l) => {
        el.querySelector("span").textContent = `${l.origin}${l.jet ? " · реактивные" : ""}`;
        return `<b>Пуски БПЛА: ${esc(l.origin)}</b><br>${launchCount(l)}${l.jet ? ", реактивные" : ""}`
          + (l.routes.length ? `<br>${l.routes.map((r) => `${r.named ? "" : "вероятно "}${esc(r.target)}: ${etaRange(r.eta_min)}`).join("<br>")}` : "")
          + `<br><span class="sub">Маршрут и время ориентировочные: по крейсерской скорости, без учёта манёвров.</span>`
          + `<br><span class="sub">Источник: ${channelLinks(l.channels)}</span>`;
      }, (l) => [l.origin_pos[1], l.origin_pos[0]]);
    syncMarkers(etaMarkers, launches.flatMap((l) => l.routes.filter((r) => !r.arrived)
      .map((r) => ({ ...r, key: `${l.origin}/${l.first_t}/${r.region}/${r.target}` }))),
      (r) => r.key, etaMarkEl, (el, r) => {
        el.textContent = r.eta_min[1] <= 0 ? "подлёт возможен" : `≈ ${dur(Math.max(r.eta_min[0], 0))}`;
        return `<b>${esc(r.target)}</b><br>Возможный подлёт ${etaRange(r.eta_min)}<br><span class="sub">${r.named ? "Направление указал канал" : "Типичное направление для этого района пуска"}</span>`;
      }, (r) => [r.path[r.path.length - 1][1], r.path[r.path.length - 1][0]]);
    syncMarkers(alertMarkers, state.alert_marks || [], (a) => a.district || a.region, alertMarkEl, (el, a) => {
      // className не перезаписываем: MapLibre держит на элементе свой класс позиционирования
      el.classList.toggle("missile", a.level === "missile");
      el.classList.toggle("uav", a.level !== "missile");
      el.setAttribute("aria-label", `${LABELS[a.level]}: ${a.name}`);
      return `<b>${esc(a.name)}</b><br><span style="color:${COLORS[a.level]}">${LABELS[a.level]}</span>`
        + `<br><span class="sub">Цель не зафиксирована, обновлено ${ago(a.updated, state.now)}</span>`
        + (a.channels.length ? `<br><span class="sub">Источник: ${channelLinks(a.channels)}</span>` : "");
    });
    syncMarkers(shootMarkers, state.shootdowns || [], (x) => `${x.channel}/${x.msg_id}/${x.place}`, shootMarkEl, (el, x) => {
      const age = (state.now - x.t) / 60;
      el.style.opacity = String(Math.max(0.35, 1 - age / 40));
      return `<b>Сбитие: ${esc(x.place)}</b><br><span class="sub">${ago(x.t, state.now)} · `
        + `<a href="${tgLink(x.channel, x.msg_id)}" target="_blank" rel="noopener">@${esc(x.channel)}</a></span>`;
    });

    renderPanel(state);
    checkMyRegion(state);
  }

  const fc = (features) => ({ type: "FeatureCollection", features });
  const point = (c, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: c } });
  const line = (c, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: c } });

  // Значки «!» (опасность без фиксации) и отметки сбития — тоже DOM-маркеры
  const alertMarkers = new Map();
  const shootMarkers = new Map();
  const alertMarkEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "alert-mark";
    el.textContent = "!";
    return el;
  };
  const shootMarkEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "shoot-mark";
    el.setAttribute("aria-label", "Сбитие БПЛА");
    el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 1l2.4 6.3L21 5l-3.3 5.6L23 13l-6 1.6 1.6 6.4-5.1-3.9L9.7 23l-.9-6.6L2 17.5l4.2-5.2L1 8.6l6.6-.6z"/></svg>`;
    return el;
  };
  const launchMarkers = new Map();
  const etaMarkers = new Map();
  const launchMarkEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "launch-mark";
    el.innerHTML = `<i aria-hidden="true"></i><span></span>`;
    return el;
  };
  const etaMarkEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "eta-mark";
    return el;
  };
  function syncMarkers(store, items, keyOf, makeEl, update, posOf = (x) => [x.lon, x.lat]) {
    const keep = new Set();
    for (const item of items) {
      const key = keyOf(item);
      keep.add(key);
      let m = store.get(key);
      if (!m) {
        m = new maplibregl.Marker({ element: makeEl() }).setLngLat(posOf(item))
          .setPopup(new maplibregl.Popup({ offset: 14, maxWidth: "280px" })).addTo(map);
        store.set(key, m);
      }
      m.getPopup().setHTML(update(m.getElement(), item));
    }
    for (const [key, m] of store) if (!keep.has(key)) { m.remove(); store.delete(key); }
  }

  // Цели рисуем DOM-маркерами: стрелка по курсу, число БПЛА рядом (без загрузки шрифтов для карты)
  const droneMarkers = new Map();
  function placeDrone(tr, pos, bearing, now) {
    let m = droneMarkers.get(tr.id);
    if (!m) {
      const el = document.createElement("button");
      el.className = "drone";
      el.type = "button";
      el.innerHTML = `<i class="scope" aria-hidden="true"></i><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l8 18-8-4.5L4 20z"/></svg><span></span>`;
      m = new maplibregl.Marker({ element: el, rotationAlignment: "map" }).setLngLat(pos)
        .setPopup(new maplibregl.Popup({ offset: 14, maxWidth: "280px" })).addTo(map);
      droneMarkers.set(tr.id, m);
    }
    m.setLngLat(pos);
    const el = m.getElement();
    el.setAttribute("aria-label", `Цель #${tr.id}`);
    el.querySelector("svg").style.transform = `rotate(${bearing ?? 0}deg)`;
    el.classList.toggle("no-course", bearing == null);
    el.classList.toggle("engaged", !!tr.engaged);
    el.querySelector("span").textContent = tr.count > 1 ? `×${tr.count}` : "";
    m.getPopup().setHTML(trackPopup(tr, now));
  }

  function trackPopup(tr, now) {
    const last = tr.points[tr.points.length - 1];
    const pr = tr.prediction;
    let html = `<b>Цель #${tr.id}${tr.count > 1 ? ` · ${tr.count} БПЛА` : ""}</b><br>`;
    if (tr.engaged) html += `<span style="color:${COLORS.missile}">Работает ПВО</span><br>`;
    if (!pr) html += `<span class="sub">Одна фиксация: курс неизвестен, маршрут появится со второй точки</span><br>`;
    html += `Последняя фиксация: ${esc(last.place)}${last.district_name && last.district_name !== last.place ? ` (${esc(last.district_name)})` : ""}, ${ago(last.t, now)}<br>`;
    html += `Точек: ${tr.points.length} · источники: ${channelLinks([...new Set(tr.points.flatMap((p) => p.sources))])}`;
    if (pr) {
      html += `<br>Курс ${pr.bearing}°, ~${pr.speed} км/ч (${BASIS[pr.basis]})`;
      if (tr.target) html += `<br>Заявленная цель: ${esc(tr.target)}`;
      if (pr.districts_eta && pr.districts_eta.length) html += `<br>Ближайшие районы: ${pr.districts_eta.slice(0, 3).map((r) => `${esc(r.name)} ${eta(r.eta_min)}`).join(", ")}`;
      if (pr.regions_eta.length) html += `<br>Может войти: ${pr.regions_eta.slice(0, 3).map((r) => `${esc(r.name)} ${eta(r.eta_min)}`).join(", ")}`;
    }
    return html;
  }

  // Связь с каналами: «пусто на карте» не должно выглядеть как «угроз нет», если данные просто не приходят
  function renderHealth(state) {
    const bar = $("health-bar");
    const chans = Object.values((state.health && state.health.channels) || {});
    if (cfg.demo || !chans.length) {
      bar.hidden = true;
      $("status-line").textContent = `${cfg.demo ? "ДЕМО · " : ""}обновлено ${hhmm(state.now)}`;
      return;
    }
    const ok = chans.filter((c) => c.ok);
    const lastOk = Math.max(0, ...chans.map((c) => c.last_ok || 0));
    const interval = (state.health && state.health.interval) || 30;
    const staleMin = lastOk ? Math.round((state.now - lastOk) / 60) : null;
    $("status-line").textContent = `${ok.length ? "онлайн" : "нет связи"} · ${ok.length}/${chans.length} каналов · обновлено ${hhmm(state.now)}`;
    if (!ok.length && chans.some((c) => c.ok === false)) {
      const err = chans.find((c) => c.error);
      bar.className = "health-bar";
      bar.textContent = `Нет связи с Telegram-каналами — карта не обновляется${staleMin != null ? ` (последние данные ${staleMin} мин назад)` : ""}.`
        + (err ? ` Ошибка: ${err.error}` : "");
      bar.hidden = false;
    } else if (lastOk && state.now - lastOk > interval * 6) {
      bar.className = "health-bar stale";
      bar.textContent = `Каналы давно не читались (${staleMin} мин) — данные могут быть устаревшими.`;
      bar.hidden = false;
    } else if (ok.length && ok.length < chans.length / 2) {
      bar.className = "health-bar stale";
      bar.textContent = `Читается только ${ok.length} из ${chans.length} каналов — часть сообщений может не попасть на карту.`;
      bar.hidden = false;
    } else {
      bar.hidden = true;
    }
  }

  function renderPanel(state) {
    // слои, выключенные на сервере (PUBLISH_*), убираем и из легенды
    const pub = state.publish || {};
    document.querySelectorAll("[data-layer]").forEach((el) => { el.hidden = pub[el.dataset.layer] === false; });
    const regions = Object.entries(state.regions).sort((a, b) =>
      (SEVERITY[b[1].status] - SEVERITY[a[1].status]) || ((a[1].eta_min ?? 0) - (b[1].eta_min ?? 0)));
    const active = state.tracks.filter((t) => t.status === "active");
    const counts = { alert: regions.filter(([, r]) => r.status !== "predicted").length, pred: regions.filter(([, r]) => r.status === "predicted").length };

    $("summary").innerHTML = `
      <div class="stat"><b>${counts.alert}</b><span>регионов с тревогой</span></div>
      <div class="stat"><b>${active.reduce((s, t) => s + t.count, 0)}</b><span>целей в воздухе</span></div>
      <div class="stat"><b>${counts.pred}</b><span>на пути подлёта</span></div>`;

    const byRegion = {};
    for (const [id, d] of Object.entries(state.districts || {})) (byRegion[d.region] ||= []).push(d);
    $("alert-list").innerHTML = regions.length ? regions.map(([id, r]) => {
      const ds = (byRegion[id] || []).filter((d) => d.status !== "predicted").map((d) => esc(d.name));
      return `<li data-region="${esc(id)}">
        <div class="row"><span>${esc(r.name)}</span><span class="badge ${r.status}">${r.status === "predicted" ? eta(r.eta_min) : LABELS[r.status]}</span></div>
        <div class="sub">${r.status === "predicted" ? "по прогнозу маршрута" : `с ${hhmm(r.since)} · обновлено ${ago(r.updated, state.now)}`}</div>
        ${r.channels && r.channels.length ? `<div class="sub src">${channelLinks(r.channels)}</div>` : ""}
        ${ds.length ? `<div class="sub districts">Районы: ${ds.join(", ")}</div>` : ""}
      </li>`;
    }).join("") : `<li class="empty">Активных тревог нет</li>`;

    $("track-list").innerHTML = active.length ? active.map((tr) => {
      const last = tr.points[tr.points.length - 1];
      const pr = tr.prediction;
      const nextArea = pr && ((pr.districts_eta && pr.districts_eta[0]) || pr.regions_eta[0]);
      const next = nextArea ? `→ ${esc(nextArea.name)} ${eta(nextArea.eta_min)}` : (tr.target ? `→ ${esc(tr.target)}` : "курс не определён");
      return `<li data-track="${tr.id}">
        <div class="row"><span>#${tr.id} · ${esc(last.place)}${tr.count > 1 ? ` · ×${tr.count}` : ""}</span><span class="sub">${ago(last.t, state.now)}</span></div>
        <div class="sub">${next}</div></li>`;
    }).join("") : `<li class="empty">Целей не зафиксировано</li>`;

    const launchList = (state.launches || []);
    $("launch-section").hidden = !launchList.length;
    $("launch-list").innerHTML = launchList.map((l) => {
      const head = l.planned ? `Подготовка к пуску${l.origin ? `: ${esc(l.origin)}` : ""}` : `Пуски${l.origin ? `: ${esc(l.origin)}` : ""}`;
      const where = l.routes.length
        ? l.routes.map((r) => `${r.named ? "" : "вероятно "}${esc(r.target)} — ${r.arrived ? "уже есть фиксации" : etaRange(r.eta_min)}`).join("<br>")
        : l.targets.length ? `в направлении: ${l.targets.map(esc).join(", ")}` : "направление не указано";
      return `<li><div class="row"><span>${head}</span><span class="sub">${hhmm(l.last_t)}</span></div>
        <div class="sub">${l.planned ? "" : `${launchCount(l)}${l.jet ? ", реактивные" : ""}<br>`}${where}</div>
        <div class="sub src">${channelLinks(l.channels)}</div></li>`;
    }).join("");

    $("feed").innerHTML = state.feed.length ? state.feed.slice(0, 25).map((m) => `
      <li><div class="row"><a class="sub src" href="${tgLink(m.channel, m.msg_id)}" target="_blank" rel="noopener">@${esc(m.channel)}</a><span class="sub">${hhmm(m.t)}</span></div>
      <div class="text">${esc(m.text)}</div></li>`).join("") : `<li class="empty">Сообщений пока нет</li>`;

    renderHealth(state);

    const fc_ = state.forecast;
    $("forecast").hidden = !fc_;
    if (fc_) {
      $("forecast-meta").innerHTML = `· <a href="${tgLink(fc_.channel, fc_.msg_id)}" target="_blank" rel="noopener">@${esc(fc_.channel)}</a>, ${hhmm(fc_.t)}`;
      $("forecast-text").textContent = fc_.text;
    }
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
    // место под панель уже учтено в map.setPadding (fitPadding)
    map.fitBounds(b, { padding: 40, maxZoom: 8, duration: 800 });
  }

  // ---------- «моя область» и уведомления ----------

  function fillRegionSelect() {
    const sel = $("my-region");
    Object.entries(regionNames).sort((a, b) => a[1].localeCompare(b[1], "ru"))
      .forEach(([id, name]) => sel.add(new Option(name, id)));
    sel.value = store.get("myRegion") || "";
    const dsel = $("my-district");
    const fillDistricts = () => {
      const list = Object.entries(districtInfo).filter(([, d]) => d.region === sel.value)
        .sort((a, b) => a[1].name.localeCompare(b[1].name, "ru"));
      dsel.length = 1;
      list.forEach(([id, d]) => dsel.add(new Option(d.name, id)));
      $("my-district-field").hidden = !list.length;
    };
    fillDistricts();
    dsel.value = store.get("myDistrict") || "";
    if (dsel.selectedIndex < 0) dsel.value = "";
    const changed = () => { store.set("myRegion", sel.value); store.set("myDistrict", dsel.value); myLast = undefined; if (lastState) checkMyRegion(lastState); };
    sel.addEventListener("change", () => { fillDistricts(); dsel.value = ""; changed(); });
    dsel.addEventListener("change", changed);
    if ("Notification" in window && Notification.permission === "default") $("notify-btn").hidden = false;
    $("notify-btn").addEventListener("click", async () => {
      await Notification.requestPermission();
      $("notify-btn").hidden = Notification.permission !== "default";
    });
  }

  let myLast;
  function checkMyRegion(state) {
    const id = $("my-region").value;
    const did = $("my-district").value;
    const box = $("my-region-status");
    if (!id) { box.hidden = true; return; }
    // выбран район — смотрим на него; если в районе тихо, но область под тревогой, показываем область
    const dinfo = did ? (state.districts || {})[did] : null;
    const rinfo = state.regions[id];
    const info = dinfo || rinfo;
    const st = info ? info.status : "none";
    const where = dinfo ? districtInfo[did].name : regionNames[id];
    box.hidden = false;
    box.className = `my-status ${st}`;
    box.textContent = !info ? "Угроз не зафиксировано" :
      (st === "predicted" ? `Возможен подлёт БПЛА: ${eta(info.eta_min)}` : LABELS[st]) + (did ? ` · ${where}` : "");
    if (myLast !== undefined && (SEVERITY[st] || 0) > (SEVERITY[myLast] || 0)) alertUser(where, box.textContent);
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

  // Статическое демо: заранее посчитанные снимки сценария, проигрываем по кругу
  async function playTimeline(url) {
    const frames = await (await fetch(url)).json();
    const bar = $("demo-bar");
    let i = 0, paused = false;
    if (bar) {
      bar.hidden = false;
      $("demo-toggle").addEventListener("click", () => {
        paused = !paused;
        $("demo-toggle").textContent = paused ? "Продолжить" : "Пауза";
      });
      $("demo-restart").addEventListener("click", () => { i = 0; step(); });
    }
    function step() {
      const f = frames[i];
      render(f);
      if (bar) {
        const min = Math.round((f.now - frames[0].now) / 60);
        $("demo-time").textContent = `минута ${min} из ${Math.round((frames[frames.length - 1].now - frames[0].now) / 60)}`;
      }
    }
    step();
    setInterval(() => { if (!paused) { i = (i + 1) % frames.length; step(); } }, 700);
  }

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

  if (cfg.channels && cfg.channels.length && !cfg.demo) {
    $("sources-section").hidden = false;
    $("sources").innerHTML = cfg.channels.map((c) => `<li><a href="${tgLink(c.channel)}" target="_blank" rel="noopener">@${esc(c.channel)}</a>${c.note ? `<div class="sub">${esc(c.note)}</div>` : ""}</li>`).join("");
  }

  // Согласие с условиями: показываем при первом входе и после смены редакции условий
  (function consent() {
    const version = cfg.termsVersion || "1";
    if (store.get("termsAccepted") === version) return;
    const box = $("consent");
    box.hidden = false;
    document.body.classList.add("consent-open");
    $("consent-accept").addEventListener("change", (e) => { $("consent-submit").disabled = !e.target.checked; });
    $("consent-form").addEventListener("submit", (e) => {
      e.preventDefault();
      if (!$("consent-accept").checked) return;
      store.set("termsAccepted", version);
      box.hidden = true;
      document.body.classList.remove("consent-open");
    });
    $("consent-accept").focus();
  })();

  // ---------- история пролётов (тепловая карта) ----------

  let heatDays = 0, heatTimer = null;
  async function loadHeat() {
    if (!heatDays || !map.getSource("heat")) return;
    try {
      const url = cfg.heatmapUrl || `api/heatmap?days=${heatDays}`;
      const h = await (await fetch(url)).json();
      map.getSource("heat").setData(fc((h.points || []).map((p) => point([p.lon, p.lat], { count: p.count }))));
      $("heat-summary").hidden = false;
      $("heat-summary").textContent = h.total
        ? `${h.total} фиксаций${h.since ? ` с ${new Date(h.since * 1000).toLocaleDateString("ru-RU")}` : ""}. Работа ПВО и сбития в карту не входят.`
        : "За этот период фиксаций в архиве пока нет — архив копится с момента запуска сервера.";
      $("heat-top").hidden = !h.points || !h.points.length;
      $("heat-top").innerHTML = (h.points || []).slice(0, 8).map((p) => `<li>${esc(p.place)} <span>— ${p.count}</span></li>`).join("");
    } catch {
      $("heat-summary").hidden = false;
      $("heat-summary").textContent = "Тепловая карта недоступна.";
    }
  }
  document.querySelectorAll("#heat-section .seg button").forEach((btn) => btn.addEventListener("click", () => {
    heatDays = Number(btn.dataset.days);
    document.querySelectorAll("#heat-section .seg button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    map.setLayoutProperty("heat", "visibility", heatDays ? "visible" : "none");
    $("heat-summary").hidden = $("heat-top").hidden = !heatDays;
    clearInterval(heatTimer);
    if (heatDays) { loadHeat(); heatTimer = setInterval(loadHeat, 10 * 60 * 1000); }
  }));

  // ---------- слова поддержки и цитаты ----------

  (async function quotes() {
    try {
      const items = (await (await fetch(cfg.quotesUrl || "quotes.json")).json()).items || [];
      if (!items.length) return;
      let i = Math.floor(Math.random() * items.length);
      const show = () => {
        const q = items[i++ % items.length];
        $("quote-text").textContent = q.author ? `«${q.text}»` : q.text;
        $("quote-author").textContent = q.author ? `— ${q.author}${q.source ? `, ${q.source}` : ""}` : "";
      };
      $("quote").hidden = false;
      show();
      setInterval(show, 45000);
    } catch { /* без цитат */ }
  })();

  if (cfg.channels && cfg.channels.length) {
    $("consent-channels").textContent = cfg.channels.map((c) => `@${c.channel}`).join(", ");
  } else {
    $("consent-channels").textContent = "список — в пользовательском соглашении";
  }

  $("legend-toggle").addEventListener("click", () => {
    const open = $("legend").classList.toggle("open");
    $("legend-toggle").setAttribute("aria-expanded", String(open));
  });

  // Панель можно свернуть до одной строки, чтобы не закрывала карту; выбор запоминается
  function setCollapsed(collapsed) {
    $("panel").classList.toggle("collapsed", collapsed);
    $("panel-toggle").setAttribute("aria-expanded", String(!collapsed));
    $("panel-toggle").textContent = collapsed ? "Показать" : "Скрыть";
    store.set("panelCollapsed", collapsed ? "1" : "");
    requestAnimationFrame(fitPadding);
  }
  $("panel-toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    setCollapsed(!$("panel").classList.contains("collapsed"));
  });
  $("panel-head").addEventListener("click", () => {
    if ($("panel").classList.contains("collapsed")) setCollapsed(false);
  });
  setCollapsed(store.get("panelCollapsed") === "1");
  window.addEventListener("resize", fitPadding);
})();
