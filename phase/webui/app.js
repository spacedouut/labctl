"use strict";
const $ = id => document.getElementById(id);
const SECTIONS = ["machine", "disks", "network", "others"];
const TOKEN_KEY = "phase_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let meta = null;
let currentVm = null;

// ---- create-wizard state (unified disks: one list, one marked boot) ----
let state = {
  name: "", description: "", tags: [], onboot: true, protect: false,
  size: "", cores: "", memory: "", gpu: "", os: "",
  disks: [{ id: "scsi0", size: "", storage: "", boot: true }],
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
  if (r.status === 401) {
    token = prompt("Access token for this phase instance:") || "";
    if (token) localStorage.setItem(TOKEN_KEY, token);
    opts.headers["X-Phase-Token"] = token;
    r = await fetch(path, opts);
  }
  return r.json();
}

// ------------------------------------------------------------------------
// toast / tabs

function toast(msg, cls, ms) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (cls ? " " + cls : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = "", ms || 3500);
}
function showTab(tab) {
  ["create", "vms", "detail"].forEach(t => $("view-" + t).classList.toggle("hide", t !== tab));
  ["create", "vms"].forEach(t => $("tab-" + t).classList.toggle("active", t === tab));
  if (tab === "vms") loadVms();
}

// ------------------------------------------------------------------------
// create wizard

function init() {
  meta = null;
  api("/api/meta").then(m => {
    meta = m;
    $("hostline").textContent = m.host || "";
    buildSizeChips();
    fillSelect($("f-os"), m.oses);
    fillSelect($("f-bridge"), ["", ...(m.networks || [])].map(v => [v || "(template default)", v]));
    fillSelect($("f-sshkey"), ["", ...(m.ssh_keys || [])].map(v => [v || "(none)", v]));
    fillSelect($("loadplan"), ["", ...(m.plans || []).map(p => p.name)].map(v => [v || "— new VM —", v]));
    if (m.sizes && m.sizes.small) state.size = "small";
    else if (m.sizes) state.size = Object.keys(m.sizes)[0];
    if (m.oses && m.oses.length) state.os = m.oses[0];
    buildStepper();
    renderAll();
    debouncedValidate();
  });
}
function fillSelect(sel, pairs) {
  sel.innerHTML = "";
  for (const [label, value] of pairs) {
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
    ["boot disk: bad size", "f-osdisk-size"],
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
  const boot = st.disks.find(d => d.boot) || st.disks[0];
  if (boot.size) a.push("--disk", `scsi0:os:${st.os}:${boot.size}`
    + (boot.storage ? `:${boot.storage}` : ""));
  st.disks.filter(d => d !== boot).forEach(d =>
    a.push("--disk", `${d.id}:data:${d.size}` + (d.storage ? `:${d.storage}` : "")));
  if (st.bridge) a.push("--bridge", st.bridge);
  if (st.vlan) a.push("--vlan", st.vlan);
  if (st.ipmode === "static" && st.ip) { a.push("--ip", st.ip); if (st.gw) a.push("--gw", st.gw); }
  if (st.tags.length) st.tags.forEach(t => a.push("--tag", t));
  ["system", "docker", "tailscale"].forEach(k => { if (st.bootstrap[k]) a.push("--" + k); });
  if (st.gpu) a.push("--gpu", st.gpu);
  return a.join(" ");
}

// --- unified disks ------------------------------------------------------
function renderDisks() {
  const tb = $("disk-table").querySelector("tbody");
  tb.innerHTML = "";
  state.disks.forEach((d, i) => {
    const tr = document.createElement("tr");
    const boot = d.boot ? "checked" : "";
    tr.innerHTML = `<td><input type="radio" name="bootdisk" ${boot}></td>
      <td>${d.id}</td><td>${d.size || "—"}</td><td>${d.storage || "—"}</td>
      <td class="del" title="remove">✕</td>`;
    tr.querySelector("input").addEventListener("change", () => {
      state.disks.forEach(x => x.boot = false);
      state.disks[i].boot = true;
      renderDisks();
      debouncedValidate();
    });
    tr.querySelector(".del").addEventListener("click", () => {
      if (state.disks.length === 1) { toast("need at least one disk", "err"); return; }
      state.disks.splice(i, 1);
      if (!state.disks.some(x => x.boot)) state.disks[0].boot = true;
      renderDisks();
      debouncedValidate();
    });
    tb.appendChild(tr);
  });
}
function addDisk() {
  const id = $("disk-new-id").value.trim();
  const size = $("disk-new-size").value.trim();
  const storage = $("disk-new-storage").value.trim();
  if (!id || !size) { toast("id and size required", "err"); return; }
  state.disks.push({ id, size, storage, boot: false });
  $("disk-new-id").value = $("disk-new-size").value = $("disk-new-storage").value = "";
  renderDisks();
  debouncedValidate();
}
function toBackend(st) {
  // unified disks -> wizard state schema (os_disk_* + data_disks)
  const boot = st.disks.find(d => d.boot) || st.disks[0];
  return {
    name: st.name, description: st.description, tags: st.tags,
    onboot: st.onboot, protect: st.protect, size: st.size,
    cores: st.cores, memory: st.memory, gpu: st.gpu, os: st.os,
    os_disk_size: boot.size, os_disk_storage: boot.storage,
    data_disks: st.disks.filter(d => d !== boot)
      .map(d => ({ id: d.id, size: d.size, storage: d.storage })),
    bridge: st.bridge, vlan: st.vlan, ipmode: st.ipmode, ip: st.ip, gw: st.gw,
    bootstrap: st.bootstrap, ssh_keys: st.ssh_keys,
  };
}

// --- form read/write ----------------------------------------------------
function readForm() {
  state.name = $("f-name").value.trim();
  state.description = $("f-desc").value.trim();
  state.cores = $("f-cores").value.trim();
  state.memory = $("f-memory").value.trim();
  state.os = $("f-os").value;
  state.bridge = $("f-bridge").value;
  state.vlan = $("f-vlan").value.trim();
  state.ipmode = document.querySelector("input[name=ipmode]:checked").value;
  state.ip = $("f-ip").value.trim();
  state.gw = $("f-gw").value.trim();
  state.tags = $("f-tags").value.split(",").map(t => t.trim()).filter(Boolean);
  state.onboot = $("f-onboot").checked;
  state.protect = $("f-protect").checked;
  state.gpu = $("f-gpu").value.trim();
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
  const osDisk = (plan.disks || []).find(d => d.role === "os") || {};
  state = {
    name: plan.name || "", description: plan.description || "",
    tags: plan.tags || [], onboot: plan.onboot !== false, protect: !!plan.protection,
    size: plan.size || "", cores: plan.cores || "", memory: plan.memory || "",
    gpu: plan.gpu || "", os: plan.os || "",
    disks: [
      { id: osDisk.id || "scsi0", size: osDisk.size || "", storage: osDisk.storage || "", boot: true },
      ...(plan.disks || []).filter(d => d.role !== "os")
        .map(d => ({ id: d.id, size: d.size || "", storage: d.storage || "", boot: false })),
    ],
    bridge: (plan.net || {}).bridge || "", vlan: (plan.net || {}).vlan || "",
    ipmode: plan.ipconfig && plan.ipconfig !== "dhcp" ? "static" : "dhcp",
    ip: plan.ipconfig && plan.ipconfig !== "dhcp" ? plan.ipconfig : "",
    gw: plan.gw || "",
    bootstrap: Object.assign({ system: false, docker: false, tailscale: false }, plan.bootstrap || {}),
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
  const vms = await api("/api/vms");
  const tb = $("vms-table").querySelector("tbody");
  tb.innerHTML = "";
  $("vms-count").textContent = `(${vms.length})`;
  vms.forEach(v => {
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

  const disks = vm.disks.map(d => `${d.id} (${d.size || "?"}${d.os ? " · boot" : ""}${d.storage ? " · " + d.storage : ""})`).join("<br>") || "—";
  const body = $("detail-body");
  body.innerHTML = `
  <div class="grid2">
    <div class="card">
      <h3>Power</h3>
      <div class="actions">
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
    <table id="d-table"><thead><tr><th>ID</th><th>Size</th><th>Storage</th><th>Resize</th><th></th></tr></thead>
      <tbody>
        ${vm.disks.map(d => `
        <tr>
          <td>${d.id}${d.os ? ' <span class="badge template">boot</span>' : ""}</td>
          <td>${d.size || "—"}</td><td>${d.storage || "—"}</td>
          <td><input type="text" class="resize-in" data-id="${d.id}" placeholder="e.g. 100G" style="width:90px;padding:5px 8px;border:1px solid var(--border);border-radius:6px"></td>
          <td><button class="btn ghost mini" data-resize="${d.id}">Apply</button></td>
        </tr>`).join("")}
      </tbody>
    </table>
    <div class="row disk-addrow" style="margin-top:10px">
      <input type="text" id="d-new-id" placeholder="id (scsi1)" spellcheck="false">
      <input type="text" id="d-new-size" placeholder="size (50G)" spellcheck="false">
      <input type="text" id="d-new-storage" placeholder="storage (nas)" spellcheck="false">
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
  $("btn-ssh").disabled = vm.status !== "running";
  $("btn-ssh").title = vm.status === "running" ? "Open SSH terminal" : "VM must be running";
  loadFirewall(vm.name);
  loadSnapshots(vm.name);
}
function esc(s) { return String(s == null ? "" : s).replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

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
  const body = { id: $("d-new-id").value.trim(), size: $("d-new-size").value.trim(), storage: $("d-new-storage").value.trim() };
  if (!body.id || !body.size || !body.storage) return toast("id, size, storage required", "err");
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

let term = null, termFit = null, termWs = null;
function openTerminal(name) {
  $("term-modal").classList.remove("hide");
  $("term-title").textContent = name + " · connecting…";
  if (term) { term.dispose(); term = null; }
  const el = $("term");
  el.innerHTML = "";
  term = new Terminal({
    cursorBlink: true, fontFamily: 'ui-monospace, "Cascadia Mono", Menlo, monospace',
    fontSize: 13, theme: { background: "#0d1117", foreground: "#c9d1d9" },
    scrollback: 4000,
  });
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open(el);
  termFit.fit();
  term.focus();
  term.onData(d => { if (termWs && termWs.readyState === 1) termWs.send(d); });
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  termWs = new WebSocket(proto + "//" + location.host + "/api/ssh/" + encodeURIComponent(name) + (token ? "?token=" + encodeURIComponent(token) : ""));
  termWs.onopen = () => {
    $("term-title").textContent = name + " · connected (exit with logout/exit)";
    term.focus();
  };
  termWs.onmessage = e => term.write(e.data);
  termWs.onclose = () => {
    term.write("\r\n\x1b[33m[connection closed]\x1b[0m\r\n");
    $("term-title").textContent = name + " · disconnected";
  };
  termWs.onerror = () => { term.write("\r\n[connection error]\r\n"); };
  setTimeout(() => termFit.fit(), 200);
}
function closeTerminal() {
  $("term-modal").classList.add("hide");
  if (termWs) { try { termWs.close(); } catch (e) {} termWs = null; }
  if (term) { term.dispose(); term = null; }
}

// ------------------------------------------------------------------------
// events

document.querySelectorAll("input[name=ipmode]").forEach(r =>
  r.addEventListener("change", () => { updateStaticVisibility(); debouncedValidate(); }));
$("disk-add").addEventListener("click", addDisk);
$("tab-create").addEventListener("click", () => showTab("create"));
$("tab-vms").addEventListener("click", () => showTab("vms"));
$("btn-back").addEventListener("click", () => showTab("vms"));
$("btn-refresh").addEventListener("click", () => {
  if ($("view-vms").classList.contains("hide") === false) loadVms();
  else if (currentVm) openVm(currentVm.name);
});
$("loadplan").addEventListener("change", e => e.target.value && loadPlan(e.target.value));
$("act-create").addEventListener("click", () => runCreateAction("create"));
$("act-provision").addEventListener("click", () => runCreateAction("provision"));
$("act-save").addEventListener("click", () => runCreateAction("save"));
$("term-close").addEventListener("click", closeTerminal);
["f-name","f-desc","f-cores","f-memory","f-vlan","f-ip","f-gw","f-tags",
 "f-gpu","f-os","f-bridge","f-sshkey","f-osdisk-size","f-osdisk-storage"]
  .forEach(id => $(id) && $(id).addEventListener("input", debouncedValidate));
["f-onboot","f-protect","f-bs-system","f-bs-docker","f-bs-tailscale"]
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

init();
