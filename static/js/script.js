document.addEventListener("DOMContentLoaded", function () {
  var toggle = document.getElementById("navToggle");
  var links = document.getElementById("navLinks");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      links.classList.toggle("open");
    });
  }

  document.querySelectorAll(".flash").forEach(function (el) {
    setTimeout(function () {
      el.style.transition = "opacity 0.4s ease";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 400);
    }, 5000);
  });

  var themeToggle = document.getElementById("themeToggle");
  var savedTheme = localStorage.getItem("lifelink-theme");
  if (savedTheme === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
    if (themeToggle) themeToggle.textContent = "\u2600";
  }

  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      if (current === "dark") {
        document.documentElement.removeAttribute("data-theme");
        localStorage.setItem("lifelink-theme", "light");
        themeToggle.textContent = "\u263E";
      } else {
        document.documentElement.setAttribute("data-theme", "dark");
        localStorage.setItem("lifelink-theme", "dark");
        themeToggle.textContent = "\u2600";
      }
    });
  }

  initProfileLocation();
});

// ---------------------------------------------------------------------------
// PROFILE LOCATION (GPS detection + search/select autocomplete)
// ---------------------------------------------------------------------------
// The profile page stores the user's location together with its coordinates.
// Two ways to set it, both ending in a POST to /api/profile/location:
//   1. "Use My Location" - the browser asks for permission (navigator.geolocation)
//      and the server reverse-geocodes the fix to the nearest known area name.
//   2. Search/select - a debounced autocomplete over the built-in location
//      table; picking a place fills in its coordinates instantly.
// Either way the coordinates are written into hidden latitude/longitude inputs
// so the normal "Save Changes" form persists them (the server validates them).
function initProfileLocation() {
  var search = document.getElementById("locationSearch");
  var results = document.getElementById("locationResults");
  var status = document.getElementById("locationStatus");
  var latInput = document.getElementById("latitude");
  var lngInput = document.getElementById("longitude");
  var gpsBtn = document.getElementById("useMyLocation");
  var saved = document.getElementById("savedLocation");
  var savedName = document.getElementById("savedLocationName");
  var savedCoords = document.getElementById("savedLocationCoords");
  if (!search || !results) return;

  var debounceTimer = null;

  function setStatus(text, kind) {
    if (!status) return;
    status.textContent = text;
    status.className = "loc-status" + (kind ? " loc-" + kind : "");
  }

  function updateSaved(location, lat, lng) {
    if (saved) saved.hidden = false;
    if (savedName) savedName.textContent = location || "\u2014";
    if (savedCoords) {
      var hasLat = lat !== null && lat !== undefined && lat !== "";
      var hasLng = lng !== null && lng !== undefined && lng !== "";
      savedCoords.textContent =
        hasLat && hasLng
          ? " (" + Number(lat).toFixed(4) + ", " + Number(lng).toFixed(4) + ")"
          : "";
    }
  }

  function saveToServer(payload, done) {
    fetch("/api/profile/location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      if (res.ok && res.data.ok) {
        if (latInput) latInput.value = res.data.latitude != null ? res.data.latitude : "";
        if (lngInput) lngInput.value = res.data.longitude != null ? res.data.longitude : "";
        if (search && res.data.location) search.value = res.data.location;
        updateSaved(res.data.location, res.data.latitude, res.data.longitude);
        if (done) done(true, res.data);
      } else {
        if (done) done(false, res.data);
      }
    }).catch(function () {
      if (done) done(false, {});
    });
  }

  function showResults(items) {
    results.innerHTML = "";
    if (!items || !items.length) {
      results.hidden = true;
      return;
    }
    items.forEach(function (p) {
      var opt = document.createElement("button");
      opt.type = "button";
      opt.className = "loc-option";
      opt.textContent = p.name;
      opt.addEventListener("click", function () {
        search.value = p.name;
        if (latInput) latInput.value = p.latitude;
        if (lngInput) lngInput.value = p.longitude;
        results.hidden = true;
        updateSaved(p.name, p.latitude, p.longitude);
        setStatus("Location selected: " + p.name + " \u2014 coordinates detected automatically.", "ok");
        saveToServer(
          { location: p.name, latitude: p.latitude, longitude: p.longitude },
          function (okSave, d) {
            if (okSave) setStatus("Location saved: " + (d.location || p.name) + ".", "ok");
            else setStatus("Coordinates detected, but saving failed. Use Save Changes to retry.", "warn");
          }
        );
      });
      results.appendChild(opt);
    });
    results.hidden = false;
  }

  search.addEventListener("input", function () {
    var q = search.value.trim();
    clearTimeout(debounceTimer);
    if (q.length < 2) {
      results.hidden = true;
      return;
    }
    debounceTimer = setTimeout(function () {
      fetch("/api/locations/search?q=" + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (d) { showResults(d.results || []); })
        .catch(function () { results.hidden = true; });
    }, 220);
  });

  document.addEventListener("click", function (e) {
    if (!results.contains(e.target) && e.target !== search) results.hidden = true;
  });

  if (gpsBtn) {
    gpsBtn.addEventListener("click", function () {
      if (!navigator.geolocation) {
        setStatus("GPS isn't supported by this browser. Search and select your location instead.", "warn");
        return;
      }
      setStatus("Detecting your location \u2026 the browser may ask for permission.", "info");
      gpsBtn.disabled = true;
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          gpsBtn.disabled = false;
          var lat = pos.coords.latitude;
          var lng = pos.coords.longitude;
          saveToServer({ latitude: lat, longitude: lng }, function (okSave, d) {
            if (okSave) {
              setStatus("Location detected: " + d.location + " (" + d.latitude + ", " + d.longitude + ").", "ok");
            } else {
              setStatus("Got your GPS position but saving it failed. Try again or pick your area manually.", "warn");
            }
          });
        },
        function (err) {
          gpsBtn.disabled = false;
          if (err.code === err.PERMISSION_DENIED) {
            setStatus("Location permission was denied. Search and select your location manually instead.", "warn");
          } else if (err.code === err.POSITION_UNAVAILABLE) {
            setStatus("Your current position isn't available. Search and select your location manually instead.", "warn");
          } else {
            setStatus("Couldn't detect your location. Search and select your location manually instead.", "warn");
          }
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
      );
    });
  }
}

/* ==========================================================================
   CHATBOT WIDGET -- LifeLink Assistant
   ========================================================================== */
(function () {
  const toggleBtn = document.getElementById('chatbotToggle');
  const closeBtn = document.getElementById('chatbotClose');
  const panel = document.getElementById('chatbotPanel');
  const messagesEl = document.getElementById('chatbotMessages');
  const suggestionsEl = document.getElementById('chatbotSuggestions');
  const form = document.getElementById('chatbotForm');
  const input = document.getElementById('chatbotInput');

  if (!toggleBtn || !panel) return; // widget not on this page

  let history = [];      // [{role, content}]
  let questionsLoaded = false;

  function openPanel() {
    panel.hidden = false;
    input.focus();
    if (!questionsLoaded) loadCommonQuestions();
  }
  function closePanel() { panel.hidden = true; }

  toggleBtn.addEventListener('click', () => {
    panel.hidden ? openPanel() : closePanel();
  });
  closeBtn.addEventListener('click', closePanel);

  function addMessage(text, who) {
    const div = document.createElement('div');
    div.className = 'chatbot-msg ' + (who === 'user' ? 'chatbot-msg-user' : 'chatbot-msg-bot');
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function loadCommonQuestions() {
    questionsLoaded = true;
    fetch('/api/chatbot/common-questions')
      .then((r) => r.json())
      .then((data) => {
        (data.questions || []).forEach((q) => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'chatbot-suggestion-btn';
          btn.textContent = q;
          btn.addEventListener('click', () => sendMessage(q));
          suggestionsEl.appendChild(btn);
        });
      })
      .catch(() => { /* silently skip suggestions if this fails */ });
  }

  function sendMessage(text) {
    text = (text || '').trim();
    if (!text) return;

    addMessage(text, 'user');
    input.value = '';
    suggestionsEl.innerHTML = ''; // clear quick-replies once the chat starts
    history.push({ role: 'user', content: text });

    const typing = document.createElement('div');
    typing.className = 'chatbot-msg chatbot-msg-bot chatbot-msg-typing';
    typing.textContent = 'Typing...';
    messagesEl.appendChild(typing);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;

    fetch('/api/chatbot/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: history.slice(-6) }),
    })
      .then((r) => r.json())
      .then((data) => {
        typing.remove();
        const reply = data.reply || "Sorry, I couldn't get an answer just now.";
        addMessage(reply, 'bot');
        history.push({ role: 'assistant', content: reply });
      })
      .catch(() => {
        typing.remove();
        addMessage('Something went wrong reaching the assistant. Please try again.', 'bot');
      })
      .finally(() => { submitBtn.disabled = false; });
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    sendMessage(input.value);
  });
})();

/* ==========================================================================
   SHOW/HIDE PASSWORD TOGGLE -- applied automatically to every password field
   ========================================================================== */
(function () {
  function addToggle(input) {
    if (input.dataset.pwToggleAdded) return;
    input.dataset.pwToggleAdded = "1";

    const wrapper = document.createElement('div');
    wrapper.className = 'pw-field-wrapper';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle-btn';
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('tabindex', '-1');
    btn.innerHTML = eyeIcon(false);
    wrapper.appendChild(btn);

    btn.addEventListener('click', () => {
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.innerHTML = eyeIcon(!showing);
      btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
    });
  }

  function eyeIcon(open) {
    return open
      ? '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.6 18.6 0 0 1 5.06-5.94M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a18.6 18.6 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>'
      : '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  }

  document.querySelectorAll('input[type="password"]').forEach(addToggle);
})();
