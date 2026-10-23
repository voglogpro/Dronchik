(() => {
  "use strict";

  const cfg = window.APP_CONFIG || {};
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.initData) { tg.ready(); tg.expand(); document.documentElement.classList.add("tg"); }
  const COLORS = { flow: "#ffb347", track: "#ffa63d", casing: "#0b0e13", missile: "#e5484d", uav: "#f2762e", warn: "#efe08a", predicted: "#f5c24c", launch: "#a99cf0", forecast: "#8fb3d9" };
  const LABELS = { missile: "Ракетная опасность", uav: "Угроза БПЛА", warn: "Внимание по БПЛА", predicted: "Вероятный подлёт" };
  const SEVERITY = { missile: 4, uav: 3, warn: 2, predicted: 1 };
  const BASIS = { target: "по заявленному курсу", observed: "по фиксациям", hint: "по направлению", alert: "по новой тревоге в соседней области", inland: "вероятно, вглубь страны от границы" };

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
    attributionControl: { compact: true, customAttribution: "Границы: © участники OpenStreetMap · Пункты: GeoNames (CC BY 4.0)" },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  window.dronchikMap = map;   // для отладки и скриншотов
  // на телефоне нижнюю часть карты закрывает панель — центрируем по видимой области
  const isPhone = () => window.innerWidth < 720;
  // нижнюю часть карты (на телефоне) или левую (на компьютере) закрывает панель — центрируем по видимой области
  function fitPadding() {
    const closed = $("panel").classList.contains("closed");
    const panel = $("panel").getBoundingClientRect();
    map.setPadding(isPhone()
      ? { top: 60, bottom: closed ? 70 : Math.round(window.innerHeight * 0.45) + 20, left: 0, right: 0 }
      : { top: 0, bottom: 0, left: closed ? 0 : Math.round(panel.right), right: 0 });
  }

  let regionNames = {};
  let regionGeoms = {};
  let districtInfo = {};   // id -> {name, region, geometry}
  let lastState = null;
  let shownRegionStatus = {};
  let shownDistrictStatus = {};
  const STATUS_FILL = (opacity) => ["match", ["coalesce", ["feature-state", "status"], "none"],
    "missile", opacity.missile, "uav", opacity.uav, "warn", opacity.warn ?? opacity.predicted, "predicted", opacity.predicted, 0];
  const STATUS_COLOR = ["match", ["coalesce", ["feature-state", "status"], "none"],
    "missile", COLORS.missile, "uav", COLORS.uav, "warn", COLORS.warn, "predicted", COLORS.predicted, "#000"];

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
    // Coordinates are already simplified at export; a second simplification can
    // make neighbouring boundary strokes diverge further when zooming in.
    map.addSource("regions", { type: "geojson", data: regions, promoteId: "id", tolerance: 0 });
    map.addSource("districts", { type: "geojson", data: districts, promoteId: "id", tolerance: 0 });
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
    // контур области или района с тревогой — яркой линией со свечением, как на референсе
    const OUTLINE = (m, u, w, p, none = 0) => ["match", ["coalesce", ["feature-state", "status"], "none"], "missile", m, "uav", u, "warn", w, "predicted", p, none];
    map.addLayer({
      id: "regions-alert-glow", type: "line", source: "regions", layout: { "line-join": "round" },
      paint: { "line-color": OUTLINE("#ff5a5f", "#ff9a3d", "#efe08a", "#f5c24c", "rgba(0,0,0,0)"), "line-blur": 4,
        "line-width": OUTLINE(9, 8, 6, 5), "line-opacity": 0.28 },
    }, before);
    map.addLayer({
      id: "regions-alert-line", type: "line", source: "regions", layout: { "line-join": "round" },
      paint: { "line-color": OUTLINE("#ff6b6f", "#ff9a3d", "#efe08a", "#f5c24c", "rgba(0,0,0,0)"),
        "line-width": OUTLINE(2.6, 2.2, 1.6, 1.4), "line-opacity": 0.95 },
    }, before);
    map.addLayer({
      id: "districts-alert-line", type: "line", source: "districts", minzoom: 5.5, layout: { "line-join": "round" },
      paint: { "line-color": OUTLINE("#ff6b6f", "#ffb35c", "#efe08a", "#f5c24c", "rgba(0,0,0,0)"),
        "line-width": OUTLINE(1.8, 1.5, 1.1, 1), "line-opacity": 0.9 },
    }, before);
    // названия областей: DOM-метки (на карте без своих шрифтов), видны на средних масштабах
    for (const f of regions.features) {
      if (!f.properties.center) continue;
      const el = document.createElement("div");
      el.className = "region-label";
      el.textContent = shortRegionName(f.properties.name);
      new maplibregl.Marker({ element: el, anchor: "top", offset: [0, 15] }).setLngLat([f.properties.center[1], f.properties.center[0]]).addTo(map);
    }
    if (!cfg.maptilerKey) addCityLabels(places);
    // фон: области Украины (только границы) и места, откуда обычно идут пуски
    const [neighbors, origins] = await Promise.all([
      fetch(cfg.neighborsUrl || "api/neighbors").then((r) => r.json()).catch(() => empty()),
      fetch(cfg.originsUrl || "api/launch-areas").then((r) => r.json()).catch(() => []),
    ]);
    map.addSource("neighbors", { type: "geojson", data: neighbors });
    map.addLayer({ id: "neighbors-fill", type: "fill", source: "neighbors", paint: { "fill-color": "#11151b" } }, "regions-fill");
    map.addLayer({
      id: "neighbors-line", type: "line", source: "neighbors",
      paint: { "line-color": "#3a4350", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.4, 8, 1], "line-dasharray": [3, 2] },
    }, "regions-fill");
    for (const o of origins) {
      const el = document.createElement("div");
      el.className = "origin-mark";
      el.dataset.layer = "launches";
      el.dataset.origin = o.name;
      el.innerHTML = `<i></i><span>${esc(o.name)}</span>`;
      new maplibregl.Marker({ element: el, anchor: "left", offset: [-4, 0] }).setLngLat([o.lon, o.lat]).addTo(map);
    }

    // пуски: тонкий пунктир от района пуска к вероятной цели — ниже всех слоёв целей
    map.addSource("launches", { type: "geojson", data: empty() });
    map.addLayer({
      id: "launch-routes", type: "line", source: "launches",
      layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.launch, "line-width": 1.6, "line-opacity": ["case", ["get", "arrived"], 0.25, ["get", "named"], 0.75, 0.45], "line-dasharray": [1, 2.5] },
    });
    // векторы из «прогноза обстановки на ночь» — самый тусклый пунктир, под пусками
    map.addSource("forecast", { type: "geojson", data: empty() });
    map.addLayer({
      id: "forecast-vectors", type: "line", source: "forecast",
      layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.forecast, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1, 7, 1.6], "line-opacity": 0.38, "line-dasharray": [2, 3] },
    }, "launch-routes");
    // массовые пролёты: коридор из сообщения канала — широкая полоса под линиями целей
    map.addSource("flows", { type: "geojson", data: empty() });
    map.addLayer({
      id: "flows-glow", type: "line", source: "flows", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": COLORS.flow, "line-opacity": 0.16, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 7, 7, 16] },
    }, "forecast-vectors");
    map.addLayer({
      id: "flows", type: "line", source: "flows", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": COLORS.flow, "line-opacity": 0.8, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1.6, 7, 3], "line-dasharray": [2.2, 1.4] },
    }, "forecast-vectors");
    // возможный полёт ракеты при ракетной опасности: расчёт от границы, а не фиксация
    map.addSource("missiles", { type: "geojson", data: empty() });
    map.addLayer({
      id: "missiles", type: "line", source: "missiles", layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.missile, "line-width": 1.8, "line-dasharray": [3, 2.2],
        "line-opacity": ["case", ["==", ["get", "state"], "arrived"], 0.35, 0.8] },
    }, "forecast-vectors");
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
    for (const id of ["origins", "predicted", "observed", "arrows", "fixes"]) map.addSource(id, { type: "geojson", data: empty() });
    map.addImage("route-arrow", routeArrowImage());

    // Маршруты как у «потока»: тёмная подложка, свечение и пунктир одного оранжевого цвета, стрелки по направлению.
    const casing = (id, source, extra = {}) => map.addLayer({
      id, type: "line", source, layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": COLORS.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 3.2, 8, 6], "line-opacity": 0.5, ...extra },
    });
    // примерный путь от границы Украины до первой фиксации — тонкий пунктир под остальными линиями цели
    map.addLayer({
      id: "origins", type: "line", source: "origins", layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.track, "line-width": 1.3, "line-dasharray": [1, 2.4],
        "line-opacity": ["case", ["==", ["get", "status"], "active"], ["case", ["get", "single"], 0.28, 0.5], 0.22] },
    });
    casing("observed-casing", "observed", { "line-opacity": ["case", ["==", ["get", "status"], "active"], 0.5, 0.2] });
    casing("predicted-casing", "predicted", { "line-opacity": ["case", ["get", "weak"], 0.2, 0.4] });
    map.addLayer({
      id: "observed-glow", type: "line", source: "observed", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": COLORS.track, "line-blur": 3, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 5, 8, 11],
        "line-opacity": ["case", ["==", ["get", "status"], "active"], 0.2, 0.07] },
    });
    map.addLayer({
      id: "predicted", type: "line", source: "predicted", layout: { "line-cap": "round" },
      paint: { "line-color": COLORS.track, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1.6, 8, 2.6], "line-dasharray": [1.8, 1.6],
        "line-opacity": ["case", ["get", "weak"], 0.4, 0.85] },
    });
    map.addLayer({
      id: "observed", type: "line", source: "observed", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": COLORS.track, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1.8, 8, 3], "line-dasharray": [4, 1.4],
        "line-opacity": ["case", ["==", ["get", "status"], "active"], 0.95, 0.4] },
    });
    // стрелки вдоль линий показывают направление полёта
    map.addLayer({
      id: "route-arrows", type: "symbol", source: "arrows",
      layout: { "symbol-placement": "line", "symbol-spacing": 70, "icon-image": "route-arrow", "icon-size": ["interpolate", ["linear"], ["zoom"], 3, 0.42, 8, 0.7],
        "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-rotation-alignment": "map", "icon-keep-upright": false },
      paint: { "icon-opacity": ["case", ["get", "weak"], 0.45, 0.9] },
    });
    map.addLayer({
      id: "fixes", type: "circle", source: "fixes",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 2, 8, 4.5],
        "circle-color": COLORS.casing, "circle-stroke-color": COLORS.track, "circle-stroke-width": 1.6,
        "circle-opacity": ["case", ["==", ["get", "status"], "active"], 1, 0.4],
        "circle-stroke-opacity": ["case", ["==", ["get", "status"], "active"], 1, 0.4],
      },
    });

    map.on("click", (e) => {
      const hit = map.queryRenderedFeatures(e.point, { layers: ["districts-fill", "regions-fill"] });
      const d = hit.find((f) => f.layer.id === "districts-fill");
      const r = hit.find((f) => f.layer.id === "regions-fill");
      if (e.originalEvent.target.closest && e.originalEvent.target.closest(MARKER_SEL)) return;
      if (d || r) areaPopup(e.lngLat, d && d.properties.id, (r && r.properties.id) || (d && d.properties.region));
      else popup.remove();   // клик в пустое место закрывает окно
    });
    map.on("mousemove", (e) => {
      const hit = map.queryRenderedFeatures(e.point, { layers: ["regions-fill"] });
      map.getCanvas().style.cursor = hit.length ? "pointer" : "";
    });

    if (cfg.timelineUrl) playTimeline(cfg.timelineUrl); else connect();
  });

  // «Курская область» → «КУРСКАЯ», «Республика Крым» → «КРЫМ»: коротко, чтобы помещалось в границах
  const shortRegionName = (n) => n.replace(/^Республика\s+/i, "").replace(/\s+(область|край|автономный округ.*|автономная область)$/i, "")
    .replace(/\s+республика$/i, "").replace(/-Югра$/, "");
  // стрелка вдоль маршрута: оранжевый шеврон с тёмным контуром (рисуем на canvas — своих иконок и шрифтов у карты нет)
  function routeArrowImage() {
    const size = 32, c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    g.lineJoin = "round";
    g.beginPath();
    g.moveTo(8, 7); g.lineTo(25, 16); g.lineTo(8, 25); g.lineTo(13, 16); g.closePath();
    g.fillStyle = COLORS.track; g.strokeStyle = COLORS.casing; g.lineWidth = 2.4;
    g.stroke(); g.fill();
    return g.getImageData(0, 0, size, size);
  }
  // сглаживание ломаной (Chaikin): путь по фиксациям читается как траектория, а не набор отрезков
  function smooth(coords, rounds = 2) {
    let pts = coords;
    for (let r = 0; r < rounds && pts.length > 2; r++) {
      const out = [pts[0]];
      for (let i = 0; i < pts.length - 1; i++) {
        const [a, b] = [pts[i], pts[i + 1]];
        out.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25], [a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75]);
      }
      out.push(pts[pts.length - 1]);
      pts = out;
    }
    return pts;
  }
  // порог, с которого значки опасности показываются по районам
  const zoomFine = () => {
    const c = map.getContainer().classList, z = map.getZoom();
    c.toggle("z-fine", z >= 6.3);
    c.toggle("z-regionnames", z >= 4.2 && z < 7.4);
  };
  map.on("zoom", zoomFine);
  map.on("load", zoomFine);

  // Одно окно на всю карту: новое открывается вместо прежнего, а не поверх него
  const popup = new maplibregl.Popup({ maxWidth: "300px", offset: 16, className: "info-pop", closeOnClick: false });
  let popupOwner = null;
  const MARKER_SEL = ".drone, .alert-mark, .shoot-mark, .launch-mark, .eta-mark, .fc-arrow";
  function showPopup(lngLat, html, owner = null) {
    popupOwner = owner;
    popup.setLngLat(lngLat).setHTML(`<div class="pop">${html}</div>`).addTo(map);
  }
  popup.on("close", () => { popupOwner = null; });
  // значок открывает окно сам, а клик по нему не доходит до карты (там открылось бы ещё и окно области)
  function bindPopup(marker, el) {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (el._html) showPopup(marker.getLngLat(), el._html, marker);
    });
  }

  const badge = (status) => `<span class="pop-badge ${status}">${status === "predicted" ? "Вероятный подлёт" : LABELS[status]}</span>`;
  const srcLine = (channels, limit = 3) => {
    const list = channels || [];
    if (!list.length) return "";
    const shown = channelLinks(list.slice(0, limit));
    return `<div class="pop-src">Источники: ${shown}${list.length > limit ? ` и ещё ${list.length - limit}` : ""}</div>`;
  };
  const areaInfo = (info) => {
    if (!info) return `<div class="pop-ok">Угроз не зафиксировано</div>`;
    if (info.status === "predicted") return `<div class="pop-row">${badge("predicted")} <span class="sub">через ${eta(info.eta_min)}</span></div>`;
    return `<div class="pop-meta">с ${hhmm(info.since)} · обновлено ${ago(info.updated, lastState.now)}</div>${srcLine(info.channels)}`;
  };

  // Клик по району или области: одно окно — район (если есть) и область общей строкой
  function areaPopup(lngLat, districtId, regionId) {
    if (!lastState) return;
    const dInfo = districtId && lastState.districts[districtId];
    const rInfo = lastState.regions[regionId];
    const rName = esc(regionNames[regionId] || regionId);
    const dCount = Object.values(lastState.districts || {}).filter((d) => d.region === regionId && d.status !== "predicted").length;
    let html;
    if (districtId) {
      html = `<div class="pop-title">${esc(districtInfo[districtId].name)}${dInfo ? badge(dInfo.status) : ""}</div>${areaInfo(dInfo)}`
        + `<div class="pop-region"><b>${rName}</b>${rInfo ? badge(rInfo.status) : ""}`
        + `${dCount > 1 ? `<div class="sub">районов с тревогой: ${dCount}</div>` : ""}</div>`;
    } else {
      html = `<div class="pop-title">${rName}${rInfo ? badge(rInfo.status) : ""}</div>${areaInfo(rInfo)}`
        + (dCount ? `<div class="pop-sub">районов с тревогой: ${dCount}</div>` : "");
    }
    showPopup(lngLat, html);
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
      c.toggle("z-origins", z >= 5.2);
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

    const predicted = [], observed = [], origins = [], arrows = [], fixes = [], seen = new Set();
    for (const tr of state.tracks) {
      const coords = tr.points.map((p) => [p.lon, p.lat]);
      // у сбитой цели путь заканчивается там, где её сбили
      if (tr.end) coords.push([tr.end.lon, tr.end.lat]);
      if (coords.length > 1) {
        const sm = smooth(coords);
        observed.push(line(sm, { status: tr.status }));
        arrows.push(line(sm, { weak: false }));
      }
      if (tr.origin) origins.push(line([[tr.origin.lon, tr.origin.lat], coords[0]], { status: tr.status, single: tr.points.length < 2 }));
      tr.points.forEach((p) => fixes.push(point([p.lon, p.lat], { status: tr.status })));
      if (tr.status !== "active") continue;
      const pr = tr.prediction;
      const last = tr.points[tr.points.length - 1];
      const pos = pr ? [pr.path[0][1], pr.path[0][0]] : [last.lon, last.lat];
      if (pr) {
        // пунктир от последней фиксации: значок дрона едет по нему между обновлениями
        const smPath = smooth([[last.lon, last.lat], ...pr.path.map(([la, lo]) => [lo, la])]);
        predicted.push(line(smPath, { weak: pr.basis === "inland" }));
        arrows.push(line(smPath, { weak: pr.basis === "inland" }));
        smoothPath.set(tr.id, smPath);
      }
      seen.add(tr.id);
      placeDrone(tr, pos, pr ? pr.bearing : null, state.now);
    }
    map.getSource("origins").setData(fc(origins));
    map.getSource("predicted").setData(fc(predicted));
    map.getSource("observed").setData(fc(observed));
    map.getSource("arrows").setData(fc(arrows));
    map.getSource("fixes").setData(fc(fixes));
    for (const [id, m] of droneMarkers) if (!seen.has(id)) { m.remove(); droneMarkers.delete(id); droneMotion.delete(id); }
    const launches = state.launches || [];
    const liveOrigins = new Set(launches.filter((l) => l.origin && !l.planned).map((l) => l.origin));
    document.querySelectorAll(".origin-mark").forEach((el) => el.classList.toggle("live", liveOrigins.has(el.dataset.origin)));
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
      }, (r) => {
        // плашку ставим посередине последнего отрезка, чтобы не закрывать значок «!» в центре региона
        const [a, b] = [r.path[r.path.length - 2], r.path[r.path.length - 1]];
        return [(a[1] + b[1]) / 2, (a[0] + b[0]) / 2];
      });
    const vectors = (state.forecast && state.forecast.vectors) || [];
    const vname = (id) => id.split(",").map((r) => regionNames[r] || r).join(" и ");
    map.getSource("forecast").setData(fc(vectors.map((v) => line(v.path.map(([la, lo]) => [lo, la]), {}))));
    syncMarkers(forecastMarkers, vectors, (v) => `${v.from}>${v.to}`, forecastArrowEl, (el, v) => {
      const [[la1, lo1], [la2, lo2]] = v.path;
      const deg = Math.atan2((lo2 - lo1) * Math.cos(((la1 + la2) / 2) * Math.PI / 180), la2 - la1) * 180 / Math.PI;
      el.firstChild.style.transform = `rotate(${deg}deg)`;
      return `<b>Прогноз на ночь</b><br>${esc(vname(v.from))} → ${esc(vname(v.to))}`
        + `<br><span class="sub">Ожидаемое направление по прогнозу <a href="${tgLink(state.forecast.channel, state.forecast.msg_id)}" target="_blank" rel="noopener">@${esc(state.forecast.channel)}</a>, а не фиксация</span>`;
    }, (v) => {
      // стрелка ближе к цели, но не на её центре — там значок «!»
      const [[la1, lo1], [la2, lo2]] = v.path;
      return [lo1 + (lo2 - lo1) * 0.8, la1 + (la2 - la1) * 0.8];
    });
    // На мелком масштабе районы одной области сливаются в один значок «!» (с ростом масштаба — по районам)
    const marks = (state.alert_marks || []).map((m) => ({ ...m }));
    const inRegion = {};
    for (const m of marks) if (m.district) (inRegion[m.region] ||= []).push(m);
    const aggs = [];
    for (const [rid, list] of Object.entries(inRegion)) {
      if (list.length < 2) continue;
      list.forEach((m) => { m.fine = true; });
      aggs.push({
        agg: true, region: rid, district: null, name: regionNames[rid] || rid, count: list.length,
        level: list.some((m) => m.level === "missile") ? "missile" : "uav",
        lat: list.reduce((s, m) => s + m.lat, 0) / list.length, lon: list.reduce((s, m) => s + m.lon, 0) / list.length,
        channels: [...new Set(list.flatMap((m) => m.channels))], updated: Math.max(...list.map((m) => m.updated)),
      });
    }
    const flows = state.flows || [];
    map.getSource("flows").setData(fc(flows.flatMap((f) => f.paths.map((p) => line(p.path.map(([la, lo]) => [lo, la]), { kind: p.kind })))));
    const flowEnds = flows.flatMap((f) => f.paths.map((p, i) => ({ f, i, kind: p.kind, path: p.path })))
      .filter((x) => x.kind === "branch" || x.i === x.f.paths.findLastIndex((q) => q.kind === "main") && !x.f.paths.some((q) => q.kind === "branch"));
    syncMarkers(flowArrowMarkers, flowEnds, (x) => `${x.f.id}/${x.i}`, flowArrowEl, (el, x) => {
      const [[la1, lo1], [la2, lo2]] = x.path.slice(-2);
      const deg = Math.atan2((lo2 - lo1) * Math.cos(((la1 + la2) / 2) * Math.PI / 180), la2 - la1) * 180 / Math.PI;
      el.firstChild.style.transform = `rotate(${deg}deg)`;
      return flowPopup(x.f, state.now);
    }, (x) => { const [la, lo] = x.path[x.path.length - 1]; return [lo, la]; });
    syncMarkers(flowChipMarkers, flows.filter((f) => f.paths.length), (f) => f.id, flowChipEl, (el, f) => {
      el.textContent = f.count ? `≈${f.count} БПЛА` : "массово";
      return flowPopup(f, state.now);
    }, (f) => {
      const path = (f.paths.find((p) => p.kind === "main") || f.paths[0]).path;
      const k = Math.max(0, Math.floor((path.length - 1) / 2));
      const [a, b] = [path[k], path[k + 1] || path[k]];
      return [(a[1] + b[1]) / 2, (a[0] + b[0]) / 2];
    });
    const missiles = state.missiles || [];
    map.getSource("missiles").setData(fc(missiles.map((m) => line([[m.origin[1], m.origin[0]], [m.target[1], m.target[0]]], { state: m.state }))));
    syncMarkers(missileMarkers, missiles, (m) => m.id, missileEl, (el, m) => {
      const deg = bearingOf(m.origin, m.target);
      el.querySelector("svg").style.transform = `rotate(${deg}deg)`;
      el.classList.toggle("arrived", m.state === "arrived");
      el.querySelector("span").textContent = missileEta(m);
      missileMotion.set(m.id, { o: [m.origin[1], m.origin[0]], t: [m.target[1], m.target[0]], dist: m.dist_km, v: m.speed, km0: m.progress_km, at: performance.now() });
      return missilePopup(m, state.now);
    }, (m) => { const mo = missileMotion.get(m.id); return mo ? missilePos(mo, performance.now()) : [m.origin[1], m.origin[0]]; });
    for (const id of [...missileMotion.keys()]) if (!missileMarkers.has(id)) missileMotion.delete(id);
    syncMarkers(alertMarkers, [...marks, ...aggs], (a) => (a.agg ? `agg/${a.region}` : a.district || a.region), alertMarkEl, (el, a) => {
      // className не перезаписываем: MapLibre держит на элементе свой класс позиционирования
      el.classList.toggle("missile", a.level === "missile");
      el.classList.toggle("uav", a.level !== "missile");
      el.classList.toggle("agg", !!a.agg);
      el.classList.toggle("fine", !!a.fine);
      el.setAttribute("aria-label", `${LABELS[a.level]}: ${a.name}`);
      return `<b>${esc(a.name)}</b><br><span style="color:${COLORS[a.level]}">${LABELS[a.level]}</span>`
        + (a.agg ? `<br><span class="sub">Районов с опасностью: ${a.count}. Приблизьте карту — они покажутся по отдельности.</span>` : "")
        + `<br><span class="sub">${state.mode === "public" ? "Сообщение канала" : "Цель не зафиксирована"}, обновлено ${ago(a.updated, state.now)}</span>`
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
  const forecastMarkers = new Map();
  const missileMarkers = new Map();
  const missileMotion = new Map();   // id -> {o, t, dist, v, km0, at}: ракета «летит» по расчётной скорости между обновлениями
  const bearingOf = ([la1, lo1], [la2, lo2]) =>
    Math.atan2((lo2 - lo1) * Math.cos(((la1 + la2) / 2) * Math.PI / 180), la2 - la1) * 180 / Math.PI;
  const missileEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "missile-mark";
    el.setAttribute("aria-label", "Возможный полёт ракеты (расчёт)");
    el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 1.5c2.2 2.4 3.3 5.4 3.3 9v5.4l2.4 2.9v2.2l-3.2-1.1-2.5 2.1-2.5-2.1-3.2 1.1v-2.2l2.4-2.9v-5.4c0-3.6 1.1-6.6 3.3-9z"/></svg><span></span>`;
    return el;
  };
  function missilePos(mo, nowMs) {
    const km = Math.min(mo.km0 + mo.v * ((nowMs - mo.at) / 3.6e6) * timeScale, mo.dist);
    const f = mo.dist ? km / mo.dist : 1;
    return [mo.o[0] + (mo.t[0] - mo.o[0]) * f, mo.o[1] + (mo.t[1] - mo.o[1]) * f];
  }
  const missileEta = (m) => (m.state === "arrived" ? "прилёт возможен" : m.left_min[1] <= 1 ? "< 1 мин" : m.left_min[0] <= 0 ? `до ${m.left_min[1]} мин` : `≈ ${m.left_min[0]}–${m.left_min[1]} мин`);
  function missilePopup(m, now) {
    const kind = m.kind === "ballistic" ? "баллистическая" : "крылатая";
    return `<div class="pop-title">Ракетная опасность<span class="pop-badge missile">${esc(m.name)}</span></div>`
      + `<div class="pop-row">Расчёт: ${kind} ракета, ~${Math.round(m.speed)} км/ч, ~${m.dist_km} км от границы</div>`
      + `<div class="pop-row">${m.state === "arrived" ? "Расчётное время подлёта уже прошло" : `Подлёт, если ракета запущена при оповещении (${hhmm(m.t0)}): через ${etaRange(m.left_min)}`}</div>`
      + `<div class="pop-sub">Это математическая оценка по оповещению, а не фиксация ракеты. Если подлёт не подтвердится, через 10 минут после расчётного времени метка снимется.</div>`
      + srcLine(m.channels);
  }
  const flowArrowMarkers = new Map();
  const flowChipMarkers = new Map();
  const flowArrowEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "flow-arrow";
    el.setAttribute("aria-label", "Направление массового пролёта");
    el.innerHTML = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1l4.5 9.5L6 8 1.5 10.5z"/></svg>`;
    return el;
  };
  const flowChipEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "flow-chip";
    return el;
  };
  function flowPopup(f, now) {
    const list = (a) => a.map(esc).join(", ");
    return `<div class="pop-title">Массовый пролёт<span class="pop-badge flow">${f.count ? `≈${f.count} БПЛА` : "массово"}</span></div>`
      + (f.via.length ? `<div class="pop-row">Через: <b>${list(f.via)}</b></div>` : "")
      + (f.to.length ? `<div class="pop-row">В направлении: <b>${list(f.to)}</b>${f.beyond.length ? `<div class="sub">и далее: ${list(f.beyond)}</div>` : ""}</div>` : "")
      + `<div class="pop-sub">Коридор по сообщению канала, а не траектория: показывает, куда указывает канал. Обновлено ${ago(f.t, now)}.</div>`
      + srcLine(f.channels);
  }
  const forecastArrowEl = () => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "fc-arrow";
    el.setAttribute("aria-label", "Направление из прогноза");
    el.innerHTML = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1l4 9-4-2.2L2 10z"/></svg>`;
    return el;
  };
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
        m = new maplibregl.Marker({ element: makeEl() }).setLngLat(posOf(item)).addTo(map);
        bindPopup(m, m.getElement());
        store.set(key, m);
      }
      m.getElement()._html = update(m.getElement(), item);
    }
    for (const [key, m] of store) if (!keep.has(key)) { m.remove(); store.delete(key); }
  }

  // Цели рисуем DOM-маркерами: стрелка по курсу, число БПЛА рядом (без загрузки шрифтов для карты)
  const droneMarkers = new Map();
  // Движение между обновлениями: значок едет по прогнозу с расчётной скоростью цели.
  // Сервер присылает положение на момент снимка; дальше считаем сами, чтобы не было скачков раз в 15–30 с.
  const droneMotion = new Map();   // id -> {path: [[lon, lat]...], cum: [км], speed, at}
  let timeScale = cfg.timeScale || 1;  // в демо время ускорено
  const kmBetween = ([lo1, la1], [lo2, la2]) => {
    const k = Math.cos(((la1 + la2) / 2) * Math.PI / 180);
    return Math.hypot((lo2 - lo1) * k, la2 - la1) * 111.2;
  };
  const smoothPath = new Map();   // id -> сглаженный прогноз: значок едет ровно по нарисованной линии
  // Сколько км вдоль линии до точки, ближайшей к pos: линия начинается у последней фиксации,
  // а сервер присылает, где цель уже сейчас, — оттуда значок и продолжает путь
  function kmAlong(path, cum, pos) {
    let best = Infinity, km = 0;
    for (let i = 1; i < path.length; i++) {
      const [a, b] = [path[i - 1], path[i]];
      const k = Math.cos(a[1] * Math.PI / 180);
      const dx = (b[0] - a[0]) * k, dy = b[1] - a[1];
      const len2 = dx * dx + dy * dy;
      const f = len2 ? Math.min(Math.max((((pos[0] - a[0]) * k) * dx + (pos[1] - a[1]) * dy) / len2, 0), 1) : 0;
      const d = kmBetween(pos, [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]);
      if (d < best) { best = d; km = cum[i - 1] + (cum[i] - cum[i - 1]) * f; }
    }
    return km;
  }
  function setMotion(id, pr, pos) {
    if (!pr || pr.path.length < 2) { droneMotion.delete(id); return; }
    const path = smoothPath.get(id) || pr.path.map(([la, lo]) => [lo, la]);
    const cum = [0];
    for (let i = 1; i < path.length; i++) cum.push(cum[i - 1] + kmBetween(path[i - 1], path[i]));
    droneMotion.set(id, { path, cum, km0: kmAlong(path, cum, pos), speed: pr.speed, at: performance.now() });
  }
  function motionPos(m, nowMs) {
    const dist = m.km0 + m.speed * ((nowMs - m.at) / 3.6e6) * timeScale;
    const { path, cum } = m;
    if (dist >= cum[cum.length - 1]) return path[path.length - 1];
    let i = 1;
    while (cum[i] < dist) i++;
    const f = (dist - cum[i - 1]) / ((cum[i] - cum[i - 1]) || 1);
    return [path[i - 1][0] + (path[i][0] - path[i - 1][0]) * f, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * f];
  }
  let lastFrame = 0;
  function animateDrones(nowMs) {
    requestAnimationFrame(animateDrones);
    if (document.hidden || nowMs - lastFrame < 80) return;
    lastFrame = nowMs;
    for (const [id, mo] of missileMotion) {
      const mk = missileMarkers.get(id);
      if (mk) mk.setLngLat(missilePos(mo, nowMs));
    }
    for (const [id, m] of droneMotion) {
      const marker = droneMarkers.get(id);
      if (marker) {
        const pos = motionPos(m, nowMs);
        marker.setLngLat(pos);
        if (popupOwner === marker) popup.setLngLat(pos);   // окно едет вместе с целью
      }
    }
  }
  requestAnimationFrame(animateDrones);

  function placeDrone(tr, pos, bearing, now) {
    let m = droneMarkers.get(tr.id);
    if (!m) {
      const el = document.createElement("button");
      el.className = "drone";
      el.type = "button";
      el.innerHTML = `<i class="scope" aria-hidden="true"></i><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l8 18-8-4.5L4 20z"/></svg><b class="dot" aria-hidden="true">?</b><span></span>`;
      m = new maplibregl.Marker({ element: el, rotationAlignment: "map" }).setLngLat(pos).addTo(map);
      bindPopup(m, el);
      droneMarkers.set(tr.id, m);
    }
    setMotion(tr.id, tr.prediction, pos);
    const mo = droneMotion.get(tr.id);
    m.setLngLat(mo ? motionPos(mo, performance.now()) : pos);
    const el = m.getElement();
    el.setAttribute("aria-label", bearing == null ? `Цель #${tr.id}, курс неизвестен` : `Цель #${tr.id}`);
    el.querySelector("svg").style.transform = `rotate(${bearing ?? 0}deg)`;
    el.classList.toggle("no-course", bearing == null);
    el.classList.toggle("weak", !!tr.prediction && tr.prediction.basis === "inland");   // курс вероятный, а не наблюдаемый
    // цель без фиксаций дольше получаса бледнеет: она летит только по расчёту
    el.style.opacity = tr.coasting ? "0.7" : "1";
    el.classList.toggle("engaged", !!tr.engaged);
    el.querySelector("span").textContent = tr.count > 1 ? `×${tr.count}` : tr.mass ? "массово" : "";
    el._html = trackPopup(tr, now);
  }

  function trackPopup(tr, now) {
    const last = tr.points[tr.points.length - 1];
    const pr = tr.prediction;
    const where = `${esc(last.place)}${last.district_name && last.district_name !== last.place ? ` (${esc(last.district_name)})` : ""}`;
    let html = `<div class="pop-title">Цель #${tr.id}${tr.count > 1 ? ` · ${tr.count} БПЛА` : ""}`
      + `${tr.engaged ? `<span class="pop-badge missile">Работает ПВО</span>` : ""}</div>`;
    html += `<div class="pop-row">Последняя фиксация: <b>${where}</b><div class="sub">${ago(last.t, now)}</div></div>`;
    if (!pr) {
      html += `<div class="pop-sub">Одна фиксация: курс неизвестен, маршрут появится со второй точки.</div>`;
    } else {
      html += `<div class="pop-row">Курс ${pr.bearing}° · ~${pr.speed} км/ч <span class="sub">(${BASIS[pr.basis]})</span></div>`;
      if (now - last.t > 10 * 60) html += `<div class="pop-sub">Положение расчётное: цель летит дальше этим курсом, пока не дадут отбой или не сообщат о сбитии${tr.coasting ? ". Новых фиксаций давно нет — точность низкая" : ""}.</div>`;
      if (tr.target) html += `<div class="pop-row">Заявленная цель: <b>${esc(tr.target)}</b></div>`;
      const next = (pr.districts_eta || []).slice(0, 2).concat(pr.regions_eta.slice(0, 2));
      if (next.length) html += `<div class="pop-row">Дальше: ${next.map((r) => `${esc(r.name)} ${eta(r.eta_min)}`).join(", ")}</div>`;
    }
    return html + srcLine([...new Set(tr.points.flatMap((p) => p.sources))]);
  }

  // Подробная карта по коду доступа; без кода — только тревоги по областям и районам
  let accessWasFull = null;
  function renderAccess(state) {
    const full = state.mode === "full";
    const section = $("access-section");
    section.hidden = !(cfg.accessCodes || full);
    section.classList.toggle("unlocked", full);
    if (full !== accessWasFull) {
      accessWasFull = full;
      if (store.get("fold:access") === null) autoOpen(section, !full);   // без кода — раскрыта, с кодом — свёрнута
    }
    $("access-state").textContent = full ? "включена" : "для своих · по коду доступа";
    $("access-text").textContent = full
      ? "Вы видите подробную карту. Это расчётная схема по сообщениям каналов, а не реальное положение объектов. Не пересылайте её."
      : "Сейчас открыта публичная карта: только области и районы, где каналы объявили опасность. С кодом доступа добавляется:";
    $("access-form").hidden = full;
    $("access-hint").hidden = full;
    $("access-logout").hidden = !full;
    $("access-hint").textContent = cfg.contact ? `Кода нет? Напишите владельцу: ${cfg.contact}` : "Кода нет? Его выдаёт владелец сайта.";
    const perks = $("consent-perks");
    if (perks) perks.hidden = !cfg.accessCodes && !full;
  }
  $("access-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("access-code").value.trim();
    if (!code) return;
    const r = await fetch("api/access", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) }).catch(() => null);
    if (r && r.ok) { location.reload(); return; }
    $("access-error").hidden = false;
    $("access-error").textContent = r && r.status === 429 ? "Слишком много попыток. Подождите 10 минут." : "Неверный код.";
  });
  $("access-logout").addEventListener("click", async () => {
    await fetch("api/logout", { method: "POST" }).catch(() => null);
    location.reload();
  });

  // Связь с каналами: «пусто на карте» не должно выглядеть как «угроз нет», если данные просто не приходят
  let healthDismissedAt = 0;
  function renderHealth(state) {
    const bar = $("health-bar");
    const h = state.health || {};
    const chans = Object.values(h.channels || {});
    if (cfg.demo || !chans.length) {
      bar.hidden = true;
      $("status-line").textContent = `${cfg.demo ? "ДЕМО · " : ""}обновлено ${hhmm(state.now)}`;
      return;
    }
    const ok = chans.filter((c) => c.ok);
    const failed = chans.filter((c) => c.ok === false);
    const lastOk = Math.max(0, ...chans.map((c) => c.last_ok || 0));
    const interval = h.interval || 30;
    const staleMin = lastOk ? Math.round((state.now - lastOk) / 60) : null;
    let text = "", cls = "health-bar";
    if (h.warming) {
      // после перезапуска сервер сначала загружает историю каналов: это не поломка
      $("status-line").textContent = `загружаем историю · ${ok.length}/${chans.length} каналов`;
      bar.hidden = true;
      return;
    }
    $("status-line").textContent = `${ok.length ? "онлайн" : "нет связи"} · ${ok.length}/${chans.length} каналов · обновлено ${hhmm(state.now)}`;
    if (!ok.length && failed.length) {
      const err = failed.find((c) => c.error);
      text = `Нет связи с Telegram-каналами — карта не обновляется${staleMin != null ? ` (последние данные ${staleMin} мин назад)` : ""}.`
        + (err ? ` Ошибка: ${err.error}` : "");
    } else if (lastOk && state.now - lastOk > interval * 6) {
      cls = "health-bar stale";
      text = `Каналы давно не читались (${staleMin} мин) — данные могут быть устаревшими.`;
    } else if (failed.length >= Math.max(3, chans.length / 3)) {
      cls = "health-bar stale";
      const err = failed.find((c) => c.error);
      text = `Не читается ${failed.length} из ${chans.length} каналов — часть сообщений может не попасть на карту.` + (err ? ` (${err.error})` : "");
    }
    // закрытое сообщение не возвращается 15 минут, пока причина та же
    const hidden = !text || (healthDismissedAt && Date.now() - healthDismissedAt < 15 * 60 * 1000);
    $("health-text").textContent = text;
    bar.className = cls;
    bar.hidden = hidden;
  }
  $("health-close").addEventListener("click", () => { healthDismissedAt = Date.now(); $("health-bar").hidden = true; });

  // Что за сообщение: по самому важному из распознанных событий
  const FEED_LEVELS = [
    ["missile_alert", "missile", "Ракетная опасность"], ["flow", "flow", "Массовый пролёт"], ["uav_alert", "uav", "Тревога по БПЛА"], ["uav_warn", "warn", "Внимание"],
    ["clear", "clear", "Отбой"], ["destroyed", "shoot", "Сбитие"], ["engaged", "shoot", "Работа ПВО"],
    ["sighting", "sight", "Фиксация БПЛА"], ["heading", "sight", "Курс БПЛА"], ["launch", "launch", "Пуск"],
  ];
  const feedLevel = (kinds) => {
    const hit = FEED_LEVELS.find(([k]) => kinds.includes(k));
    return hit ? { key: hit[1], label: hit[2] } : { key: "info", label: "Сообщение" };
  };

  function renderPanel(state) {
    // слои, выключенные на сервере (PUBLISH_*), убираем и из легенды
    const pub = state.publish || {};
    document.querySelectorAll("[data-layer]").forEach((el) => { el.hidden = pub[el.dataset.layer] === false; });
    const regions = Object.entries(state.regions).sort((a, b) =>
      (SEVERITY[b[1].status] - SEVERITY[a[1].status]) || ((a[1].eta_min ?? 0) - (b[1].eta_min ?? 0)));
    const active = state.tracks.filter((t) => t.status === "active");
    const counts = { alert: regions.filter(([, r]) => r.status === "uav" || r.status === "missile").length,
      warn: regions.filter(([, r]) => r.status === "warn").length, pred: regions.filter(([, r]) => r.status === "predicted").length };

    // в публичном режиме дронов нет — и счётчиков целей тоже
    $("summary").innerHTML = `
      <div class="stat"><b>${counts.alert}</b><span>регионов с тревогой</span></div>`
      + (pub.tracks === false
        ? `<div class="stat"><b>${counts.warn}</b><span>с режимом «внимание»</span></div>`
        : `<div class="stat"><b>${active.reduce((s, t) => s + t.count, 0)}</b><span>целей в воздухе</span></div>
      <div class="stat"><b>${counts.pred}</b><span>на пути подлёта</span></div>`);
    renderAccess(state);

    $("track-list").innerHTML = active.length ? active.map((tr) => {
      const last = tr.points[tr.points.length - 1];
      const pr = tr.prediction;
      const nextArea = pr && ((pr.districts_eta && pr.districts_eta[0]) || pr.regions_eta[0]);
      const next = nextArea ? `→ ${esc(nextArea.name)} ${eta(nextArea.eta_min)}` : (tr.target ? `→ ${esc(tr.target)}` : "курс не определён");
      return `<li data-track="${tr.id}">
        <div class="row"><span>#${tr.id} · ${esc(last.place)}${tr.count > 1 ? ` · ×${tr.count}` : ""}</span><span class="sub">${ago(last.t, state.now)}</span></div>
        <div class="sub">${next}</div></li>`;
    }).join("") : `<li class="empty">Целей не зафиксировано</li>`;

    const miss = state.missiles || [];
    $("missile-section").hidden = !miss.length || pub.tracks === false;
    $("missile-list").innerHTML = miss.map((m) => `<li data-region="${esc(m.id)}">
      <div class="row"><span>${esc(m.name)}</span><span class="badge missile">${m.kind === "ballistic" ? "баллист." : "крылатые"}</span></div>
      <div class="sub">${m.state === "arrived" ? "расчётное время подлёта прошло" : `подлёт ${etaRange(m.left_min)}`} · ~${m.dist_km} км от границы</div></li>`).join("");

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

    const full = state.mode !== "public";
    $("feed-sub").textContent = full
      ? "Все сообщения, которые прочитал наш бот. Нажмите на сообщение — карта покажет область."
      : "Тревоги, которые наш бот прочитал в открытых каналах. Нажмите на сообщение — карта покажет область.";
    $("feed").innerHTML = state.feed.length ? state.feed.slice(0, full ? 30 : 40).map((m) => {
      const lv = feedLevel(m.kinds || []);
      const region = (m.regions || []).find((r) => regionGeoms[r]);
      return `<li class="lv-${lv.key}"${region ? ` data-region="${esc(region)}"` : ""}>
        <div class="row"><span class="feed-badge ${lv.key}">${lv.label}</span><span class="sub">${hhmm(m.t)}</span></div>
        <div class="text">${esc(m.text)}</div>
        <div class="sub"><a class="src" href="${tgLink(m.channel, m.msg_id)}" target="_blank" rel="noopener">@${esc(m.channel)}</a>${(m.regions || []).length ? ` · ${(m.regions || []).slice(0, 3).map((r) => esc(regionNames[r] || r)).join(", ")}` : ""}</div></li>`;
    }).join("") : `<li class="empty">Сообщений пока нет</li>`;

    renderHealth(state);
    const alertCount = regions.filter(([, r]) => r.status !== "predicted").length;
    $("panel-open").classList.toggle("alert", alertCount > 0);
    $("panel-open-text").textContent = alertCount ? `Тревога: ${alertCount} регион${alertCount === 1 ? "" : alertCount < 5 ? "а" : "ов"}` : "Обстановка";

    const fc_ = state.forecast;
    $("forecast").hidden = !fc_;
    if (fc_) {
      const sm = fc_.summary || { title: "Прогноз обстановки", directions: [], notes: [] };
      $("forecast-title").textContent = sm.title;
      $("forecast-expected").hidden = !sm.expected;
      $("forecast-expected").textContent = sm.expected ? `ожидается ${sm.expected}` : "";
      $("forecast-directions").innerHTML = sm.directions.map((d) => `<li>${esc(d)}</li>`).join("");
      $("forecast-notes").innerHTML = sm.notes.map((n) => `<li>${esc(n)}</li>`).join("");
      $("forecast-text").textContent = fc_.text;
      $("forecast-meta").innerHTML = `Прогноз канала <a href="${tgLink(fc_.channel, fc_.msg_id)}" target="_blank" rel="noopener">@${esc(fc_.channel)}</a> от ${hhmm(fc_.t)}. `
        + "Это оценка канала, а не данные наблюдения: направления на карте — тусклый пунктир.";
    }
  }

  $("missile-list").addEventListener("click", (e) => {
    const li = e.target.closest("[data-region]");
    if (li && regionGeoms[li.dataset.region]) flyToRegion(li.dataset.region);
  });
  $("feed").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
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
    const FRAME_MS = 700;
    timeScale = frames.length > 1 ? ((frames[1].now - frames[0].now) * 1000) / FRAME_MS : 1;
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
    setInterval(() => { if (!paused) { i = (i + 1) % frames.length; step(); } }, FRAME_MS);
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

  $("consent-channels").innerHTML = (cfg.channels || []).length
    ? cfg.channels.map((c) => `<span>@${esc(c.channel)}</span>`).join("")
    : `<span>список — в пользовательском соглашении</span>`;

  const setLegend = (open) => {
    $("legend").classList.toggle("open", open);
    $("legend-toggle").setAttribute("aria-expanded", String(open));
    store.set("legendOpen", open ? "1" : "0");
  };
  $("legend-toggle").addEventListener("click", () => setLegend(!$("legend").classList.contains("open")));
  $("legend").classList.toggle("open", store.get("legendOpen") === "1");
  $("legend-toggle").setAttribute("aria-expanded", String(store.get("legendOpen") === "1"));

  // Блоки панели сворачиваются; выбор запоминается (по умолчанию открыты ключевые, остальные свёрнуты)
  const FOLD_DEFAULT_CLOSED = new Set(["forecast", "heat", "launch", "sources"]);
  // программное раскрытие/сворачивание не считается выбором пользователя и не запоминается
  function autoOpen(d, open) {
    if (d.open === open) return;
    d.dataset.auto = "1";
    d.open = open;
  }
  document.querySelectorAll("details[data-fold]").forEach((d) => {
    const saved = store.get(`fold:${d.dataset.fold}`);
    if (saved === "0" || (saved === null && FOLD_DEFAULT_CLOSED.has(d.dataset.fold))) autoOpen(d, false);
    if (saved === "1") autoOpen(d, true);
    d.addEventListener("toggle", () => {
      if (d.dataset.auto) { delete d.dataset.auto; return; }
      store.set(`fold:${d.dataset.fold}`, d.open ? "1" : "0");
    });
  });

  // Панель убирается целиком (крестик), вернуть — кнопкой «Обстановка»; выбор запоминается
  function setClosed(closed) {
    $("panel").classList.toggle("closed", closed);
    $("panel-open").hidden = !closed;
    store.set("panelClosed", closed ? "1" : "");
    requestAnimationFrame(fitPadding);
  }
  $("panel-toggle").addEventListener("click", () => { setClosed(true); $("panel-open").focus(); });
  $("panel-open").addEventListener("click", () => { setClosed(false); $("panel-toggle").focus(); });
  setClosed(store.get("panelClosed") === "1");
  window.addEventListener("resize", fitPadding);
})();

