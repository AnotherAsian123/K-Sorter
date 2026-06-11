// K-Sorter UI glue. Small and calm, like the rest of it.

function toggleTheme() {
  const html = document.documentElement;
  const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", next);
  try { localStorage.setItem("ks-theme", next); } catch (e) {}
}
(function () {
  // Saved choice wins; otherwise follow the system preference.
  let theme = null;
  try { theme = localStorage.getItem("ks-theme"); } catch (e) {}
  if (!theme && window.matchMedia &&
      matchMedia("(prefers-color-scheme: dark)").matches) {
    theme = "dark";
  }
  if (theme) document.documentElement.setAttribute("data-theme", theme);
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
  const html = await r.text();
  // Morph keeps existing DOM nodes (no flash); a view transition makes the
  // resolved block fade out while the rest of the stack glides up.
  const apply = () => {
    if (window.Idiomorph) {
      Idiomorph.morph(el, html, { morphStyle: "innerHTML" });
    } else {
      el.innerHTML = html;
    }
    if (window.htmx) window.htmx.process(el);
  };
  const reduceMotion = window.matchMedia &&
    matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (document.startViewTransition && !reduceMotion) {
    document.startViewTransition(apply);
  } else {
    apply();
  }
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

async function resetLearned(btn) {
  if (!confirm("Forget all learned aliases? Seed and manually-added data are kept.")) return;
  btn.disabled = true;
  const msg = document.getElementById("reset-msg");
  try {
    const r = await postForm("/db/reset-learned", {});
    const j = await r.json();
    msg.textContent = ` Forgot ${j.removed} learned alias(es).`;
  } catch (e) {
    msg.textContent = " Reset failed — see the log file.";
  }
  btn.disabled = false;
}

async function resetApprovals(btn) {
  if (!confirm("Re-check all files in future audits? This forgets which files you marked OK.")) return;
  btn.disabled = true;
  const msg = document.getElementById("approvals-msg");
  try {
    const r = await postForm("/db/reset-approvals", {});
    const j = await r.json();
    msg.textContent = ` Cleared ${j.removed} approval(s).`;
  } catch (e) {
    msg.textContent = " Reset failed — see the log file.";
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
  Alpine.data("resolveItem", (id, groupCands, memberCands, presetGroupId, suggestedMember) => ({
    id,
    groups: groupCands || [],
    members: memberCands || [],
    groupId: presetGroupId || ((groupCands && groupCands[0]) ? groupCands[0].id : ""),
    memberId: "",
    q: "", online: [], busy: false, lookupBusy: false, addBusy: false,
    newMember: suggestedMember || "",
    init() {
      // Whenever the group changes (search, dropdown, or online-add), pull that
      // group's members automatically — no separate member lookup needed.
      this.$watch("groupId", () => this.loadMembers());
      if (this.groupId && !this.members.length) this.loadMembers();
    },
    async searchGroups() {
      const q = this.q.trim();
      if (!q) return;
      const r = await fetch(`/groups/search?q=${encodeURIComponent(q)}`);
      const list = await r.json();
      // Best match first: exact name, then starts-with, then contains.
      const ql = q.toLowerCase();
      const score = (g) => {
        const n = (g.name || "").toLowerCase();
        return n === ql ? 3 : n.startsWith(ql) ? 2 : n.includes(ql) ? 1 : 0;
      };
      list.sort((a, b) => score(b) - score(a));
      this.groups = list;
      if (list.length) {
        // Selecting the top match triggers the watcher -> members load.
        if (this.groupId !== list[0].id) this.groupId = list[0].id;
        else this.loadMembers();
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
    async addMember() {
      const name = this.newMember.trim();
      if (!this.groupId || !name) return;
      const r = await postForm("/members/add", { group_id: this.groupId, name });
      const j = await r.json();
      if (j.ok) {
        this.members = [{ id: j.member_id, name: j.name, name_ko: "", current: true }, ...this.members];
        this.memberId = j.member_id;
        this.newMember = "";
      } else {
        alert(j.error || "Could not add member.");
      }
    },
    async skip() { await swapStatus("/skip", { item_id: this.id }); },
    async lookup() {
      const term = this.q.trim();
      if (!term) {
        // Inline guidance instead of a silent alert; focus the search box.
        this.online = [{ title: "Type the group's name in the Search box above, then click Look up online.", url: "#", _none: true }];
        this.$nextTick(() => { const i = this.$el.querySelector('input[x-model="q"]'); if (i) i.focus(); });
        return;
      }
      this.lookupBusy = true;
      this.online = [{ title: "Searching the web for “" + term + "”…", url: "#", _loading: true }];
      try {
        const r = await postForm("/enrich/search", { name: term });
        const list = await r.json();
        this.online = list.length ? list
          : [{ title: "No results — try a different spelling or the Korean name.", url: "#", _none: true }];
      } catch (e) {
        this.online = [{ title: "Lookup failed — the server may have no internet access (see the log file).", url: "#", _none: true }];
      } finally {
        this.lookupBusy = false;
      }
    },
    async addOnline(cand) {
      if (cand._loading || cand._none || this.addBusy) return;
      this.addBusy = true;
      const r = await postForm("/enrich/add", { name: cand.title });
      const j = await r.json();
      this.addBusy = false;
      if (j.ok) {
        this.groups = [{ id: j.group_id, name: cand.title, name_ko: "" }, ...this.groups];
        this.groupId = j.group_id;  // watcher loads members (empty for a new group)
        this.online = [];
      }
    },
  }));
});
