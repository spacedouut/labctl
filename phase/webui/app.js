"use strict";
const $ = id => document.getElementById(id);
const SECTIONS = ["machine", "disks", "network", "others"];
const TOKEN_KEY = "phase_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let auth = {mode: "token", authenticated: false, user: null};
let meta = null;
let currentVm = null;

// ---- create-wizard state (the template owns its system disk) ------------
let state = {
  name: "", description: "", tags: [], onboot: true, protect: false,
  size: "", cores: "", memory: "", gpu: "", os: "",
  disks: [],
  hardware: { firmware: "bios", secure_boot: false, tpm: false, efi_storage: "", tpm_storage: "", display: "default", audio: "none" },
  bridge: "", vlan: "", ipmode: "dhcp", ip: "", gw: "",
  bootstrap: { system: false, docker: false, tailscale: false },
  ssh_keys: [],
};
let errors = {};

// ------------------------------------------------------------------------
// api

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  if (token) opts.headers["X-Phase-Token"] = token;
  let r = await fetch(path, opts);
  if (r.status === 401 && auth.mode === "token") {
    token = prompt("Access token for this phase instance:") || "";
    if (token) localStorage.setItem(TOKEN_KEY, token);
    opts.headers["X-Phase-Token"] = token;
    r = await fetch(path, opts);
  }
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || `request failed (${r.status})`);
  return body;
}

async function boot() {
  const r = await fetch("/api/auth/session", {cache:"no-store"});
  auth = await r.json();
  if (auth.mode === "pam" && !auth.authenticated) {
    $("app-shell").classList.add("hide");
    $("login-screen").classList.remove("hide");
    $("login-user").focus();
    return;
  }
  $("login-screen").classList.add("hide");
  $("app-shell").classList.remove("hide");
  init();
}
async function login(e) {
  e.preventDefault();
  const error = $("login-error"); error.textContent = "";
  const submit = $("login-form").querySelector("button"); submit.disabled = true;
  try {
    const r = await fetch("/api/auth/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username:$("login-user").value.trim(), password:$("login-password").value})});
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "Sign-in failed");
    $("login-password").value = "";
    auth = {mode:"pam", authenticated:true, user:body.user};
    $("login-screen").classList.add("hide"); $("app-shell").classList.remove("hide"); init();
  } catch (err) { error.textContent = err.message; }
  finally { submit.disabled = false; }
}

// ------------------------------------------------------------------------
// toast / workspace navigation

function toast(msg, cls, ms) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (cls ? " " + cls : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = "", ms || 3500);
}
function showTab(tab) {
  ["dashboard", "create", "vms", "storage", "detail", "settings"].forEach(t => $("view-" + t).classList.toggle("hide", t !== tab));
  const navTab = tab === "detail" ? "vms" : tab;
  document.querySelectorAll(".side-nav [data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === navTab));
  $("crumb").textContent = ({dashboard:"OVERVIEW", create:"CREATE VM", vms:"INVENTORY", storage:"STORAGE", detail:"INVENTORY", settings:"SETTINGS"})[tab] || "PHASE";
  $("plan-loader").classList.toggle("hide", tab !== "create");
  if (tab === "vms") loadVms();
  if (tab === "storage") loadStorage();
  if (tab === "dashboard") loadDashboard();
  if (tab === "settings") loadSettings();
}

// ------------------------------------------------------------------------
// create wizard

function init() {
  meta = null;
  $("rail-plan").textContent = "loading…";
  api("/api/meta").then(m => {
    meta = m;
    $("hostline").textContent = m.host || "";
    buildSizeChips();
    const images = (m.system_images || []).map(i => [`${i.os} · ${i.disk}`, i.os]);
    fillSelect($("f-os"), images.length ? images : m.oses);
    fillSelect($("f-bridge"), ["", ...(m.networks || [])].map(v => [v || "(template default)", v]));
    fillSelect($("f-sshkey"), ["", ...(m.ssh_keys || [])].map(v => [v || "(none)", v]));
    const storages = ["", ...(m.storages || [])];
    fillSelect($("disk-new-storage"), storages.map(v => [v || "Select storage", v]));
    fillSelect($("f-efi-storage"), storages.map(v => [v || "Default storage", v]));
    fillSelect($("f-tpm-storage"), storages.map(v => [v || "Default storage", v]));
    fillSelect($("loadplan"), ["", ...(m.plans || []).map(p => p.name)].map(v => [v || "— new VM —", v]));
    if (m.sizes && m.sizes.small) state.size = "small";
    else if (m.sizes) state.size = Object.keys(m.sizes)[0];
    if (m.oses && m.oses.length) state.os = m.oses[0];
    buildStepper();
    renderAll();
    debouncedValidate();
    if (!$("view-dashboard").classList.contains("hide")) loadDashboard();
  }).catch(e => {
    $("rail-plan").textContent = "Enter the access token to load this console.";
    toast(e.message || "Could not load phase", "err", 6000);
  });
}
function fillSelect(sel, pairs) {
  sel.innerHTML = "";
  for (const item of pairs) {
    // accept both [label, value] pairs and bare strings (os list)
    const [label, value] = Array.isArray(item) ? item : [item, item];
    const o = document.createElement("option");
    o.textContent = label; o.value = value; sel.appendChild(o);
  }
}
function buildSizeChips() {
  const box = $("size-chips");
  box.innerHTML = "";
  for (const [name, s] of Object.entries(meta.sizes || {})) {
    const c = document.createElement("div");
    c.className = "chip" + (state.size === name ? " sel" : "");
    c.dataset.size = name;
    const gb = (s.memory / 1024).toFixed(s.memory % 1024 ? 1 : 0);
    c.innerHTML = `<div class="n">${name}</div><div class="d">${s.cores} vCPU · ${gb} GB</div>`;
    c.addEventListener("click", () => { state.size = name; buildSizeChips(); debouncedValidate(); });
    box.appendChild(c);
  }
}
function showSection(name) {
  SECTIONS.forEach(s => $("pane-" + s).classList.toggle("hide", s !== name));
  document.querySelectorAll(".stepper [data-sec]").forEach(b =>
    b.classList.toggle("active", b.dataset.sec === name));
}

// --- stepper (GCP-style section list with summary subtext) --------------
function buildStepper() {
  const nav = $("stepper");
  nav.innerHTML = "";
  SECTIONS.forEach(s => {
    const b = document.createElement("button");
    b.dataset.sec = s;
    b.innerHTML = `<span class="s-title"><span class="s-dot"></span>${s[0].toUpperCase() + s.slice(1)}</span>`
      + `<div class="s-sub"></div>`;
    b.addEventListener("click", () => {
      document.querySelectorAll(".stepper [data-sec]").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      showSection(s);
    });
    nav.appendChild(b);
  });
}
// --- error field highlighting -------------------------------------------
function highlightErrors(all) {
  // map backend error strings to form controls
  const map = [
    ["name is required", "f-name"], ["name must match", "f-name"], ["name too long", "f-name"],
    ["pick a size", "size-chips"],
    ["cores must be", "f-cores"], ["memory must be", "f-memory"],
    ["vlan must be", "f-vlan"],
    ["static IP required", "f-ip"], ["is not a static IP", "f-ip"],
    ["gateway doesn't look like an IP", "f-gw"],
  ];
  const bad = new Set();
  all.forEach(e => map.forEach(([pat, id]) => { if (e.includes(pat)) bad.add(id); }));
  document.querySelectorAll(".field.bad, .chips.bad").forEach(el => el.classList.remove("bad"));
  bad.forEach(id => {
    const el = $(id);
    if (!el) return;
    (el.classList.contains("chips") ? el : el.closest(".field") || el).classList.add("bad");
  });
}
function updateStepper() {
  SECTIONS.forEach((s, i) => {
    const b = document.querySelector(`.stepper [data-sec="${s}"]`);
    if (!b) return;
    const errs = (errors[i + 1] || []).length;
    b.querySelector(".s-dot").className = "s-dot" + (errs ? " bad" : " ok");
    let sub = "—";
    if (s === "machine") sub = `${state.name || "(unnamed)"} · ${state.size || "?"}`;
    else if (s === "disks") sub = `${state.disks.length} disk(s)`;
    else if (s === "network") sub = `${state.bridge || "default"} · ${state.ipmode}`;
    else if (s === "others") sub = state.tags.join(",")
      || (state.bootstrap.system || state.bootstrap.docker || state.bootstrap.tailscale
          ? "bootstrap" : "—");
    b.querySelector(".s-sub").textContent = sub;
  });
}

// --- equivalent command (GCP "Equivalent code" analog) -------------------
function buildCommand(st) {
  const a = ["phase", "vm", "create", "--name", st.name, "--size", st.size];
  if (st.cores) a.push("--cores", st.cores);
  if (st.memory) a.push("--memory", st.memory);
  a.push("--os", st.os);
  st.disks.forEach(d =>
    a.push("--disk", `${d.id}:data:${d.size}` + (d.storage ? `:${d.storage}` : "")));
  if (st.bridge) a.push("--bridge", st.bridge);
  if (st.vlan) a.push("--vlan", st.vlan);
  if (st.ipmode === "static" && st.ip) { a.push("--ip", st.ip); if (st.gw) a.push("--gw", st.gw); }
  if (st.tags.length) st.tags.forEach(t => a.push("--tag", t));
  ["system", "docker", "tailscale"].forEach(k => { if (st.bootstrap[k]) a.push("--" + k); });
  if (st.gpu) a.push("--gpu", st.gpu);
  return a.join(" ");
}

// --- additional data disks ---------------------------------------------
function renderDisks() {
  const tb = $("disk-table").querySelector("tbody");
  tb.innerHTML = "";
  state.disks.forEach((d, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${d.id}</td><td>${d.size || "—"}</td><td>${d.storage || "—"}</td>
      <td class="del" title="remove">✕</td>`;
    tr.querySelector(".del").addEventListener("click", () => {
      state.disks.splice(i, 1);
      renderDisks();
      debouncedValidate();
    });
    tb.appendChild(tr);
  });
}
function addDisk() {
  const bus = $("disk-new-bus").value;
  const size = $("disk-new-size").value.trim();
  const storage = $("disk-new-storage").value.trim();
  if (!size || !storage) { toast("size and storage required", "err"); return; }
  const used = new Set(state.disks.map(d => d.id)); let n = 0;
  while (used.has(bus + n)) n++;
  state.disks.push({ id: bus + n, size, storage });
  $("disk-new-size").value = "";
  renderDisks();
  debouncedValidate();
}
function toBackend(st) {
  // phase's plan schema still identifies the template disk internally;
  // there is deliberately no user-facing boot-disk choice or boot order.
  return {
    name: st.name, description: st.description, tags: st.tags,
    onboot: st.onboot, protect: st.protect, size: st.size,
    cores: st.cores, memory: st.memory, gpu: st.gpu, os: st.os,
    system_disk_id: st.system_disk_id || "scsi0", os_disk_size: "", os_disk_storage: "",
    data_disks: st.disks.map(d => ({ id: d.id, size: d.size, storage: d.storage })),
    bridge: st.bridge, vlan: st.vlan, ipmode: st.ipmode, ip: st.ip, gw: st.gw,
    bootstrap: st.bootstrap, ssh_keys: st.ssh_keys,
    hardware: st.hardware,
  };
}

// --- form read/write ----------------------------------------------------
function readForm() {
  state.name = $("f-name").value.trim();
  state.description = $("f-desc").value.trim();
  state.cores = $("f-cores").value.trim();
  state.memory = $("f-memory").value.trim();
  state.os = $("f-os").value;
  const image = (meta.system_images || []).find(i => i.os === state.os);
  state.system_disk_id = image ? image.disk : "scsi0";
  state.bridge = $("f-bridge").value;
  state.vlan = $("f-vlan").value.trim();
  state.ipmode = document.querySelector("input[name=ipmode]:checked").value;
  state.ip = $("f-ip").value.trim();
  state.gw = $("f-gw").value.trim();
  state.tags = $("f-tags").value.split(",").map(t => t.trim()).filter(Boolean);
  state.onboot = $("f-onboot").checked;
  state.protect = $("f-protect").checked;
  state.gpu = $("f-gpu").value.trim();
  state.hardware = { firmware: $("f-firmware").value, secure_boot: $("f-secureboot").checked,
    tpm: $("f-tpm").checked, efi_storage: $("f-efi-storage").value,
    tpm_storage: $("f-tpm-storage").value, display: $("f-display").value, audio: $("f-audio").value };
  state.bootstrap.system = $("f-bs-system").checked;
  state.bootstrap.docker = $("f-bs-docker").checked;
  state.bootstrap.tailscale = $("f-bs-tailscale").checked;
  state.ssh_keys = $("f-sshkey").value ? [$("f-sshkey").value] : [];
}
function writeForm() {
  $("f-name").value = state.name;
  $("f-desc").value = state.description;
  $("f-cores").value = state.cores;
  $("f-memory").value = state.memory;
  $("f-os").value = state.os || (meta.oses[0] || "");
  $("f-bridge").value = state.bridge;
  $("f-vlan").value = state.vlan;
  const mode = document.querySelector(`input[name=ipmode][value=${state.ipmode}]`);
  if (mode) mode.checked = true;
  $("f-ip").value = state.ip;
  $("f-gw").value = state.gw;
  $("f-tags").value = state.tags.join(", ");
  $("f-onboot").checked = !!state.onboot;
  $("f-protect").checked = !!state.protect;
  $("f-gpu").value = state.gpu;
  const hw = Object.assign({firmware:"bios", secure_boot:false, tpm:false, efi_storage:"", tpm_storage:"", display:"default", audio:"none"}, state.hardware || {});
  $("f-firmware").value = hw.firmware; $("f-secureboot").checked = hw.secure_boot; $("f-tpm").checked = hw.tpm;
  $("f-efi-storage").value = hw.efi_storage; $("f-tpm-storage").value = hw.tpm_storage; $("f-display").value = hw.display; $("f-audio").value = hw.audio;
  $("f-bs-system").checked = !!state.bootstrap.system;
  $("f-bs-docker").checked = !!state.bootstrap.docker;
  $("f-bs-tailscale").checked = !!state.bootstrap.tailscale;
  $("f-sshkey").value = state.ssh_keys[0] || "";
  updateStaticVisibility();
  renderDisks();
}
function updateStaticVisibility() {
  $("static-fields").style.display =
    document.querySelector("input[name=ipmode]:checked").value === "static" ? "" : "none";
}
function renderAll() {
  writeForm();
  buildSizeChips();
  showSection("machine");
}

// --- validation + review -------------------------------------------------
let validateTimer = null;
function debouncedValidate() { clearTimeout(validateTimer); validateTimer = setTimeout(validate, 400); }
async function validate() {
  readForm();
  const res = await api("/api/plan", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: toBackend(state) }),
  });
  errors = res.errors || {};
  const all = [];
  for (let i = 1; i <= 5; i++) if (errors[i]) all.push(...errors[i]);
  $("errors").textContent = all.length ? "✗ " + all.join("\n") : "";
  highlightErrors(all);
  const ok = !!res.plan;
  ["act-create", "act-provision", "act-save"].forEach(id => $(id).disabled = !ok);
  if (res.plan) {
    $("rail-plan").textContent = res.plan_text;
    $("rail-cmd").textContent = buildCommand(state);
  } else {
    $("rail-plan").textContent = "complete the highlighted fields";
  }
  updateStepper();
}

// --- actions -------------------------------------------------------------
async function runCreateAction(kind) {
  readForm();
  const res = await api("/api/" + kind, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: toBackend(state) }),
  });
  if (res.error) { toast(res.error, "err"); return; }
  if (res.task) { setBusy(true, "Provisioning…"); pollTask(res.task, () => { setBusy(false); toast(`Provisioned ${state.name}.`, "ok"); validate(); }); }
  else if (res.vmid) { toast(`Created ${state.name} (VMID ${res.vmid}), stopped.`, "ok"); validate(); }
  else if (res.path) toast("Saved plan → " + res.path, "ok");
}
function setBusy(busy, msg) {
  ["act-create", "act-provision", "act-save"].forEach(id => $(id).disabled = busy);
  $("act-provision").innerHTML = busy ? `<span class="spinner"></span>${msg}` : "🚀 Create + provision";
}
async function pollTask(tid, done) {
  const r = await api("/api/task/" + tid);
  if (r.status === "done") { done && done(r); return; }
  if (r.status === "error") { toast("Failed: " + r.error, "err", 6000); return; }
  setTimeout(() => pollTask(tid, done), 1200);
}

// --- plan load -----------------------------------------------------------
async function loadPlan(name) {
  const plan = await api("/api/plans/" + encodeURIComponent(name));
  if (!plan || plan.error) return;
  state = {
    name: plan.name || "", description: plan.description || "",
    tags: plan.tags || [], onboot: plan.onboot !== false, protect: !!plan.protection,
    size: plan.size || "", cores: plan.cores || "", memory: plan.memory || "",
    gpu: plan.gpu || "", os: plan.os || "",
    disks: (plan.disks || []).filter(d => d.role !== "os")
      .map(d => ({ id: d.id, size: d.size || "", storage: d.storage || "" })),
    bridge: (plan.net || {}).bridge || "", vlan: (plan.net || {}).vlan || "",
    ipmode: plan.ipconfig && plan.ipconfig !== "dhcp" ? "static" : "dhcp",
    ip: plan.ipconfig && plan.ipconfig !== "dhcp" ? plan.ipconfig : "",
    gw: plan.gw || "",
    bootstrap: Object.assign({ system: false, docker: false, tailscale: false }, plan.bootstrap || {}),
    hardware: Object.assign({firmware:"bios", secure_boot:false, tpm:false, efi_storage:"", tpm_storage:"", display:"default", audio:"none"}, plan.hardware || {}),
    ssh_keys: plan.ssh_keys || [],
  };
  writeForm();
  buildSizeChips();
  debouncedValidate();
  toast("Loaded plan " + name, "ok");
}

// ------------------------------------------------------------------------
// VMs list

async function loadVms() {
  $("vms-count").textContent = "";
  const tb = $("vms-table").querySelector("tbody");
  tb.innerHTML = `<tr><td colspan="7" class="loadrow"><span class="spinner"></span>loading…</td></tr>`;
  let vms, host;
  try {
    [vms, host] = await Promise.all([api("/api/vms"), api("/api/host")]);
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="7" class="loadrow">failed to load VMs — refresh to retry</td></tr>`;
    return;
  }
  tb.innerHTML = "";
  const machines = vms.filter(v => !v.template);
  const templates = vms.filter(v => v.template);
  $("vms-count").textContent = `(${machines.length} machines · ${templates.length} templates)`;
  machines.forEach(v => {
    const tr = document.createElement("tr");
    const badge = v.template ? '<span class="badge template">template</span>'
      : `<span class="badge ${v.status}">${v.status}</span>`;
    tr.innerHTML = `<td class="click"><b>${v.name}</b></td><td>${v.vmid}</td>
      <td>${badge}</td><td>${v.ip || "—"}</td><td>${v.cores || "—"}</td>
      <td>${v.memory ? Math.round(v.memory / 1024) + " GB" : "—"}</td>
      <td>${v.tags || "—"}</td>`;
    tr.addEventListener("click", () => openVm(v.name));
    tb.appendChild(tr);
  });
  $("inventory-templates").innerHTML = templates.map(v => `<button data-vm="${esc(v.name)}"><span><b>${esc(v.name)}</b><small>VMID ${v.vmid}</small></span><span class="badge template">template</span></button>`).join("") || `<span class="sub">No templates</span>`;
  $("inventory-templates").querySelectorAll("[data-vm]").forEach(b => b.addEventListener("click", () => openVm(b.dataset.vm)));
  const storage = (host && host.storage) || [];
  $("inventory-storage").innerHTML = storage.map(s => {
    const total = Number(s.total || 0), used = Number(s.used || 0);
    const capacity = total ? `${bytes(used)} / ${bytes(total)}` : "capacity unavailable";
    const name = s.storage || s.name || "";
    return `<button class="inventory-row storage-link" data-storage="${esc(name)}"><span><b>${esc(name || "—")}</b><small>${esc(s.type || "storage")} · ${esc(s.status || "unknown")}</small></span><span>${capacity}</span></button>`;
  }).join("") || `<span class="sub">No storage pools</span>`;
  $("inventory-storage").querySelectorAll("[data-storage]").forEach(b => b.addEventListener("click", () => openStorage(b.dataset.storage)));
}

function storagePercent(s) {
  const total = Number(s.total || 0), used = Number(s.used || 0);
  return total ? Math.min(100, Math.round(100 * used / total)) : 0;
}
function storageName(s) { return s.storage || s.name || "—"; }
async function loadStorage() {
  const body = $("storage-body");
  body.innerHTML = `<div class="card loadrow"><span class="spinner"></span>loading storage pools…</div>`;
  let host;
  try { host = await api("/api/host"); }
  catch (e) { body.innerHTML = `<div class="card loadrow">Storage data unavailable — refresh to retry.</div>`; return; }
  const pools = host.storage || [];
  $("storage-count").textContent = pools.length ? `(${pools.length} pools)` : "";
  const used = pools.reduce((n, s) => n + Number(s.used || 0), 0);
  const total = pools.reduce((n, s) => n + Number(s.total || 0), 0);
  const hot = pools.slice().sort((a,b) => storagePercent(b) - storagePercent(a))[0];
  body.innerHTML = `<div class="metric-grid storage-metrics">${metric("Allocated", bytes(used), total ? `${Math.round(100 * used / total)}% of ${bytes(total)}` : "")}${metric("Available", bytes(Math.max(0, total-used)), "across reported pools")}${metric("Highest use", hot ? storagePercent(hot) + "%" : "—", hot ? storageName(hot) : "")}</div><div class="storage-pool-grid">${pools.map(s => { const pct=storagePercent(s), name=storageName(s), avail=Number(s.avail || Math.max(0, Number(s.total||0)-Number(s.used||0))); return `<button class="storage-pool ${pct >= 90 ? "critical" : pct >= 75 ? "warning" : ""}" data-storage="${esc(name)}"><span class="pool-top"><b>${esc(name)}</b><span class="badge ${esc(s.status || "unknown")}">${esc(s.status || "unknown")}</span></span><span class="pool-type">${esc(s.type || "storage")}</span><span class="pool-number">${pct}%</span><span class="capacity"><span style="width:${pct}%"></span></span><span class="pool-capacity">${Number(s.total||0) ? `${bytes(s.used)} used · ${bytes(avail)} free` : "capacity unavailable"}</span></button>`; }).join("") || `<div class="card"><span class="sub">No storage pools reported by Proxmox.</span></div>`}</div>`;
  body.querySelectorAll("[data-storage]").forEach(b => b.addEventListener("click", () => openStorage(b.dataset.storage)));
}
async function openStorage(name) {
  showTab("storage");
  $("storage-count").textContent = "";
  const body = $("storage-body");
  body.innerHTML = `<div class="card loadrow"><span class="spinner"></span>loading ${esc(name)}…</div>`;
  let d;
  try { d = await api("/api/storage/" + encodeURIComponent(name)); }
  catch (e) { body.innerHTML = `<div class="card loadrow">Could not load ${esc(name)}.</div>`; return; }
  if (d.error) { body.innerHTML = `<div class="card loadrow">${esc(d.error)}</div>`; return; }
  const s=d.storage || {}, rows=d.content || [], pct=storagePercent(s), total=Number(s.total||0), used=Number(s.used||0), avail=Number(s.avail || Math.max(0,total-used));
  const kinds={}; rows.forEach(r => { const k=r.content || r.format || "volume"; kinds[k]=(kinds[k]||0)+1; });
  body.innerHTML = `<div class="page-title storage-detail-title"><div><div class="eyebrow">STORAGE POOL</div><h2>${esc(storageName(s))}</h2></div><button class="btn ghost" id="storage-back">← All storage</button></div><div class="storage-detail-grid"><section class="card pool-readout"><div class="pool-top"><span><b>${esc(s.type || "storage")}</b><small>${esc(s.status || "unknown")}</small></span><strong>${pct}%</strong></div><div class="capacity large"><span style="width:${pct}%"></span></div><div class="pool-totals"><span><b>${bytes(used)}</b> used</span><span><b>${bytes(avail)}</b> free</span><span><b>${total ? bytes(total) : "—"}</b> total</span></div></section><section class="card"><h3>Contents</h3><div class="content-summary">${Object.entries(kinds).map(([k,n]) => `<span><b>${n}</b> ${esc(k)}</span>`).join("") || `<span class="sub">No volumes reported</span>`}</div></section></div><section class="card"><div class="section-heading"><h3>Stored items</h3><span class="sub">${rows.length} reported</span></div>${d.content_error ? `<p class="sub">Contents unavailable: ${esc(d.content_error)}</p>` : rows.length ? `<table class="storage-content"><thead><tr><th>Volume</th><th>Kind</th><th>Size</th><th>VMID</th><th>Format</th></tr></thead><tbody>${rows.map(r => `<tr><td class="mono">${esc(r.volid || r.name || "—")}</td><td>${esc(r.content || "—")}</td><td>${r.size ? bytes(r.size) : "—"}</td><td>${r.vmid || "—"}</td><td>${esc(r.format || "—")}</td></tr>`).join("")}</tbody></table>` : `<p class="sub">No stored items reported by this backend.</p>`}</section>`;
  $("storage-back").addEventListener("click", loadStorage);
}

function bytes(n) {
  n = Number(n || 0); const units = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
function metric(label, value, note) {
  return `<div class="metric"><span>${label}</span><strong>${value}</strong>${note ? `<small>${note}</small>` : ""}</div>`;
}
async function loadDashboard() {
  const [vms, host] = await Promise.all([api("/api/vms"), api("/api/host")]);
  if (!Array.isArray(vms)) return toast(vms.error || "Could not load overview", "err");
  const machines = vms.filter(v => !v.template);
  const active = machines.filter(v => v.status === "running").length;
  const templates = vms.filter(v => v.template).length;
  const s = host.status || {}, m = s.memory || {}, root = s.rootfs || {};
  const load = Array.isArray(s.loadavg) ? s.loadavg[0] : s.loadavg;
  const cpuCount = s.cpuinfo && s.cpuinfo.cpus;
  $("dashboard-metrics").innerHTML = metric("Machines", machines.length, "") + metric("Running", active, "") + metric("CPU", s.cpu != null ? Math.round(s.cpu * 100) + "%" : "—", cpuCount ? `${cpuCount} logical CPUs · load ${Number(load || 0).toFixed(2)}` : "") + metric("Memory", bytes(m.used), m.total ? `${Math.round(100 * m.used / m.total)}% of ${bytes(m.total)}` : "") + metric("Root filesystem", bytes(root.used), root.total ? `${Math.round(100 * root.used / root.total)}% used` : "") + metric("Templates", templates, "");
  $("dashboard-vms").innerHTML = vms.slice(0, 7).map(v => `<button data-vm="${esc(v.name)}"><span><b>${esc(v.name)}</b><small>${v.ip || "no guest IP"}</small></span><span class="badge ${v.template ? "template" : v.status}">${v.template ? "template" : v.status}</span></button>`).join("") || "<span class=\"sub\">No machines yet.</span>";
  $("dashboard-vms").querySelectorAll("[data-vm]").forEach(b => b.addEventListener("click", () => openVm(b.dataset.vm)));
  $("dashboard-host").innerHTML = host.error ? esc(host.error) : `<b>${esc(host.node || "host")}</b><div class="util-line"><span>CPU</span><div class="capacity"><span style="width:${Math.min(100, 100 * Number(s.cpu || 0))}%"></span></div><b>${Math.round(100 * Number(s.cpu || 0))}%</b></div><div class="util-line"><span>Memory</span><div class="capacity"><span style="width:${m.total ? Math.min(100, 100 * (m.used || 0) / m.total) : 0}%"></span></div><b>${m.total ? Math.round(100 * m.used / m.total) : 0}%</b></div><div class="util-line"><span>Root</span><div class="capacity"><span style="width:${root.total ? Math.min(100, 100 * (root.used || 0) / root.total) : 0}%"></span></div><b>${root.total ? Math.round(100 * root.used / root.total) : 0}%</b></div>`;
  $("dashboard-storage").innerHTML = (host.storage || []).map(x => { const total=Number(x.total||0), used=Number(x.used||0), pct=total?Math.min(100,100*used/total):0; return `<div class="storage-bar"><span><b>${esc(x.storage || x.name || "—")}</b><small>${esc(x.type || "storage")} · ${esc(x.status || "unknown")}</small></span><div class="capacity"><span style="width:${pct}%"></span></div><span>${total ? `${bytes(used)} / ${bytes(total)}` : "capacity unavailable"}</span></div>`; }).join("") || "<span class=\"sub\">No storage data.</span>";
}
async function loadHost() {
  const host = await api("/api/host");
  if (host.error) return toast(host.error, "err");
  const s = host.status || {}, m = s.memory || {};
  const root = s.rootfs || {}, load = Array.isArray(s.loadavg) ? s.loadavg[0] : s.loadavg;
  $("host-metrics").innerHTML = metric("Node", host.node || "—", s.cpuinfo && s.cpuinfo.cpus ? s.cpuinfo.cpus + " logical CPUs" : "") + metric("Uptime", s.uptime ? Math.floor(s.uptime / 86400) + " days" : "—", "") + metric("Memory", bytes(m.used), m.total ? `${Math.round(100 * m.used / m.total)}% of ${bytes(m.total)}` : "") + metric("CPU", s.cpu != null ? Math.round(s.cpu * 100) + "%" : "—", load != null ? `load ${Number(load).toFixed(2)}` : "") + metric("Root filesystem", bytes(root.used), root.total ? `${Math.round(100 * root.used / root.total)}% of ${bytes(root.total)}` : "");
  $("host-storage").querySelector("tbody").innerHTML = (host.storage || []).map(x => `<tr><td>${esc(x.storage || x.name || "—")}</td><td>${esc(x.type || "—")}</td><td>${esc(x.status || "—")}</td><td>${bytes(x.used)}</td><td>${bytes(x.avail)}</td></tr>`).join("") || `<tr><td colspan="5" class="loadrow">No storage data.</td></tr>`;
}
async function loadSettings() {
  const s = await api("/api/settings");
  if (s.error) return toast(s.error, "err");
  $("s-default-user").value = s.default_user || "";
  $("s-default-bridge").value = s.default_bridge || "";
  $("s-default-storage").value = s.default_storage || "";
  $("s-backup-storage").value = s.backup_storage || "";
  $("s-vm-agent").checked = s.vm_agent !== false;
  const pam = auth.mode === "pam";
  $("auth-summary").textContent = pam ? `Signed in as ${auth.user}. Phase access is granted to the configured PAM users.` : "Phase uses an access token; it does not have a separate web password.";
  $("reset-token").classList.toggle("hide", pam);
  $("forget-token").classList.toggle("hide", pam);
  $("logout").classList.toggle("hide", !pam);
}
async function saveSettings() {
  const settings = {default_user: $("s-default-user").value.trim(), default_bridge: $("s-default-bridge").value.trim(), default_storage: $("s-default-storage").value.trim(), backup_storage: $("s-backup-storage").value.trim(), vm_agent: $("s-vm-agent").checked};
  const r = await api("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({settings})});
  if (r.error) return toast(r.error, "err");
  toast("Settings saved to phase.json", "ok"); init();
}
async function rotateToken() {
  if (!confirm("Rotate the Phase access token? Other browser sessions will need the new token.")) return;
  const r = await api("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({settings:{rotate_token:true}})});
  if (!r.ok || !r.new_token) return toast(r.error || "Token rotation failed", "err");
  token = r.new_token;
  localStorage.setItem(TOKEN_KEY, token);
  toast("Access token rotated for this browser", "ok", 6000);
}

// ------------------------------------------------------------------------
// VM detail

async function openVm(name) {
  showTab("detail");
  $("detail-name").textContent = name;
  const vm = await api("/api/vms/" + encodeURIComponent(name));
  if (vm.error) { toast(vm.error, "err"); showTab("vms"); return; }
  currentVm = vm;
  renderDetail(vm);
}
function renderDetail(vm) {
  $("detail-status").className = "badge " + (vm.template ? "template" : vm.status);
  $("detail-status").textContent = vm.template ? "template" : vm.status;
  $("detail-ip").textContent = vm.ip ? "· " + vm.ip : "";

  const disks = vm.disks.map(d => `${d.id} (${d.size || d.volume || "?"}${d.role === "system" ? " · system image" : ""})`).join("<br>") || "—";
  const body = $("detail-body");
  body.innerHTML = `
  <div class="grid2">
    <div class="card">
      <h3>Power</h3>
      <div class="power-actions">
        <button class="btn" data-pw="start" ${vm.status === "running" ? "disabled" : ""}>▶ Start</button>
        <button class="btn ghost" data-pw="reboot" ${vm.status !== "running" ? "disabled" : ""}>↻ Reboot</button>
        <button class="btn ghost" data-pw="shutdown" ${vm.status !== "running" ? "disabled" : ""}>⏻ Shutdown</button>
        <button class="btn ghost" data-pw="stop" ${vm.status !== "running" ? "disabled" : ""}>⏼ Stop</button>
        <button class="btn ghost" data-pw="pause" ${vm.status !== "running" ? "disabled" : ""}>⏸ Pause</button>
      </div>
    </div>
    <div class="card">
      <h3>Info</h3>
      <div class="kv">
        <span class="k">VMID</span><span class="v">${vm.vmid}</span>
        <span class="k">vCPU</span><span class="v">${vm.cores || "—"}</span>
        <span class="k">Memory</span><span class="v">${vm.memory ? Math.round(vm.memory / 1024) + " GB" : "—"}</span>
        <span class="k">Network</span><span class="v">${vm.net0 || "—"}</span>
        <span class="k">Disks</span><span class="v">${disks}</span>
        <span class="k">On boot</span><span class="v">${vm.onboot === "1" ? "yes" : "no"}</span>
        <span class="k">Protected</span><span class="v">${vm.protection === "1" ? "yes" : "no"}</span>
        <span class="k">Description</span><span class="v">${vm.description || "—"}</span>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Edit</h3>
    <div class="row">
      <div class="field"><label>vCPUs</label><input type="number" id="e-cores" value="${vm.cores}" min="1"></div>
      <div class="field"><label>Memory (MB)</label><input type="number" id="e-memory" value="${vm.memory}" min="128"></div>
      <div class="field" style="flex:1"><label>Description</label><input type="text" id="e-desc" value="${esc(vm.description)}" spellcheck="false"></div>
      <div class="field"><label>Tags (comma)</label><input type="text" id="e-tags" value="${esc(vm.tags)}" spellcheck="false"></div>
    </div>
    <label class="toggle"><input type="checkbox" id="e-onboot" ${vm.onboot === "1" ? "checked" : ""}> start on boot</label>
    <label class="toggle"><input type="checkbox" id="e-protect" ${vm.protection === "1" ? "checked" : ""}> protected</label>
    <div class="actions"><button class="btn" id="e-save">Save</button></div>
  </div>

  <div class="card">
    <h3>Disks</h3>
    <table id="d-table" class="disk-table"><thead><tr><th>Device</th><th>Capacity</th><th>Storage</th><th>Volume</th><th></th></tr></thead>
      <tbody>
        ${vm.disks.map(d => `
        <tr>
          <td><b>${d.id}</b><small>${d.role === "system" ? `System image${d.image ? " · " + esc(d.image) : ""}` : (d.bus || d.id.replace(/\d+$/, ""))}</small></td>
          <td>${d.size || "—"}</td><td>${d.storage || "—"}</td><td class="disk-volume">${d.volume || "—"}</td>
          <td class="disk-actions"><details><summary>Manage</summary><div class="disk-menu"><label>New size<input type="text" class="resize-in" data-id="${d.id}" placeholder="e.g. +20G"></label><button class="btn ghost mini" data-resize="${d.id}">Resize</button></div></details></td>
        </tr>`).join("")}
      </tbody>
    </table>
    <div class="disk-addrow detail-disk-add" style="margin-top:14px">
      <div class="field"><label>Bus</label><select id="d-new-bus">${diskBusOptions()}</select></div>
      <div class="field"><label>Capacity</label><input type="text" id="d-new-size" placeholder="50G" spellcheck="false"></div>
      <div class="field"><label>Storage</label><select id="d-new-storage">${storageOptions()}</select></div>
      <button class="btn ghost" id="d-add">＋ Attach disk</button>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h3>Firewall</h3>
      <div class="fw-row">
        <input type="text" id="fw-from" placeholder="from (alias|cidr)" spellcheck="false">
        <input type="text" id="fw-port" placeholder="port" spellcheck="false" style="width:80px">
        <select id="fw-proto"><option>tcp</option><option>udp</option></select>
        <button class="btn ghost mini" id="fw-add">Allow</button>
      </div>
      <pre id="fw-list" style="font-size:12px;margin-top:10px;max-height:180px;overflow:auto">—</pre>
    </div>
    <div class="card">
      <h3>Snapshots</h3>
      <div class="snap-row">
        <input type="text" id="snap-name" placeholder="snap name" spellcheck="false">
        <button class="btn ghost mini" id="snap-add">Take snapshot</button>
      </div>
      <div id="snap-list" style="margin-top:10px;font-size:13px">—</div>
    </div>
  </div>

  <div class="card">
    <h3>Backup</h3>
    <button class="btn ghost" id="b-backup">Run backup (snapshot mode)</button>
  </div>

  <div class="card danger-zone">
    <h3>Danger zone</h3>
    <p style="font-size:13px;color:var(--muted);margin-bottom:10px">Permanently destroy ${vm.name} (VMID ${vm.vmid}). This cannot be undone.</p>
    <button class="btn red" id="b-destroy">🗑 Destroy VM</button>
  </div>`;

  // wire events
  body.querySelectorAll("[data-pw]").forEach(b =>
    b.addEventListener("click", () => vmPower(vm.name, b.dataset.pw)));
  $("e-save").addEventListener("click", () => vmEdit(vm.name));
  $("d-add").addEventListener("click", () => vmDiskAdd(vm.name));
  body.querySelectorAll("[data-resize]").forEach(b =>
    b.addEventListener("click", () => {
      const inp = body.querySelector(`.resize-in[data-id="${b.dataset.resize}"]`);
      vmDiskResize(vm.name, b.dataset.resize, inp.value);
    }));
  $("fw-add").addEventListener("click", () => vmFirewall(vm.name));
  $("snap-add").addEventListener("click", () => vmSnapshot(vm.name));
  $("b-backup").addEventListener("click", () => vmBackup(vm.name));
  $("b-destroy").addEventListener("click", () => vmDestroy(vm.name));
  $("btn-ssh").onclick = () => openTerminal(vm.name);
  $("btn-serial").onclick = () => openTerminalPath("serial", vm.name, "SERIAL");
  $("btn-ssh").disabled = vm.status !== "running";
  $("btn-ssh").title = vm.status === "running" ? "Open SSH terminal" : "VM must be running";
  loadFirewall(vm.name);
  loadSnapshots(vm.name);
}
function esc(s) { return String(s == null ? "" : s).replace(/"/g, "&quot;").replace(/</g, "&lt;"); }
function diskBusOptions() {
  return [["SCSI", "scsi"], ["SATA", "sata"], ["VirtIO", "virtio"], ["IDE", "ide"]]
    .map(([label, value]) => `<option value="${value}">${label}</option>`).join("");
}
function storageOptions() {
  const storages = (meta && meta.storages) || [];
  return `<option value="">Select storage</option>` + storages.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
}
function nextDiskId(vm, bus) {
  const used = new Set(vm.disks.map(d => d.id)); let n = 0;
  while (used.has(bus + n)) n++;
  return bus + n;
}

async function vmPower(name, action) {
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/power", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (r.error) return toast(r.error, "err");
  toast(action + "…", "");
  pollTask(r.task, () => { toast(name + " " + action + "ed", "ok"); openVm(name); });
}
async function vmEdit(name) {
  const body = {
    cores: $("e-cores").value, memory: $("e-memory").value,
    description: $("e-desc").value,
    tags: $("e-tags").value.split(",").map(t => t.trim()).filter(Boolean),
    onboot: $("e-onboot").checked, protection: $("e-protect").checked,
  };
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/edit", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.error) return toast(r.error, "err");
  toast("saving…", "");
  pollTask(r.task, () => { toast("Saved", "ok"); openVm(name); });
}
async function vmDiskAdd(name) {
  const bus = $("d-new-bus").value;
  const body = { id: nextDiskId(currentVm, bus), size: $("d-new-size").value.trim(), storage: $("d-new-storage").value.trim() };
  if (!body.size || !body.storage) return toast("capacity and storage required", "err");
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/disks/add", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.error) return toast(r.error, "err");
  pollTask(r.task, () => { toast("disk attached", "ok"); openVm(name); });
}
async function vmDiskResize(name, id, size) {
  if (!size) return toast("size required", "err");
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/disks/resize", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, size }),
  });
  if (r.error) return toast(r.error, "err");
  pollTask(r.task, () => { toast("disk resized", "ok"); openVm(name); });
}
async function vmFirewall(name) {
  const body = { from: $("fw-from").value.trim(), port: $("fw-port").value.trim(), proto: $("fw-proto").value };
  if (!body.from || !body.port) return toast("from and port required", "err");
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/firewall", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.error) return toast(r.error, "err");
  pollTask(r.task, () => { toast("rule added", "ok"); $("fw-from").value = ""; $("fw-port").value = ""; loadFirewall(name); });
}
async function loadFirewall(name) {
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/firewall");
  $("fw-list").textContent = r.rules || r.error || "—";
}
async function vmSnapshot(name) {
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/snapshot", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: $("snap-name").value.trim() }),
  });
  if (r.error) return toast(r.error, "err");
  pollTask(r.task, () => { toast("snapshot taken", "ok"); $("snap-name").value = ""; loadSnapshots(name); });
}
async function loadSnapshots(name) {
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/snapshots");
  const box = $("snap-list");
  if (r.error) { box.textContent = r.error; return; }
  box.innerHTML = (r && r.length)
    ? r.map(s => `• ${s.name}`).join("<br>") : "none";
}
async function vmBackup(name) {
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/backup", {
    method: "POST",
  });
  if (r.error) return toast(r.error, "err");
  toast("backup running…", "");
  pollTask(r.task, () => toast("backup done", "ok"));
}
async function vmDestroy(name) {
  const typed = prompt(`Type ${name} to permanently destroy this VM:`);
  if (typed !== name) { toast("cancelled", "err"); return; }
  const r = await api("/api/vms/" + encodeURIComponent(name) + "/destroy", {
    method: "POST",
  });
  if (r.error) return toast(r.error, "err");
  toast("destroying…", "");
  pollTask(r.task, () => { toast(name + " destroyed", "ok"); showTab("vms"); });
}

// ------------------------------------------------------------------------
// terminal

function openTerminal(name) {
  openTerminalPath("ssh", name, "SSH");
}
function openTerminalPath(kind, name, label) {
  window.open(`/terminal.html?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`, `phase-${kind}-${name}`, "popup,width=1040,height=720,resizable=yes,scrollbars=no");
}

// ------------------------------------------------------------------------
// events

document.querySelectorAll("input[name=ipmode]").forEach(r =>
  r.addEventListener("change", () => { updateStaticVisibility(); debouncedValidate(); }));
$("disk-add").addEventListener("click", addDisk);
document.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => showTab(b.dataset.view)));
$("btn-back").addEventListener("click", () => showTab("vms"));
$("btn-host-console").addEventListener("click", () => openTerminalPath("host", "host", "HOST CONSOLE"));
$("save-settings").addEventListener("click", saveSettings);
$("btn-refresh").addEventListener("click", () => {
  if ($("view-vms").classList.contains("hide") === false) loadVms();
  else if ($("view-storage").classList.contains("hide") === false) loadStorage();
  else if ($("view-dashboard").classList.contains("hide") === false) loadDashboard();
  else if (currentVm) openVm(currentVm.name);
});
$("btn-storage-refresh").addEventListener("click", loadStorage);
$("loadplan").addEventListener("change", e => e.target.value && loadPlan(e.target.value));
$("act-create").addEventListener("click", () => runCreateAction("create"));
$("act-provision").addEventListener("click", () => runCreateAction("provision"));
$("act-save").addEventListener("click", () => runCreateAction("save"));
$("reset-token").addEventListener("click", rotateToken);
$("forget-token").addEventListener("click", () => { localStorage.removeItem(TOKEN_KEY); token = ""; toast("Token forgotten on this browser", "ok"); });
$("login-form").addEventListener("submit", login);
$("logout").addEventListener("click", async () => { await fetch("/api/auth/logout", {method:"POST"}); auth.authenticated=false; boot(); });
["f-name","f-desc","f-cores","f-memory","f-vlan","f-ip","f-gw","f-tags",
 "f-gpu","f-os","f-bridge","f-sshkey"]
  .forEach(id => $(id) && $(id).addEventListener("input", debouncedValidate));
["f-onboot","f-protect","f-bs-system","f-bs-docker","f-bs-tailscale"]
  .forEach(id => $(id) && $(id).addEventListener("change", debouncedValidate));
["f-firmware","f-secureboot","f-tpm","f-efi-storage","f-tpm-storage","f-display","f-audio"]
  .forEach(id => $(id) && $(id).addEventListener("change", debouncedValidate));

SECTIONS.forEach(s => {
  const b = document.querySelector(`.stepper [data-sec="${s}"]`);
  if (!b) return;
  b.addEventListener("click", () => {
    document.querySelectorAll(".stepper [data-sec]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    showSection(s);
  });
});

boot();
