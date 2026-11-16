"use strict";

const repository = "onetapggt-maker/fortnite-chest-radar";
const github = `https://github.com/${repository}`;
const issueApi = `https://api.github.com/repos/${repository}/issues?state=open&per_page=100`;
const canvas = document.querySelector("#island");
const ctx = canvas.getContext("2d");
const placeButton = document.querySelector("#report");
const status = document.querySelector("#map-note");
const reportStatus = document.querySelector("#report-status");
const refresh = document.querySelector("#refresh");
const picture = new Image();

const layers = {
  chest: { label: "Обычные", color: "#ffd476", points: [] },
  rare_chest: { label: "Редкие", color: "#62e5ff", points: [] },
  buried_chest: { label: "Закопанные", color: "#f3a9a0", points: [] },
  sightings: { label: "Наблюдения", color: "#c788ff", points: [] },
};
let data;
let controls = {};
let view = { x: 0, y: 0, zoom: 1 };
let screen = { w: 1, h: 1, fit: 1 };
let reports = [];
let selectedReport = null;
let placing = false;
let gesture = null;

function parseObservation(issue, map) {
  if (issue.pull_request || !Number.isInteger(issue.number) || typeof issue.body !== "string") return null;
  const match = issue.body.match(/^RADAR-43\.10: (area|map) (\d{1,4}),(\d{1,4})\s*$/m);
  if (!match) return null;
  const x = Number(match[2]);
  const y = Number(match[3]);
  return x >= 0 && x < map.width && y >= 0 && y < map.height
    ? { x, y, number: issue.number, kind: match[1] }
    : null;
}

async function observations() {
  refresh.disabled = true;
  reportStatus.textContent = "Загружаю наблюдения…";
  try {
    const response = await fetch(issueApi, { headers: { Accept: "application/vnd.github+json" } });
    if (!response.ok) throw new Error(String(response.status));
    reports = (await response.json()).map(issue => parseObservation(issue, data)).filter(Boolean);
    layers.sightings.points = reports;
    controls.sightings.count.textContent = reports.length;
    document.querySelector("#recent").replaceChildren(...reports.slice(0, 10).map(report => {
      const entry = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = `№${report.number} · ${report.x}, ${report.y}`;
      button.addEventListener("click", () => {
        selectedReport = report;
        view = { x: report.x, y: report.y, zoom: Math.max(view.zoom, 3) };
        draw();
      });
      entry.append(button);
      return entry;
    }));
    reportStatus.textContent = reports.length ? `На карте: ${reports.length}` : "Пока нет опубликованных наблюдений.";
  } catch {
    reportStatus.textContent = "GitHub не ответил. Попробуй обновить позже.";
  } finally {
    refresh.disabled = false;
    draw();
  }
}

function initLayers() {
  const items = document.querySelector("#layers");
  controls = Object.fromEntries(Object.entries(layers).map(([key, layer]) => {
    const row = document.createElement("label");
    row.className = "layer";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = true;
    input.addEventListener("change", draw);
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.setProperty("--dot", layer.color);
    const count = document.createElement("output");
    count.textContent = layer.points.length;
    row.append(input, dot, document.createTextNode(layer.label), count);
    items.append(row);
    return [key, { input, count }];
  }));
}

function resize() {
  const bounds = canvas.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(bounds.width * ratio);
  canvas.height = Math.round(bounds.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  screen = { w: bounds.width, h: bounds.height,
    fit: Math.min(bounds.width / data.width, bounds.height / data.height) };
  draw();
}

function reset() {
  view = { x: data.width / 2, y: data.height / 2, zoom: 1 };
  selectedReport = null;
  draw();
}

function draw() {
  if (!data) return;
  ctx.clearRect(0, 0, screen.w, screen.h);
  const scale = screen.fit * view.zoom;
  const left = screen.w / 2 - view.x * scale;
  const top = screen.h / 2 - view.y * scale;
  ctx.drawImage(picture, left, top, data.width * scale, data.height * scale);
  for (const [key, layer] of Object.entries(layers)) {
    if (!controls[key].input.checked) continue;
    ctx.fillStyle = layer.color;
    ctx.strokeStyle = layer.color;
    for (const point of layer.points) {
      const x = left + point.x * scale;
      const y = top + point.y * scale;
      if (x < -50 || y < -50 || x > screen.w + 50 || y > screen.h + 50) continue;
      ctx.beginPath();
      const r = key === "sightings" ? Math.max(8, 22 * scale) : Math.min(4, 1.4 + view.zoom / 3);
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      if (key === "sightings") {
        ctx.globalAlpha = .22;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.lineWidth = selectedReport?.number === point.number ? 3 : 1.5;
        ctx.stroke();
      } else {
        ctx.fill();
      }
    }
  }
}

function toWorld(x, y) {
  const scale = screen.fit * view.zoom;
  return {
    x: view.x + (x - screen.w / 2) / scale,
    y: view.y + (y - screen.h / 2) / scale,
  };
}

function zoom(factor, x = screen.w / 2, y = screen.h / 2) {
  const anchor = toWorld(x, y);
  view.zoom = Math.max(.85, Math.min(12, view.zoom * factor));
  const after = toWorld(x, y);
  view.x += anchor.x - after.x;
  view.y += anchor.y - after.y;
  draw();
}

function setPlacing(enabled) {
  placing = enabled;
  canvas.classList.toggle("placing", enabled);
  placeButton.textContent = enabled ? "Отменить" : "Добавить наблюдение";
  status.textContent = enabled ? "Укажи примерный район на карте" : "Колесо для масштаба · перетаскивание для обзора";
}

function choosePoint(x, y) {
  const world = toWorld(x, y);
  const px = Math.round(world.x);
  const py = Math.round(world.y);
  if (px < 0 || py < 0 || px >= data.width || py >= data.height) return;
  const params = new URLSearchParams({
    title: "Наблюдение сундука",
    body: `Примерный район, отмеченный на карте Fortnite v43.10.\n\nRADAR-43.10: map ${px},${py}`,
  });
  document.querySelector("#picked").textContent = `Отмеченный район: ${px}, ${py}.`;
  document.querySelector("#issue-link").href = `${github}/issues/new?${params}`;
  setPlacing(false);
  document.querySelector("#report-dialog").showModal();
}

function bindMap() {
  new ResizeObserver(resize).observe(canvas);
  placeButton.disabled = false;
  placeButton.addEventListener("click", () => setPlacing(!placing));
  document.querySelector("#reset").addEventListener("click", reset);
  document.querySelectorAll("[data-zoom]").forEach(button =>
    button.addEventListener("click", () => zoom(Number(button.dataset.zoom))));
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15, event.offsetX, event.offsetY);
  }, { passive: false });
  canvas.addEventListener("pointerdown", event => {
    if (!event.isPrimary || event.button !== 0) return;
    gesture = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY };
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", event => {
    if (!event.isPrimary || !gesture) return;
    const scale = screen.fit * view.zoom;
    view.x -= (event.clientX - gesture.x) / scale;
    view.y -= (event.clientY - gesture.y) / scale;
    gesture.x = event.clientX;
    gesture.y = event.clientY;
    draw();
  });
  canvas.addEventListener("pointerup", event => {
    if (!event.isPrimary || !gesture) return;
    const movement = Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY);
    if (movement < 6 && placing) choosePoint(event.offsetX, event.offsetY);
  });
  canvas.addEventListener("lostpointercapture", () => {
    gesture = null;
    canvas.classList.remove("dragging");
  });
  canvas.addEventListener("keydown", event => {
    const step = 45 / (screen.fit * view.zoom);
    switch (event.key) {
      case "ArrowLeft": view.x -= step; break;
      case "ArrowRight": view.x += step; break;
      case "ArrowUp": view.y -= step; break;
      case "ArrowDown": view.y += step; break;
      case "+": case "=": zoom(1.35); break;
      case "-": zoom(0.74); break;
      case "Escape": setPlacing(false); break;
      case "Enter": if (placing) choosePoint(screen.w / 2, screen.h / 2); break;
      default: return;
    }
    event.preventDefault();
    draw();
  });
}

async function start() {
  try {
    picture.src = "island.png";
    const response = await fetch("chests.json");
    if (!response.ok) throw new Error(String(response.status));
    const map = await response.json();
    await picture.decode();
    for (const [type, x, y] of map.points) {
      const layer = layers[map.types[type]];
      if (layer) layer.points.push({ x, y });
    }
    data = map;
    initLayers();
    reset();
    bindMap();
    setPlacing(false);
    refresh.addEventListener("click", observations);
    observations();
  } catch {
    status.textContent = "Не удалось загрузить карту. Обнови страницу.";
    reportStatus.textContent = "Наблюдения недоступны до загрузки карты.";
    refresh.disabled = true;
  }
}

start();
