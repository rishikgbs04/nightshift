// nightshift app: swipe to approve/skip, undo, run status. No framework.
(() => {
  const toast = document.getElementById("toast");
  const toastMsg = document.getElementById("toast-msg");
  const undoBtn = document.getElementById("toast-undo");
  let toastTimer = null;
  let undoFn = null;

  function showToast(msg, undo) {
    toastMsg.textContent = msg;
    undoFn = undo || null;
    undoBtn.hidden = !undo;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 5000);
  }
  undoBtn?.addEventListener("click", () => { if (undoFn) undoFn(); toast.classList.remove("show"); });

  async function act(id, what, to) {
    const r = await fetch("/api/action", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ id, do: what, to: to || "" }),
    });
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    if (!r.ok) throw new Error("failed");
    return r.json();
  }

  const LABEL = { approve: "Approved", skip: "Skipped", applied: "Marked as applied", requeue: "Moved to review" };
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function updateCounts(counts) {
    document.querySelectorAll("[data-count]").forEach((el) => (el.textContent = counts[el.dataset.count] || 0));
    const head = document.getElementById("headline");
    if (head) {
      const n = counts.queued || 0;
      head.textContent = n ? `${n} ${n === 1 ? "job" : "jobs"} ready for you` : "You're all caught up";
    }
    document.querySelectorAll(".tab").forEach((t) => {
      const key = new URL(t.href).searchParams.get("status");
      const c = t.querySelector(".c");
      if (c && key in counts) c.textContent = counts[key];
      if (c && !(key in counts)) c.textContent = "0";
    });
  }

  function removeCard(el) {
    const h = el.offsetHeight;
    el.style.height = h + "px";
    el.classList.add("leaving");
    requestAnimationFrame(() => { el.style.height = "0px"; el.style.opacity = "0"; el.style.marginBottom = "-12px"; });
    setTimeout(() => { el.remove(); maybeEmpty(); }, reduce ? 0 : 260);
  }

  function maybeEmpty() {
    const feed = document.getElementById("feed");
    if (feed && !feed.querySelector(".swipe")) location.reload();
  }

  async function decide(swipe, what) {
    const id = swipe.dataset.id;
    swipe.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const res = await act(id, what);
      updateCounts(res.counts);
      removeCard(swipe);
      showToast(LABEL[what] || "Done", async () => {
        const back = await act(id, "undo", res.previous);
        updateCounts(back.counts);
        location.reload();
      });
    } catch (e) {
      swipe.querySelectorAll("button").forEach((b) => (b.disabled = false));
      const card = swipe.querySelector(".card");
      if (card) card.style.transform = "";
      swipe.classList.remove("dir-left", "dir-right");
      if (e.message !== "signed out") showToast("Couldn't save that. Check your connection and try again.");
    }
  }

  // buttons in feed cards
  document.querySelectorAll(".swipe [data-act]").forEach((b) =>
    b.addEventListener("click", () => decide(b.closest(".swipe"), b.dataset.act)));

  // buttons in the detail page dock
  document.querySelectorAll(".dock [data-act]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const res = await act(b.dataset.id, b.dataset.act);
        showToast(LABEL[b.dataset.act] || "Done", async () => { await act(b.dataset.id, "undo", res.previous); location.reload(); });
        setTimeout(() => (location.href = "/?status=" + encodeURIComponent(res.previous || "queued")), 900);
      } catch (e) { b.disabled = false; if (e.message !== "signed out") showToast("Couldn't save that. Try again."); }
    }));

  // swipe gestures on review cards
  const THRESHOLD = 96;
  document.querySelectorAll(".card[data-swipeable]").forEach((card) => {
    const swipe = card.closest(".swipe");
    let x0 = 0, y0 = 0, dx = 0, active = false, horizontal = null, pid = null;
    card.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      x0 = e.clientX; y0 = e.clientY; dx = 0; active = true; horizontal = null; pid = e.pointerId;
    });
    card.addEventListener("pointermove", (e) => {
      if (!active || e.pointerId !== pid) return;
      const mx = e.clientX - x0, my = e.clientY - y0;
      if (horizontal === null && (Math.abs(mx) > 8 || Math.abs(my) > 8)) {
        horizontal = Math.abs(mx) > Math.abs(my);
        if (horizontal) { card.setPointerCapture(pid); card.classList.add("dragging"); }
      }
      if (!horizontal) return;
      dx = mx;
      card.style.transform = `translateX(${dx}px) rotate(${dx / 40}deg)`;
      swipe.classList.toggle("dir-right", dx > 12);
      swipe.classList.toggle("dir-left", dx < -12);
    });
    const end = () => {
      if (!active) return;
      active = false;
      card.classList.remove("dragging");
      if (horizontal && Math.abs(dx) > THRESHOLD) {
        card.style.transform = `translateX(${dx > 0 ? 120 : -120}%)`;
        decide(swipe, dx > 0 ? "approve" : "skip");
      } else {
        card.style.transform = "";
        swipe.classList.remove("dir-left", "dir-right");
      }
    };
    card.addEventListener("pointerup", end);
    card.addEventListener("pointercancel", end);
    // a drag shouldn't also open the job
    card.addEventListener("click", (e) => { if (Math.abs(dx) > 8) { e.preventDefault(); dx = 0; } }, true);
  });

  // "Add a job": tailoring takes a few seconds
  document.getElementById("add-form")?.addEventListener("submit", () => {
    const b = document.getElementById("add-btn");
    b.disabled = true; b.textContent = "Tailoring…";
    document.getElementById("add-wait").hidden = false;
  });

  // settings: run now + live status
  const runBtn = document.getElementById("run-now");
  async function poll() {
    const r = await fetch("/api/run");
    if (!r.ok) return;
    const s = await r.json();
    const dot = document.getElementById("run-dot");
    const text = document.getElementById("run-text");
    document.getElementById("run-log").textContent = (s.log || []).join("\n");
    if (s.running) {
      dot.className = "dot on"; text.textContent = "Running now"; runBtn.disabled = true;
      setTimeout(poll, 3000);
    } else {
      runBtn.disabled = false;
      if (s.error) { dot.className = "dot bad"; text.textContent = "Last run failed: " + s.error; }
      else if (s.finished) { dot.className = "dot"; text.textContent = `Finished. ${s.counts.queued || 0} ready to review.`; }
    }
  }
  runBtn?.addEventListener("click", async () => {
    runBtn.disabled = true;
    await fetch("/api/run", { method: "POST" });
    setTimeout(poll, 800);
  });
  if (runBtn && runBtn.disabled) setTimeout(poll, 1500);

  document.getElementById("notify-test")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    const r = await fetch("/api/notify-test", { method: "POST" });
    const ok = r.ok && (await r.json()).ok;
    const p = document.getElementById("notify-result");
    p.hidden = false;
    p.textContent = ok ? "Sent. Check your phone." : "Couldn't send. Check the ntfy topic in Settings (config.yaml).";
    e.target.disabled = false;
  });

  // inbox: check now
  document.querySelectorAll("#inbox-check").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; const label = b.textContent; b.textContent = "Checking…";
    try {
      const r = await fetch("/api/inbox-check", { method: "POST" });
      const d = await r.json();
      if (!r.ok) { showToast(d.error || "Couldn't check your email."); }
      else { showToast(d.matched ? `${d.matched} new ${d.matched === 1 ? "reply" : "replies"}` : "No new replies"); if (d.matched) setTimeout(() => location.reload(), 900); }
    } catch (e) { showToast("Couldn't reach the server."); }
    b.disabled = false; b.textContent = label;
  }));

  // people: send / don't send / undo / save edits
  document.querySelectorAll(".contact [data-cact]").forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest(".contact");
    const id = card.dataset.cid;
    b.disabled = true;
    try {
      if (b.dataset.cact === "save") {
        const r = await fetch("/api/contact-edit", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ id, subject: card.querySelector(".c-subject").value, body: card.querySelector(".c-body").value }) });
        const d = await r.json();
        card.querySelector(".c-body").value = d.body;
        showToast("Saved");
      } else {
        const r = await fetch("/api/contact", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ id, do: b.dataset.cact }) });
        if (r.status === 401) { location.href = "/login"; return; }
        if (!r.ok) throw new Error();
        const d = await r.json();
        showToast(d.label);
        setTimeout(() => location.reload(), 700);
      }
    } catch (e) { showToast("Couldn't save that. Try again."); }
    b.disabled = false;
  }));

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
