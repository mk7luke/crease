(function () {
  var form = document.getElementById("form");
  var urlEl = document.getElementById("url");
  var statusEl = document.getElementById("status");
  var go = document.getElementById("go");
  var stage = document.getElementById("stage");
  var meta = document.getElementById("meta");
  var coverFrame = document.getElementById("cover-frame");
  var innerFrame = document.getElementById("inner-frame");
  var innerPhone = document.getElementById("inner-phone");
  var innerName = document.getElementById("inner-name");
  var innerSize = document.getElementById("inner-size");
  var creaseBand = document.getElementById("crease-band");
  var poseBtn = document.getElementById("toggle-pose");
  var creaseBtn = document.getElementById("toggle-crease");
  var copyBtn = document.getElementById("copy-link");
  var upright = false;
  var creaseOn = true;
  var lastFrame = "";
  var INNER_OPEN = { w: 951, h: 669 };
  var INNER_UP = { w: 669, h: 951 };

  function setStatus(msg, err) {
    statusEl.textContent = msg || "";
    statusEl.className = err ? "status err" : "status";
  }

  function fitFrames() {
    var wraps = document.querySelectorAll("[data-fit]");
    for (var i = 0; i < wraps.length; i++) {
      var wrap = wraps[i];
      var frame = wrap.querySelector("iframe");
      if (!frame) continue;
      var w = frame.offsetWidth;
      var h = frame.offsetHeight;
      var avail = wrap.parentElement ? wrap.parentElement.clientWidth : w;
      var maxW = Math.min(avail, window.innerWidth - 48);
      if (maxW < 80) maxW = 80;
      var scale = Math.min(1, maxW / w);
      wrap.style.width = w * scale + "px";
      wrap.style.height = h * scale + "px";
      frame.style.transform = "scale(" + scale + ")";
    }
  }

  function applyPose() {
    var spec = upright ? INNER_UP : INNER_OPEN;
    innerFrame.style.width = spec.w + "px";
    innerFrame.style.height = spec.h + "px";
    innerPhone.classList.toggle("upright", upright);
    innerName.textContent = upright ? "Inner upright" : "Inner open";
    innerSize.textContent = upright
      ? "7.6 in · 669 × 951"
      : "7.6 in · 951 × 669";
    poseBtn.setAttribute("aria-pressed", upright ? "true" : "false");
    poseBtn.textContent = upright ? "Inner open" : "Inner upright";
    creaseBand.classList.toggle("off", !creaseOn);
    creaseBtn.setAttribute("aria-pressed", creaseOn ? "true" : "false");
    creaseBtn.textContent = creaseOn ? "Crease on" : "Crease off";
    fitFrames();
  }

  function showPreview(data) {
    lastFrame = data.frame;
    coverFrame.src = data.frame;
    innerFrame.src = data.frame;
    var bits = [];
    bits.push(data.status ? "HTTP " + data.status : "");
    if (data.final_url) bits.push(data.final_url);
    if (data.truncated) bits.push("HTML truncated at 1 MB");
    meta.textContent = bits.filter(Boolean).join(" · ");
    stage.hidden = false;
    applyPose();
    try {
      var u = new URL(window.location.href);
      u.searchParams.set("u", data.requested || urlEl.value);
      history.replaceState(null, "", u.pathname + u.search);
    } catch (e) {}
    stage.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function run(url) {
    url = (url || "").trim();
    if (!url) {
      setStatus("Need a URL.", true);
      return;
    }
    go.disabled = true;
    setStatus("Fetching…");
    fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, status: r.status, body: j };
        });
      })
      .then(function (pack) {
        if (!pack.ok) {
          var msg = pack.body && (pack.body.detail || pack.body.error);
          if (Array.isArray(msg)) {
            msg = msg.map(function (x) { return x.msg || JSON.stringify(x); }).join(", ");
          }
          throw new Error(msg || ("HTTP " + pack.status));
        }
        showPreview(pack.body);
        setStatus("");
      })
      .catch(function (err) {
        setStatus(err.message || "Preview failed.", true);
      })
      .then(function () {
        go.disabled = false;
      });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    run(urlEl.value);
  });

  document.querySelectorAll(".chip").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var u = btn.getAttribute("data-url");
      urlEl.value = u;
      run(u);
    });
  });

  poseBtn.addEventListener("click", function () {
    upright = !upright;
    applyPose();
  });
  creaseBtn.addEventListener("click", function () {
    creaseOn = !creaseOn;
    applyPose();
  });
  copyBtn.addEventListener("click", function () {
    var href = window.location.href;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(href).then(function () {
        copyBtn.textContent = "Copied";
        setTimeout(function () { copyBtn.textContent = "Copy link"; }, 1200);
      });
    }
  });

  window.addEventListener("resize", fitFrames);

  var params = new URLSearchParams(window.location.search);
  var initial = params.get("u");
  if (initial) {
    urlEl.value = initial;
    run(initial);
  }
})();
