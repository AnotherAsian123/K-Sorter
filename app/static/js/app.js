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

// Review-item: when the group changes, reload that group's members.
async function loadMembers(sel) {
  const gid = sel.value;
  const memberSel = sel.parentElement.querySelector("select[name=member_id]");
  if (!gid || !memberSel) return;
  const r = await fetch(`/members/search?group_id=${encodeURIComponent(gid)}`);
  const list = await r.json();
  memberSel.innerHTML =
    '<option value="">— group folder (no member) —</option>' +
    list.map((m) => `<option value="${m.id}">${m.name}</option>`).join("");
}

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

  Alpine.data("manualItem", (id) => ({
    id, q: "", groups: [], groupId: "", members: [], memberId: "", online: [],
    async searchGroups() {
      if (!this.q.trim()) { this.groups = []; return; }
      const r = await fetch(`/groups/search?q=${encodeURIComponent(this.q)}`);
      this.groups = await r.json();
    },
    async loadMembers() {
      this.members = []; this.memberId = "";
      if (!this.groupId) return;
      const r = await fetch(`/members/search?group_id=${encodeURIComponent(this.groupId)}`);
      this.members = await r.json();
    },
    async confirm() {
      if (!this.groupId) return;
      await swapStatus("/resolve", {
        item_id: this.id, group_id: this.groupId,
        member_id: this.memberId, learn: "true",
      });
    },
    async skip() { await swapStatus("/skip", { item_id: this.id }); },
    async lookup() {
      if (!this.q.trim()) { alert("Type a group name to look up first."); return; }
      const r = await postForm("/enrich/search", { name: this.q });
      this.online = await r.json();
    },
    async addOnline(title) {
      const r = await postForm("/enrich/add", { name: title });
      const j = await r.json();
      if (j.ok) {
        this.groups = [{ id: j.group_id, name: title, name_ko: "" }, ...this.groups];
        this.groupId = j.group_id;
        this.online = [];
        await this.loadMembers();
      }
    },
  }));
});
