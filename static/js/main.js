(function () {
  "use strict";
  var bar = document.getElementById("loading-bar");
  function startLoading() { if (bar) { bar.classList.add("on"); } }
  window.addEventListener("pageshow", function () { if (bar) { bar.classList.remove("on"); } });

  // --- mobile navigation ---
  var toggle = document.getElementById("nav-toggle");
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (toggle) { toggle.setAttribute("aria-expanded", open ? "true" : "false"); }
  }
  if (toggle) {
    toggle.addEventListener("click", function () { setNav(!document.body.classList.contains("nav-open")); });
    var scrim = document.getElementById("scrim");
    if (scrim) { scrim.addEventListener("click", function () { setNav(false); }); }
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { setNav(false); } });
  }

  // --- show / hide password ---
  document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-toggle-password"));
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      btn.firstElementChild.className = show ? "fa-solid fa-eye-slash" : "fa-solid fa-eye";
    });
  });

  // --- confirmation dialog for destructive forms: <form data-confirm="Question?" data-confirm-label="Archive"> ---
  var dlg = document.getElementById("confirm-dialog");
  var pending = null;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.dataset.confirmed === "1") { return; }
    var msg = form.getAttribute("data-confirm");
    if (msg && dlg && typeof dlg.showModal === "function") {
      e.preventDefault();
      pending = form;
      document.getElementById("confirm-text").textContent = msg;
      document.getElementById("confirm-ok").textContent = form.getAttribute("data-confirm-label") || "Confirm";
      dlg.showModal();
      return;
    }
    if (msg && !dlg && !window.confirm(msg)) { e.preventDefault(); return; }
    // loading state on real submissions
    if (!e.defaultPrevented && (form.method || "").toLowerCase() === "post") {
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.classList.add("is-loading"); });
      startLoading();
      setTimeout(function () { form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; }); }, 0);
    } else if (!e.defaultPrevented) { startLoading(); }
  });
  if (dlg) {
    document.getElementById("confirm-ok").addEventListener("click", function () {
      dlg.close();
      if (pending) { pending.dataset.confirmed = "1"; pending.requestSubmit ? pending.requestSubmit() : pending.submit(); }
    });
    document.getElementById("confirm-cancel").addEventListener("click", function () { pending = null; dlg.close(); });
  }

  // --- toasts ---
  document.querySelectorAll(".toast").forEach(function (t) {
    var close = t.querySelector("button");
    if (close) { close.addEventListener("click", function () { t.remove(); }); }
    if (t.classList.contains("success") || t.classList.contains("info")) { setTimeout(function () { t.remove(); }, 6000); }
  });

  // --- links show the progress bar ---
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[href]");
    if (a && !a.target && !e.ctrlKey && !e.metaKey && a.getAttribute("href").charAt(0) !== "#" &&
        a.origin === location.origin && !a.hasAttribute("download") && !a.hasAttribute("data-no-loading")) { startLoading(); }
  });

  // --- attendance helpers ---
  var markAll = document.getElementById("mark-all-present");
  if (markAll) {
    markAll.addEventListener("click", function () {
      document.querySelectorAll("input[type=radio][value=present]").forEach(function (r) { r.checked = true; });
    });
  }
  // selects that reload the page with a query string
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });
  // fee plan fills the amount
  var plan = document.getElementById("plan_id");
  if (plan) {
    plan.addEventListener("change", function () {
      var opt = plan.options[plan.selectedIndex], amt = document.getElementById("amount");
      if (opt && opt.dataset.amount && amt && !amt.dataset.touched) { amt.value = opt.dataset.amount; }
    });
    var amt = document.getElementById("amount");
    if (amt) { amt.addEventListener("input", function () { amt.dataset.touched = "1"; }); }
  }
  var target = document.getElementById("target");
  if (target) {
    var syncTarget = function () {
      document.querySelectorAll("[data-target]").forEach(function (el) { el.hidden = el.getAttribute("data-target") !== target.value; });
    };
    target.addEventListener("change", syncTarget); syncTarget();
  }
  document.querySelectorAll("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  // --- dashboard charts (data comes from the server as JSON) ---
  var dataEl = document.getElementById("chart-data");
  if (dataEl && window.Chart) {
    var d = JSON.parse(dataEl.textContent);
    var violet = "#6d28d9", grid = "#e6e8f0";
    var base = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
                 scales: { y: { beginAtZero: true, grid: { color: grid } }, x: { grid: { display: false } } } };
    var mk = function (id, cfg) { var c = document.getElementById(id); if (c) { return new Chart(c, cfg); } };
    var labels = function (rows) { return rows.map(function (r) { return r.label; }); };
    var values = function (rows) { return rows.map(function (r) { return r.value; }); };
    var currency = dataEl.getAttribute("data-currency") || "";
    mk("chart-fees", { type: "bar", data: { labels: labels(d.fees), datasets: [{ data: values(d.fees), backgroundColor: violet, borderRadius: 6 }] },
      options: Object.assign({}, base, { plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return currency + c.parsed.y.toLocaleString(); } } } } }) });
    mk("chart-attendance", { type: "line", data: { labels: labels(d.attendance), datasets: [{ data: values(d.attendance), borderColor: violet, backgroundColor: "rgba(109,40,217,.12)", fill: true, tension: .3, spanGaps: true, pointRadius: 3 }] },
      options: Object.assign({}, base, { scales: { y: { min: 0, max: 100, grid: { color: grid }, ticks: { callback: function (v) { return v + "%"; } } }, x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } } } }) });
    mk("chart-enrollment", { type: "line", data: { labels: labels(d.enrollment), datasets: [{ data: values(d.enrollment), borderColor: "#0f172a", backgroundColor: "rgba(15,23,42,.08)", fill: true, tension: .25, pointRadius: 3 }] }, options: base });
    mk("chart-belts", { type: "doughnut", data: { labels: labels(d.belts), datasets: [{ data: values(d.belts), backgroundColor: d.belts.map(function (b) { return b.color; }), borderColor: "#334155", borderWidth: 1 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "right", labels: { boxWidth: 12 } } } } });
  }
})();
