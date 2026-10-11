/* global qlExtractProfile */
// QL side panel: reads the LinkedIn profile you're viewing, saves it to your local QL,
// and finds a checked work email. Talks only to your own backend.

const $ = (id) => document.getElementById(id);
const state = { backend: "http://127.0.0.1:8765", autoRead: true, tab: null, data: null,
                known: null, contactId: null, dirty: false, last: { url: "", name: "" }, busy: false };

const STATUS_TEXT = {
  valid: ["valid", "Confirmed by their mail server"],
  accept_all: ["accept-all", "Their server accepts any address, so this is the best guess"],
  unknown: ["unknown", "No answer from their server; best guess from the naming pattern"],
  invalid: ["invalid", "Their server says this mailbox doesn't exist"],
  unchecked: ["unchecked", "Guessed from the naming pattern, not checked"],
};

function show(id, on) { $(id).classList.toggle("hidden", !on); }
function msg(text, cls = "") { $("msg").textContent = text || ""; $("msg").className = "msg " + cls; }
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isProfile = (url) => /^https:\/\/www\.linkedin\.com\/in\/[^/]+/.test(url || "");
const profileKey = (url) => (url || "").split(/[?#]/)[0].replace(/\/+$/, "").toLowerCase();

function initials(name) {
  const p = (name || "?").replace(/\./g, " ").split(/\s+/).filter((w) => /^[\p{L}\p{N}]/u.test(w));
  if (!p.length) return "?";
  return (p[0][0] + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase();
}
const hue = (name) => "h" + ([...(name || "")].reduce((s, c) => s + c.codePointAt(0), 0) % 6);

async function api(path, opts = {}) {
  const res = await fetch(state.backend + path, {
    ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error((data && data.detail) || `QL error ${res.status}`);
  return data;
}

async function checkBackend() {
  try {
    const h = await api("/api/health");
    $("conn").textContent = "Connected to QL";
    $("conn").className = "conn ok";
    $("capText").textContent = `${h.captures_24h} of ${h.capture_cap}`;
    $("capBar").style.width = `${Math.min(100, (100 * h.captures_24h) / h.capture_cap)}%`;
    return h;
  } catch {
    $("conn").textContent = "QL not running";
    $("conn").className = "conn err";
    return null;
  }
}

// ---------- reading the page ----------

async function readPage() {
  const key = profileKey(state.tab.url);
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: state.tab.id }, func: qlExtractProfile,
      });
      if (result && !result.error && result.full_name) {
        // After LinkedIn's in-page navigation the previous person can linger for a moment.
        const stale = key !== state.last.url && result.full_name === state.last.name;
        if (!stale && (result.headline || attempt >= 2)) {
          state.last = { url: key, name: result.full_name };
          return result;
        }
      }
    } catch (err) {
      if (attempt === 5) throw err;
    }
    await sleep(700);
  }
  return null;
}

function renderProfile(d) {
  $("avatar").textContent = initials(d.full_name);
  $("avatar").className = "avatar " + hue(d.full_name);
  $("pName").textContent = d.full_name;
  $("pHeadline").textContent = d.headline || "";
  $("pMeta").textContent = [d.company_name, d.location].filter(Boolean).join(" · ");
  const roles = $("roles");
  roles.replaceChildren();
  for (const r of (d.experience || []).slice(0, 3)) {
    const li = el("li");
    li.append(el("span", [r.title, r.company].filter(Boolean).join(" · ")), el("span", r.dates || ""));
    roles.append(li);
  }
  const form = $("form");
  for (const k of ["full_name", "title", "company_name", "location", "headline"]) form.elements[k].value = d[k] || "";
  state.dirty = false;
}

function renderPlaceholder() {
  $("avatar").textContent = "?";
  $("pName").textContent = "Ready to read this profile";
  $("pHeadline").textContent = "Click the arrow to read it.";
  $("pMeta").textContent = "";
  $("roles").replaceChildren();
}

// ---------- main flow ----------

async function refresh() {
  [state.tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  Object.assign(state, { data: null, known: null, contactId: null });
  $("form").elements.email.value = "";
  ["domainCard", "resultCard", "known"].forEach((id) => show(id, false));
  msg("");
  const health = await checkBackend();
  show("offline", !health);
  if (!health) { show("where", false); show("profile", false); return; }
  const onProfile = state.tab && isProfile(state.tab.url);
  show("where", !onProfile);
  show("profile", onProfile);
  if (!onProfile) return;

  lookupKnown();
  if (state.autoRead) await doRead();
  else renderPlaceholder();
  updateGoButton();
}

async function doRead() {
  $("pName").textContent = "Reading profile…";
  $("pHeadline").textContent = "";
  try {
    const d = await readPage();
    if (!d) { msg("Couldn't read this page yet. Scroll a little, then click the arrow.", "warn"); return; }
    state.data = d;
    renderProfile(d);
  } catch (err) {
    msg("Couldn't read the page: " + err.message, "err");
  }
}

async function lookupKnown() {
  try {
    const k = await api("/api/lookup?url=" + encodeURIComponent(state.tab.url));
    if (!k.found) return;
    state.known = k;
    state.contactId = k.contact_id;
    const stage = { new: "New", ready: "Email found", contacted: "Contacted", replied: "Replied", closed: "Closed" }[k.stage] || k.stage;
    $("knownText").textContent = `Already in QL · ${stage}`;
    show("known", true);
    setLinks(k.page);
    if (k.email) showEmail({ email: k.email, status: k.email_status, candidates: [], messages: [] });
    if (k.email && !$("form").elements.email.value) $("form").elements.email.value = k.email;
    updateGoButton();
  } catch { /* lookup is best effort */ }
}

function updateGoButton() {
  const go = $("go"), k = state.known, d = state.data;
  const differs = k && d && ((d.title && k.title !== undefined && d.title !== k.title) ||
                             (d.company_name && k.company && d.company_name !== k.company));
  const typed = $("form").elements.email.value.trim();
  if (typed && (!k || typed.toLowerCase() !== (k.email || ""))) go.textContent = "Save with this email";
  else if (!k) go.textContent = "Save & find email";
  else if (differs || state.dirty) go.textContent = "Update & find email";
  else go.textContent = k.email ? "Check email again" : "Find email";
}

function openQL(page) {
  chrome.tabs.create({ url: state.backend + (page || "/") });
}

function collect() {
  const form = $("form");
  const body = { ...(state.data || {}) };
  for (const k of ["full_name", "title", "company_name", "location", "headline", "notes"]) {
    const v = form.elements[k].value.trim();
    if (v) body[k] = v; else delete body[k];
  }
  delete body.email;
  if (!body.linkedin_url) body.linkedin_url = state.tab.url.split(/[?#]/)[0];
  delete body.error;
  return body;
}

function setLinks(page) {
  $("openQL").href = state.backend + page;
  $("draftQL").href = state.backend + page;
}

async function go() {
  if (state.busy) return;
  state.busy = true;
  $("go").disabled = true;
  msg("");
  try {
    if (!state.data && !state.known) await doRead();
    // Always send what's on the page: new people are saved, people already in QL get updated
    // (updates don't count towards the daily limit).
    const body = collect();
    if (!body.full_name) throw new Error("No name found. Open 'Edit details' and type it.");
    const r = await api("/api/capture", { method: "POST", body: JSON.stringify(body) });
    const email = state.known && state.known.email;
    state.contactId = r.contact_id;
    state.known = { contact_id: r.contact_id, domain: r.domain, page: r.page, company: r.company,
                    title: r.title, email };
    setLinks(r.page);
    $("knownText").textContent = r.created ? "Saved to QL" : "Updated in QL";
    show("known", true);
    state.dirty = false;
    checkBackend();
    const typed = $("form").elements.email.value.trim().toLowerCase();
    if (typed && typed !== (email || "")) { await saveTypedEmail(typed); return; }
    if (!r.company) throw new Error("No company found. Add it under 'Edit details'.");
    if (r.domain) await find();
    else await askDomain();
  } catch (err) {
    msg(err.message, "err");
  } finally {
    state.busy = false;
    $("go").disabled = false;
    updateGoButton();
  }
}

async function askDomain() {
  show("domainCard", true);
  $("dCompany").textContent = (state.known && state.known.company) || (state.data && state.data.company_name) || "this company";
  const chips = $("chips");
  chips.replaceChildren(el("span", "Looking for likely domains…", "hint"));
  try {
    const r = await api(`/api/contacts/${state.contactId}/domain-suggestions`);
    chips.replaceChildren();
    for (const s of r.suggestions) {
      const b = el("button", s.domain, "chip-btn");
      b.title = s.mx ? "Has a mail server" : "Couldn't check";
      b.addEventListener("click", () => setDomain(s.domain));
      chips.append(b);
    }
    if (!r.suggestions.length) chips.append(el("span", "No guess has a mail server. Type it from their website.", "hint"));
  } catch (err) { chips.replaceChildren(el("span", err.message, "hint")); }
}

async function setDomain(domain) {
  try {
    const r = await api(`/api/contacts/${state.contactId}/domain`, { method: "POST", body: JSON.stringify({ domain }) });
    show("domainCard", false);
    if (state.known) state.known.domain = r.domain;
    if (r.mx_ok === false) msg(`${r.domain} has no mail server; double-check it.`, "warn");
    await find();
  } catch (err) { msg(err.message, "err"); }
}

async function find() {
  show("resultCard", true);
  show("emailBox", false);
  show("working", true);
  $("note").textContent = "";
  try {
    const r = await api(`/api/contacts/${state.contactId}/find`, { method: "POST", body: "{}" });
    showEmail(r);
    if (state.known) state.known.email = r.email;
  } catch (err) {
    $("note").textContent = err.message;
  } finally {
    show("working", false);
  }
}

function showEmail(r) {
  show("resultCard", true);
  show("working", false);
  if (!r.email) {
    show("emailBox", true);
    $("email").textContent = "No usable address yet";
    $("emailStatus").replaceChildren();
    $("others").replaceChildren();
    show("editRow", true);
    $("emailInput").value = "";
    $("note").textContent = "Type an address you know or want to try. " + (r.messages || []).join(" ");
    return;
  }
  show("emailBox", true);
  $("email").textContent = r.email;
  const [label, text] = STATUS_TEXT[r.status] || [r.status, ""];
  const box = $("emailStatus");
  box.replaceChildren(el("span", label, "status " + r.status), el("span", text, "status-note"));
  const others = $("others");
  others.replaceChildren();
  for (const c of (r.candidates || []).filter((c) => c.address !== r.email && c.status !== "invalid").slice(0, 3)) {
    others.append(el("li", `${c.address} · ${c.status.replace("_", "-")}`));
  }
  $("note").textContent = (r.messages || []).join(" ");
}

// ---------- wiring ----------

$("go").addEventListener("click", go);
$("openTop").addEventListener("click", () => openQL(state.known && state.known.page));
$("knownOpen").addEventListener("click", (e) => { e.preventDefault(); openQL(state.known && state.known.page); });
$("reread").addEventListener("click", async () => { state.last = { url: "", name: "" }; await doRead(); updateGoButton(); });
$("form").addEventListener("input", () => { state.dirty = true; updateGoButton(); });
$("setDomain").addEventListener("click", () => setDomain($("domain").value));
$("domain").addEventListener("keydown", (e) => { if (e.key === "Enter") setDomain($("domain").value); });
$("edit").addEventListener("click", () => {
  const current = $("email").textContent;
  $("emailInput").value = current.includes("@") ? current : "";
  show("editRow", !$("editRow").classList.contains("hidden") ? false : true);
  $("emailInput").focus();
});
async function saveTypedEmail(address) {
  $("emailInput").value = address;
  show("resultCard", true);
  await saveEmail();
}

async function saveEmail() {
  const address = $("emailInput").value.trim();
  if (!address || !state.contactId) return;
  $("saveEmail").disabled = true;
  show("working", true);
  $("workingText").textContent = "Checking " + address + "…";
  try {
    const r = await api(`/api/contacts/${state.contactId}/email`, {
      method: "POST", body: JSON.stringify({ address, check: true }),
    });
    show("editRow", false);
    showEmail(r);
    if (state.known) state.known.email = r.email;
    $("form").elements.email.value = r.email;
    $("editBox").open = false;
    state.dirty = false;
    updateGoButton();
  } catch (err) { msg(err.message, "err"); }
  show("working", false);
  $("workingText").textContent = "Checking with their mail server…";
  $("saveEmail").disabled = false;
}
$("saveEmail").addEventListener("click", saveEmail);
$("emailInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveEmail(); });
$("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("email").textContent);
  $("copy").textContent = "Copied";
  setTimeout(() => ($("copy").textContent = "Copy"), 1200);
});
$("retry").addEventListener("click", refresh);
$("gear").addEventListener("click", () => $("settings").classList.toggle("hidden"));
$("autoRead").addEventListener("change", async (e) => {
  state.autoRead = e.target.checked;
  await chrome.storage.local.set({ autoRead: state.autoRead });
});
$("backend").addEventListener("change", async (e) => {
  state.backend = e.target.value.trim().replace(/\/$/, "");
  await chrome.storage.local.set({ backend: state.backend });
  refresh();
});

let timer = null;
const soon = () => { clearTimeout(timer); timer = setTimeout(refresh, 400); };
chrome.tabs.onActivated.addListener(soon);
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (state.tab && tabId === state.tab.id && info.url) soon();
});

(async () => {
  const saved = await chrome.storage.local.get(["backend", "autoRead"]);
  if (saved.backend) state.backend = saved.backend;
  if (saved.autoRead === false) state.autoRead = false;
  $("backend").value = state.backend;
  $("autoRead").checked = state.autoRead;
  refresh();
})();
