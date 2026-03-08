(function () {
  "use strict";

  function getInitData() {
    return (typeof Telegram !== "undefined" && Telegram.WebApp && Telegram.WebApp.initData) ? Telegram.WebApp.initData : "";
  }

  var lastQuery = "";
  var currentUserUuid = "";

  function byId(id) {
    return document.getElementById(id);
  }

  function formatBytes(bytesVal) {
    var n = Number(bytesVal) || 0;
    var u = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < u.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(2) + " " + u[i];
  }

  function formatDateTime(str) {
    if (!str) return "Не указано";
    try {
      var s = str.replace("Z", "+00:00");
      var d = new Date(s);
      if (isNaN(d.getTime())) return str;
      var day = ("0" + d.getDate()).slice(-2);
      var month = ("0" + (d.getMonth() + 1)).slice(-2);
      var year = d.getFullYear();
      var h = ("0" + d.getHours()).slice(-2);
      var m = ("0" + d.getMinutes()).slice(-2);
      var sec = ("0" + d.getSeconds()).slice(-2);
      return day + "." + month + "." + year + " " + h + ":" + m + ":" + sec;
    } catch (e) {
      return str;
    }
  }

  function row(label, value) {
    return "<div class=\"row\"><span class=\"label\">" + escapeHtml(label) + "</span><span class=\"value\">" + escapeHtml(String(value == null || value === "" ? "—" : value)) + "</span></div>";
  }

  function rowCode(label, value) {
    return "<div class=\"row\"><span class=\"label\">" + escapeHtml(label) + "</span><span class=\"value\"><code>" + escapeHtml(String(value == null || value === "" ? "—" : value)) + "</code></span></div>";
  }

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  function iconSvg(name) {
    var icons = {
      profile: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>',
      traffic: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>',
      dates: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>',
      subscription: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>',
      devices: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"></rect><line x1="12" y1="18" x2="12.01" y2="18"></line></svg>',
      check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>'
    };
    return icons[name] || '';
  }

  function iconWithText(iconName, text) {
    return '<span class="icon-text"><span class="icon">' + iconSvg(iconName) + '</span>' + escapeHtml(text) + '</span>';
  }

  function renderProfile(user) {
    var html = "<h3>" + iconWithText("profile", "Профиль") + "</h3>";
    html += rowCode("UUID", user.uuid || "N/A");
    html += rowCode("Short UUID", user.shortUuid || "N/A");
    html += row("ID", user.id);
    html += row("Username", user.username ? "@" + user.username : "N/A");
    html += row("Email", user.email || "Не указан");
    html += row("Telegram ID", user.telegramId || "Не указан");
    html += row("Статус", user.status || "N/A");
    html += row("Тег", user.tag || "Не указан");
    html += row("Описание", user.description || "Не указано");
    if (user.hwidDeviceLimit != null) html += row("Лимит устройств", user.hwidDeviceLimit);
    if (user.subLastUserAgent) html += row("Последний User-Agent", user.subLastUserAgent);
    var squads = user.activeInternalSquads;
    if (squads && squads.length) {
      html += "<div class=\"row\"><span class=\"label\">Активные сквады</span><span class=\"value\">" + escapeHtml(squads.map(function (s) { return s.name || "N/A"; }).join(", ")) + "</span></div>";
    }
    return html;
  }

  function renderTraffic(user) {
    var html = "<h3>" + iconWithText("traffic", "Трафик") + "</h3>";
    var ut = user.userTraffic;
    if (ut) {
      var used = ut.usedTrafficBytes || 0;
      var lifetime = ut.lifetimeUsedTrafficBytes || 0;
      var limit = user.trafficLimitBytes || 0;
      html += row("Использовано", formatBytes(used));
      html += row("Всего использовано", formatBytes(lifetime));
      html += row("Лимит", limit > 0 ? formatBytes(limit) : "Безлимит");
      html += row("Стратегия сброса", user.trafficLimitStrategy || "NO_RESET");
      if (ut.onlineAt) html += row("Онлайн", formatDateTime(ut.onlineAt));
      if (ut.firstConnectedAt) html += row("Первое подключение", formatDateTime(ut.firstConnectedAt));
    } else {
      html += "<p class=\"empty\">Нет данных о трафике.</p>";
    }
    return html;
  }

  function renderDates(user) {
    var html = "<h3>" + iconWithText("dates", "Даты") + "</h3>";
    html += row("Истекает", formatDateTime(user.expireAt));
    html += row("Создан", formatDateTime(user.createdAt));
    html += row("Обновлен", formatDateTime(user.updatedAt));
    if (user.subRevokedAt) html += row("Подписка отозвана", formatDateTime(user.subRevokedAt));
    if (user.subLastOpenedAt) html += row("Последнее открытие", formatDateTime(user.subLastOpenedAt));
    if (user.lastTrafficResetAt) html += row("Последний сброс трафика", formatDateTime(user.lastTrafficResetAt));
    return html;
  }

  function renderSubscription(subscription) {
    var html = "<h3>" + iconWithText("subscription", "Подписка") + "</h3>";
    if (!subscription) {
      html += "<p class=\"empty\">Данные о подписке недоступны.</p>";
      return html;
    }
    html += row("Найдена", subscription.isFound ? "Да" : "Нет");
    var subUser = subscription.user;
    if (subUser) {
      html += row("Дней осталось", subUser.daysLeft);
      html += row("Использовано", subUser.trafficUsed);
      html += row("Лимит", subUser.trafficLimit);
      html += row("Всего использовано", subUser.lifetimeTrafficUsed);
      html += row("Активна", subUser.isActive ? "Да" : "Нет");
      html += row("Статус", subUser.userStatus);
    }
    return html;
  }

  function renderHwid(devices, userUuid) {
    var html = "<h3>" + iconWithText("devices", "Устройства (HWID)") + "</h3>";
    if (!devices || !devices.length) {
      html += "<p class=\"empty\">Устройства не найдены.</p>";
      return html;
    }
    html += "<p class=\"empty\">Всего устройств: " + devices.length + "</p>";
    devices.forEach(function (device, i) {
      html += "<div class=\"device-block\" data-hwid=\"" + escapeHtml(device.hwid || "") + "\"><div class=\"device-title\">Устройство " + (i + 1) + "</div>";
      html += rowCode("HWID", device.hwid || "N/A");
      if (device.platform) html += row("Платформа", device.platform);
      if (device.osVersion) html += row("ОС", device.osVersion);
      if (device.deviceModel) html += row("Модель", device.deviceModel);
      if (device.userAgent) html += row("User-Agent", device.userAgent.length > 60 ? device.userAgent.slice(0, 60) + "…" : device.userAgent);
      if (device.createdAt) html += row("Добавлено", formatDateTime(device.createdAt));
      if (device.updatedAt) html += row("Обновлено", formatDateTime(device.updatedAt));
      html += "<button type=\"button\" class=\"btn-device-del\" data-hwid=\"" + escapeHtml(device.hwid || "") + "\">Удалить</button>";
      html += "</div>";
    });
    return html;
  }

  function showLoading(show) {
    byId("loading").classList.toggle("hidden", !show);
  }

  function showError(msg) {
    var el = byId("error");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function showResult(show) {
    byId("result").classList.toggle("hidden", !show);
  }

  function setResult(user, subscription, hwidDevices, query) {
    if (query) lastQuery = query;
    currentUserUuid = user.uuid || "";
    byId("result-username").textContent = user.username ? "@" + user.username : "ID " + (user.telegramId || user.id);
    var statusEl = byId("result-status");
    var s = (user.status || "").toUpperCase();
    var shortStatus = { ACTIVE: "", EXPIRED: "Истёк", DISABLED: "Выкл", LIMITED: "Лимит" }[s] || (user.status || "—");
    if (s === "ACTIVE") {
      statusEl.innerHTML = '<span class="status-icon">' + iconSvg("check") + '</span>';
    } else {
      statusEl.textContent = shortStatus;
    }
    statusEl.classList.toggle("inactive", s !== "ACTIVE");

    byId("actions-row").classList.remove("hidden");
    byId("panel-profile").innerHTML = renderProfile(user);
    byId("panel-traffic").innerHTML = renderTraffic(user);
    byId("panel-dates").innerHTML = renderDates(user);
    byId("panel-subscription").innerHTML = renderSubscription(subscription);
    byId("panel-hwid").innerHTML = renderHwid(hwidDevices, currentUserUuid);

    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === "profile");
    });
    document.querySelectorAll(".panel").forEach(function (p) {
      var name = p.id.replace("panel-", "");
      p.classList.toggle("active", name === "profile");
    });
  }

  function switchTab(tabName) {
    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === tabName);
    });
    document.querySelectorAll(".panel").forEach(function (p) {
      var name = p.id.replace("panel-", "");
      p.classList.toggle("active", name === tabName);
    });
  }

  function callAction(apiPath, body, thenRefresh) {
    var payload = Object.assign({ initData: getInitData() }, body || {});
    showLoading(true);
    fetch(window.location.origin + apiPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        showLoading(false);
        if (data.ok) {
          if (thenRefresh && lastQuery) { showError(""); doLookup(lastQuery); }
        } else {
          showError(data.message || data.error || "Ошибка");
        }
      })
      .catch(function () {
        showLoading(false);
        showError("Ошибка сети.");
      });
  }

  function doLookup(queryOverride) {
    var queryEl = byId("query");
    var query = (queryOverride !== undefined ? queryOverride : (queryEl && queryEl.value || "")).trim();
    if (!query) {
      showError("Введите Telegram ID или @username");
      return;
    }

    showError("");
    showResult(false);
    showLoading(true);

    var apiBase = window.location.origin;
    fetch(apiBase + "/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, initData: getInitData() })
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { status: res.status, data: data };
        });
      })
      .then(function (r) {
        showLoading(false);
        if (r.status === 401) {
          showError("Ошибка авторизации. Откройте приложение из Telegram.");
          return;
        }
        if (r.status === 403) {
          showError("Доступ запрещён. Вы не в списке менеджеров.");
          return;
        }
        if (r.status === 404 || (r.data && r.data.error === "user_not_found")) {
          showError("Пользователь не найден.");
          return;
        }
        if (r.status === 400) {
          showError(r.data && r.data.error === "query_required" ? "Введите запрос." : "Неверный запрос.");
          return;
        }
        if (r.status !== 200 || !r.data || !r.data.ok) {
          showError("Ошибка сервера. Попробуйте позже.");
          return;
        }
        setResult(r.data.user, r.data.subscription || null, r.data.hwid_devices || [], query);
        showResult(true);
      })
      .catch(function () {
        showLoading(false);
        showError("Ошибка сети. Проверьте подключение.");
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = byId("btn-search");
    var queryEl = byId("query");
    if (btn) btn.addEventListener("click", function () { doLookup(); });
    if (queryEl) queryEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") doLookup();
    });

    var btnReset = byId("btn-reset-traffic");
    var btnRevoke = byId("btn-revoke-sub");
    var btnHwidAll = byId("btn-hwid-delete-all");
    if (btnReset) btnReset.addEventListener("click", function () {
      if (!currentUserUuid) return;
      callAction("/api/actions/reset-traffic", { userUuid: currentUserUuid }, true);
    });
    if (btnRevoke) btnRevoke.addEventListener("click", function () {
      if (!currentUserUuid) return;
      callAction("/api/actions/revoke-subscription", { userUuid: currentUserUuid }, true);
    });
    if (btnHwidAll) btnHwidAll.addEventListener("click", function () {
      if (!currentUserUuid) return;
      if (!confirm("Удалить все HWID устройства этого пользователя?")) return;
      callAction("/api/actions/hwid-delete-all", { userUuid: currentUserUuid }, true);
    });

    byId("result").addEventListener("click", function (e) {
      var t = e.target;
      if (t && t.classList && t.classList.contains("btn-device-del")) {
        var hwid = t.getAttribute("data-hwid");
        if (hwid && currentUserUuid) {
          if (!confirm("Удалить это устройство?")) return;
          callAction("/api/actions/hwid-delete", { userUuid: currentUserUuid, hwid: hwid }, true);
        }
      }
    });

    document.querySelectorAll(".tab").forEach(function (t) {
      t.addEventListener("click", function () {
        var tab = this.getAttribute("data-tab");
        if (tab) switchTab(tab);
      });
    });

    if (typeof Telegram !== "undefined" && Telegram.WebApp) {
      Telegram.WebApp.ready();
      Telegram.WebApp.expand();
    }
  });
})();
