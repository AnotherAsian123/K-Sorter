// K-Sorter UI glue. Small and calm, like the rest of it.

function toggleTheme() {
  const html = document.documentElement;
  const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", next);
  try { localStorage.setItem("ks-theme", next); } catch (e) {}
}
(function () {
  try {
    const saved = localStorage.getItem("ks-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {}
})();

async function postForm(url, data) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(data),
  });
}

async function swapStatus(url, data) {
  const r = await postForm(url, data);
  const el = document.getElementById("status");
  el.innerHTML = await r.text();
  if (window.htmx) window.htmx.process(el);
}

async function updateDb(btn) {
  const msg = document.getElementById("db-msg");
  btn.disabled = true;
  msg.textContent = " Updating in the background…";
  try {
    const r = await postForm("/update-db", {});
    const j = await r.json();
    msg.textContent = " " + (j.message || "Started.");
  } catch (e) {
    msg.textContent = " Update failed — see the log file for full details.";
  }
  btn.disabled = false;
}

async function undoBatch(batchId, btn) {
  btn.disabled = true;
  btn.textContent = "Undoing…";
  try {
    const r = await postForm("/undo", { batch_id: batchId });
    const j = await r.json();
    btn.textContent = `Restored ${j.restored}`;
    setTimeout(() => location.reload(), 700);
  } catch (e) {
    btn.textContent = "Undo failed";
  }
}

// Keep the page where it is across #status swaps (so confirming an item doesn't
// jump you to the bottom). Covers htmx forms + polling.
(function () {
  let y = null;
  function isStatus(e) {
    const t = (e.detail && e.detail.target) || e.target;
    return t && t.id === "status";
  }
  document.addEventListener("htmx:beforeSwap", (e) => { if (isStatus(e)) y = window.scrollY; });
  document.addEventListener("htmx:afterSettle", (e) => {
    if (isStatus(e) && y !== null) { window.scrollTo(0, y); y = null; }
  });
})();

document.addEventListener("alpine:init", () => {
  const n = window.KS_NAMING || { language: "en", template: "nested" };
  const d = window.KS_DEFAULTS || { source: "", dest: "" };

  Alpine.data("setup", () => ({
    source: d.source, dest: d.dest, mode: "apply",
    language: n.language, template: n.template,
    saveSettings() {
      postForm("/settings", { language: this.language, template: this.template });
    },
  }));

  // One resolver for BOTH the confirm and manual queues: candidate quick-picks
  // PLUS a search box and online lookup, so you can always reach the right group
  // even when the suggestions are wrong or the group isn't in the DB yet.
  Alpine.data("resolveItem", (id, groupCands, memberCands, presetGroupId) => ({
    id,
    groups: groupCands || [],
    members: memberCands || [],
    groupId: presetGroupId || ((groupCands && groupCands[0]) ? groupCands[0].id : ""),
    memberId: "",
    q: "", online: [], busy: false,
    init() { if (this.groupId && !this.members.length) this.loadMembers(); },
    async searchGroups() {
      if (!this.q.trim()) return;
      const r = await fetch(`/groups/search?q=${encodeURIComponent(this.q)}`);
      this.groups = await r.json();
      if (this.groups.length && !this.groups.find((g) => g.id === this.groupId)) {
        this.groupId = this.groups[0].id;
        await this.loadMembers();
      }
    },
    async loadMembers() {
      this.memberId = "";
      if (!this.groupId) { this.members = []; return; }
      const r = await fetch(`/members/search?group_id=${encodeURIComponent(this.groupId)}`);
      this.members = await r.json();
    },
    async confirm() {
      if (!this.groupId || this.busy) return;
      this.busy = true;
      await swapStatus("/resolve", {
        item_id: this.id, group_id: this.groupId,
        member_id: this.memberId, learn: "true",
      });
    },
    async skip() { await swapStatus("/skip", { item_id: this.id }); },
    async lookup() {
      const term = this.q.trim();
      if (!term) { alert("Type the group's name in the search box first, then look it up."); return; }
      this.online = [{ title: "Searching…", url: "#", _loading: true }];
      const r = await postForm("/enrich/search", { name: term });
      this.online = await r.json();
      if (!this.online.length) this.online = [{ title: "No results — try a different spelling.", url: "#", _none: true }];
    },
    async addOnline(cand) {
      if (cand._loading || cand._none) return;
      const r = await postForm("/enrich/add", { name: cand.title });
      const j = await r.json();
      if (j.ok) {
        this.groups = [{ id: j.group_id, name: cand.title, name_ko: "" }, ...this.groups];
        this.groupId = j.group_id;
        this.online = [];
        await this.loadMembers();
      }
    },
  }));
});
