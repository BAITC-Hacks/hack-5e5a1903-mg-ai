/**
 * Логика дашборда: восемь страниц собираются из ответов API прогноза.
 *
 * Синтетических данных здесь нет: каждое число на экране приходит из
 * /api/forecast/*, а поле data_source из ответа показывается пользователем
 * плашкой «демонстрационные данные», пока бэкенд отдает заглушку.
 *
 * Разметка лежит в index.html (шаблон x-dc), сюда она обращается через
 * ZHEL.view(): функция возвращает ровно те поля, которые упомянуты в шаблоне.
 */
window.ZHEL = (function () {
  "use strict";

  var Api = window.ZhelApi;
  var E = window.React.createElement;

  // --- палитра и мелкие помощники -----------------------------------------

  var LIME = "oklch(0.6 0.16 135)";
  var CYAN = "oklch(0.6 0.12 225)";
  var AMBER = "oklch(0.6 0.16 75)";
  var VIOLET = "oklch(0.6 0.16 300)";
  var RED = "oklch(0.6 0.18 25)";
  var MODEL_COLORS = [LIME, CYAN, AMBER, VIOLET, RED];
  var GREY = "#5F6762";

  function clamp(x, a, b) {
    return Math.max(a, Math.min(b, x));
  }
  function mean(a) {
    return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : 0;
  }
  function sum(a) {
    return a.reduce(function (s, x) { return s + x; }, 0);
  }
  function pct(v) {
    return Math.round(v * 100);
  }
  function pad(n) {
    return String(n).padStart(2, "0");
  }

  // Время местное (UTC+5) приходит строкой вида 2026-02-04T17:00:00Z.
  // Разбираем ее как текст: перевод через Date сдвинул бы часы на пояс браузера.
  function hhmm(iso) {
    return iso ? iso.slice(11, 16) : "—";
  }
  function ddmm(iso) {
    return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) : "—";
  }
  function ddmmyyyy(iso) {
    return iso ? ddmm(iso) + "." + iso.slice(0, 4) : "—";
  }
  function full(iso) {
    return iso ? ddmm(iso) + " " + hhmm(iso) : "—";
  }
  function hhmmss(iso) {
    return iso ? iso.slice(11, 19) : "—";
  }
  function isMidnight(iso) {
    return iso && iso.slice(11, 13) === "00";
  }
  function num(v, digits) {
    return v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(digits === undefined ? 1 : digits);
  }

  function plural(n, one, few, many) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function linePath(arr, max, min) {
    var lo = min === undefined ? 0 : min;
    var span = max - lo || 1;
    var n = arr.length;
    if (!n) return "";
    if (n === 1) arr = [arr[0], arr[0]];
    return arr
      .map(function (v, i) {
        var x = ((i / (arr.length - 1)) * 1000).toFixed(1);
        var y = (300 - clamp((v - lo) / span, 0, 1) * 300).toFixed(1);
        return (i ? "L" : "M") + x + "," + y;
      })
      .join("");
  }

  function bandPath(lo, hi, max) {
    var n = hi.length;
    if (!n) return "";
    var x = function (i) { return ((i / (n - 1)) * 1000).toFixed(1); };
    var y = function (v) { return (300 - clamp(v / max, 0, 1) * 300).toFixed(1); };
    var d = "M" + x(0) + "," + y(hi[0]);
    for (var i = 1; i < n; i++) d += "L" + x(i) + "," + y(hi[i]);
    for (var j = n - 1; j >= 0; j--) d += "L" + x(j) + "," + y(lo[j]);
    return d + "Z";
  }

  // --- рисунки турбин ------------------------------------------------------

  var BLADE = "M-4,-2 C-7,-30 -5,-70 -1.5,-100 L1.5,-100 C4,-70 6,-35 5,-2 Z";

  function rotor(dur, fill, stroke, key) {
    var blades = [0, 120, 240].map(function (a) {
      return E("path", { key: a, d: BLADE, transform: "rotate(" + a + ")", fill: fill, stroke: stroke, strokeWidth: 1.2, strokeLinejoin: "round" });
    });
    return E(
      "g",
      {
        key: key,
        style: {
          transformOrigin: "0px 0px",
          transformBox: "view-box",
          animation: "zspin " + (dur || 4) + "s linear infinite",
          animationPlayState: dur ? "running" : "paused",
        },
      },
      blades[0],
      blades[1],
      blades[2],
    );
  }

  function turbineEl(dur) {
    return E(
      "svg",
      { viewBox: "-110 -110 220 330", width: "100%", height: "100%", style: { overflow: "visible", display: "block" } },
      E("path", { d: "M-3,4 L3,4 L7,220 L-7,220 Z", fill: "#FFFFFF", stroke: "rgba(20,24,22,0.28)", strokeWidth: 1 }),
      E("rect", { x: -9, y: -8, width: 30, height: 16, rx: 6, fill: "#FFFFFF", stroke: "rgba(20,24,22,0.28)", strokeWidth: 1 }),
      rotor(dur, "#FFFFFF", "rgba(20,24,22,0.28)", "r"),
      E("circle", { r: 8, fill: "#FFFFFF", stroke: "rgba(20,24,22,0.2)", strokeWidth: 1 }),
      E("circle", { r: 3, fill: LIME }),
    );
  }

  function rotorEl(dur, fill, stroke, hub) {
    return E(
      "svg",
      { viewBox: "-106 -106 212 212", width: "100%", height: "100%", style: { display: "block", overflow: "visible" } },
      rotor(dur, fill, stroke, "r"),
      E("circle", { r: 13, fill: hub }),
    );
  }

  function rotorDuration(windMs) {
    return windMs < 3 ? 0 : +clamp(36 / windMs, 0.9, 9).toFixed(2);
  }

  // --- ресурсы API ---------------------------------------------------------

  var RES = {
    issues: { scope: "global", path: function () { return "/forecast/issues?page=1&size=100"; }, pick: function (r) { return r.items; } },
    site: { scope: "global", path: function () { return "/forecast/site"; } },
    backtest: { scope: "global", path: function () { return "/forecast/backtest"; } },
    model: { scope: "global", path: function () { return "/forecast/model"; } },
    forecast: { scope: "date", path: function (d) { return "/forecast/" + d; } },
    agentLog: { scope: "date", path: function (d) { return "/forecast/" + d + "/agent-log"; } },
    weather: { scope: "date", path: function (d) { return "/forecast/" + d + "/weather"; } },
    dispatch: { scope: "risk", path: function (d, risk) { return "/forecast/" + d + "/dispatch?risk=" + risk; } },
  };

  // Что нужно каждой странице. Шапка всегда требует список выпусков.
  var NEEDS = {
    overview: ["issues", "forecast"],
    dispatch: ["issues", "site", "dispatch"],
    agent: ["issues", "agentLog"],
    backtest: ["issues", "backtest"],
    weather: ["issues", "weather"],
    model: ["issues", "model"],
    site: ["issues", "site", "forecast"],
  };

  var LOADING_TEXT = {
    issues: "Загружаем список выпусков",
    site: "Загружаем паспорт объекта",
    backtest: "Загружаем метрики бэктеста",
    model: "Загружаем описание модели",
    forecast: "Загружаем прогноз на выбранный день",
    agentLog: "Загружаем журнал агента",
    weather: "Загружаем прогоны погоды",
    dispatch: "Считаем заявку под выбранный риск",
  };

  var app = null; // корневой компонент страницы

  var PAGES = [
    ["overview", "Обзор", "прогноз на 48 ч"],
    ["dispatch", "Диспетчер", "заявка на рынок"],
    ["agent", "Агент", "как считает агент"],
    ["backtest", "Бэктест", "точность за период"],
    ["weather", "Погода", "источники погоды"],
    ["model", "Модель", "чем считаем"],
    ["site", "Объект", "турбины на карте"],
  ];

  var PAGE_KEYS = PAGES.map(function (item) { return item[0]; });

  /** Раздел берется из адреса страницы: ссылку на конкретный экран можно переслать. */
  function pageFromHash() {
    var hash = (window.location.hash || "").replace("#", "");
    return PAGE_KEYS.indexOf(hash) >= 0 ? hash : null;
  }

  function initialState() {
    return {
      page: pageFromHash(),
      issueIdx: 0,
      horizon: 48,
      hover: null,
      showWind: true,
      showActual: true,
      risk: 20,
      playing: false,
      busy: false,
      authed: false,
      data: {},
      err: {},
      pending: {},
      stamp: {},
    };
  }

  function currentIssue(s) {
    var issues = s.data.issues;
    if (!issues || !issues.length) return null;
    return issues[clamp(s.issueIdx, 0, issues.length - 1)];
  }

  function currentDate(s) {
    var issue = currentIssue(s);
    return issue ? issue.issue_date : null;
  }

  function stampFor(s, key) {
    var res = RES[key];
    if (res.scope === "global") return "global";
    var date = currentDate(s);
    if (!date) return null;
    if (res.scope === "risk") return date + "@" + (s.risk / 100).toFixed(2);
    return date;
  }

  function merge(cmp, updates) {
    var next = {};
    Object.keys(updates).forEach(function (field) {
      next[field] = Object.assign({}, cmp.state[field], updates[field]);
    });
    cmp.setState(next);
  }

  function fetchKey(cmp, key, stamp) {
    var res = RES[key];
    var s = cmp.state;
    var path = res.scope === "risk" ? res.path(currentDate(s), (s.risk / 100).toFixed(2)) : res.scope === "date" ? res.path(currentDate(s)) : res.path();
    var p = {}, e = {};
    p[key] = stamp;
    e[key] = null;
    merge(cmp, { pending: p, err: e });
    Api.get(path)
      .then(function (payload) {
        if (cmp.state.pending[key] !== stamp) return; // выбрали другой день, ответ уже не нужен
        var d = {}, st = {}, pend = {};
        d[key] = res.pick ? res.pick(payload) : payload;
        st[key] = stamp;
        pend[key] = null;
        merge(cmp, { data: d, stamp: st, pending: pend });
        queue(cmp); // список выпусков открывает дорогу запросам, привязанным к дню
      })
      .catch(function (error) {
        if (error && error.unauthorized) {
          logout(cmp);
          return;
        }
        if (cmp.state.pending[key] !== stamp) return;
        var er = {}, pend = {};
        er[key] = error;
        pend[key] = null;
        merge(cmp, { err: er, pending: pend });
      });
  }

  function ensure(cmp) {
    if (!cmp.state.authed) return;
    var s = cmp.state;
    var keys = NEEDS[pageOf(s)] || [];
    keys.forEach(function (key) {
      var stamp = stampFor(s, key);
      if (stamp === null) return; // день еще неизвестен, ждем список выпусков
      if (s.pending[key] === stamp) return;
      if (s.stamp[key] === stamp && (s.data[key] || s.err[key])) return;
      fetchKey(cmp, key, stamp);
    });
  }

  function queue(cmp) {
    window.setTimeout(function () {
      ensure(cmp);
    }, 0);
  }

  function invalidate(cmp, keys) {
    var st = {}, d = {}, e = {}, p = {};
    keys.forEach(function (k) {
      st[k] = null;
      d[k] = null;
      e[k] = null;
      p[k] = null;
    });
    merge(cmp, { stamp: st, data: d, err: e, pending: p });
    queue(cmp);
  }

  function pageOf(s) {
    return s.page || (app && app.props && app.props.startPage) || "overview";
  }

  // --- экран входа ---------------------------------------------------------

  function el(id) {
    return document.getElementById(id);
  }

  function showLogin(message) {
    var login = el("zhel-login");
    var shell = el("zhel-app");
    if (login) login.style.display = "flex";
    if (shell) shell.style.display = "none";
    var api = el("zhel-login-api");
    if (api) api.textContent = "API: " + Api.base();
    var box = el("zhel-login-error");
    if (box) {
      box.style.display = message ? "block" : "none";
      box.textContent = message || "";
    }
  }

  function showApp() {
    var login = el("zhel-login");
    var shell = el("zhel-app");
    if (login) login.style.display = "none";
    if (shell) shell.style.display = "block";
  }

  function logout(cmp) {
    Api.clearToken();
    cmp.setState({ authed: false, data: {}, err: {}, pending: {}, stamp: {} });
    showLogin("Сессия закончилась, войдите заново.");
  }

  function wireLoginForm(cmp) {
    var form = el("zhel-login-form");
    if (!form || form.dataset.wired) return;
    form.dataset.wired = "1";
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = el("zhel-login-submit");
      var email = el("zhel-login-email").value.trim();
      var password = el("zhel-login-password").value;
      button.disabled = true;
      button.textContent = "Входим…";
      Api.login(email, password)
        .then(function () {
          button.disabled = false;
          button.textContent = "Войти";
          el("zhel-login-password").value = "";
          showApp();
          cmp.setState({ authed: true }, function () {
            ensure(cmp);
          });
        })
        .catch(function (error) {
          button.disabled = false;
          button.textContent = "Войти";
          showLogin(error.message + (error.detail ? " (" + error.detail + ")" : ""));
        });
    });
  }

  // --- жизненный цикл ------------------------------------------------------

  function mount(cmp) {
    app = cmp;
    cmp.logRef = window.React.createRef();
    wireLoginForm(cmp);
    if (Api.hasToken()) {
      showApp();
      cmp.setState({ authed: true }, function () {
        ensure(cmp);
      });
    } else {
      showLogin("");
    }
  }

  function unmount(cmp) {
    window.clearInterval(cmp.replayTimer);
    window.clearTimeout(cmp.riskTimer);
  }

  // --- разбор ответа прогноза ---------------------------------------------

  /** Сворачивает почасовые строки по турбинам в ряд по станции. */
  function stationHours(forecast) {
    var byTime = {};
    var order = [];
    (forecast.hours || []).forEach(function (h) {
      var row = byTime[h.valid_time_utc];
      if (!row) {
        row = {
          lead: h.lead_h,
          local: h.valid_time_local,
          utc: h.valid_time_utc,
          n: 0,
          p10: 0,
          p50: 0,
          p90: 0,
          mw: 0,
          wind: 0,
          temp: 0,
          actSum: 0,
          actN: 0,
          flags: {},
          source: h.source,
          run: h.run_init_utc,
        };
        byTime[h.valid_time_utc] = row;
        order.push(row);
      }
      row.n += 1;
      row.p10 += h.p10;
      row.p50 += h.p50;
      row.p90 += h.p90;
      row.mw += h.p50_mw;
      row.wind += h.wind_ms;
      row.temp += h.temp_c;
      if (h.actual !== null && h.actual !== undefined) {
        row.actSum += h.actual;
        row.actN += 1;
      }
      (h.flags || []).forEach(function (f) {
        row.flags[f] = (row.flags[f] || 0) + 1;
      });
    });
    return order
      .sort(function (a, b) { return a.lead - b.lead; })
      .map(function (r) {
        return {
          lead: r.lead,
          local: r.local,
          utc: r.utc,
          p10: r.p10 / r.n,
          p50: r.p50 / r.n,
          p90: r.p90 / r.n,
          mw: r.mw,
          wind: r.wind / r.n,
          temp: r.temp / r.n,
          actual: r.actN ? r.actSum / r.actN : null,
          flags: Object.keys(r.flags),
          source: r.source,
          run: r.run,
        };
      });
  }

  function turbineNames(forecast) {
    var seen = {};
    (forecast.hours || []).forEach(function (h) {
      seen[h.turbine] = true;
    });
    return Object.keys(seen).sort();
  }

  /** Установленная мощность станции: из паспорта объекта, иначе из самого прогноза. */
  function capacityOf(s) {
    if (s.data.site) return s.data.site.capacity_mw;
    var forecast = s.data.forecast;
    if (!forecast || !forecast.hours || !forecast.hours.length) return 0;
    var rated = 0;
    forecast.hours.forEach(function (h) {
      if (h.p50 > 0.01) rated = Math.max(rated, h.p50_mw / h.p50);
    });
    return rated * turbineNames(forecast).length;
  }

  // --- сборка страниц ------------------------------------------------------

  var FLAG_LABEL = {
    cut_out_risk: "Отсечка по ветру выше 25 м/с",
    icing_risk: "Риск обледенения",
    ramp: "Резкие изменения мощности",
    degraded: "Запасной источник погоды",
    source_spread: "Расхождение источников погоды",
  };

  function buildOverview(s, cmp) {
    var forecast = s.data.forecast;
    if (!forecast) return { kpis: [], cells: [], xTicks: [], yTicks: [], checks: [], wx: [], cols: [], hv: { has: false }, tb: {} };
    var rows = stationHours(forecast);
    var hz = Math.min(s.horizon, rows.length);
    var view = rows.slice(0, hz);
    var p50 = view.map(function (r) { return r.p50; });
    var p10 = view.map(function (r) { return r.p10; });
    var p90 = view.map(function (r) { return r.p90; });
    var winds = view.map(function (r) { return r.wind; });
    var temps = view.map(function (r) { return r.temp; });
    var hasActual = view.some(function (r) { return r.actual !== null; });
    var windMax = Math.max(5, Math.ceil(Math.max.apply(null, winds.concat([0])) / 5) * 5);
    var kpi = forecast.kpi;
    var issue = forecast.issue;
    var hover = s.hover !== null && s.hover < hz ? s.hover : null;
    var hoverLeft = hover === null ? 0 : (hover / Math.max(1, hz - 1)) * 100;
    var tipRow = view[hover === null ? 0 : hover];

    var flagCount = function (name) {
      return view.filter(function (r) { return r.flags.indexOf(name) >= 0; }).length;
    };
    var check = function (name, value, ok, status) {
      return { name: name, val: value, st: status, color: ok ? LIME : AMBER };
    };

    var cells = view.map(function (r) {
      return {
        t: hhmm(r.local),
        d: isMidnight(r.local) ? ddmm(r.local) : "",
        v: pct(r.p50),
        band: pct(r.p10) + "–" + pct(r.p90),
        bg: "oklch(0.6 0.16 135 / " + (0.04 + r.p50 * 0.34).toFixed(3) + ")",
        bgStrong: "oklch(0.6 0.16 135 / " + (0.08 + r.p50 * 0.85).toFixed(3) + ")",
      };
    });

    var xTicks = [];
    for (var k = 0; k < hz; k += hz > 24 ? 6 : 3) {
      xTicks.push({ left: (k / Math.max(1, hz - 1)) * 100 + "%", t: hhmm(view[k].local), d: isMidnight(view[k].local) ? ddmm(view[k].local) : "" });
    }

    // Текст собирается из KPI ответа, чтобы не расходиться с плитками выше.
    var plain =
      "Больше всего энергии " + full(kpi.peak_hour_local) + " — до " + num(kpi.peak_mw, 2) + " МВт. " +
      (kpi.hours_below_10_pct
        ? "Ниже 10% мощности станция проработает " + kpi.hours_below_10_pct + " ч из 48. "
        : "Длительных периодов штиля в прогнозе нет. ") +
      (issue.degraded ? "Выпуск построен на запасном источнике погоды, к числам нужно относиться осторожнее." : "Выпуск построен на основном источнике погоды.");

    return {
      title: "Прогноз на " + ddmm(view[0].local) + (hz > 24 ? " – " + ddmm(view[hz - 1].local) : ""),
      subtitle: "Выпуск " + full(issue.issue_time_utc) + " UTC · " + hz + " ч по часам · версия " + issue.version,
      version: issue.version,
      meanP: Math.round(kpi.mean_load_pct),
      summary: forecast.summary,
      plain: plain,
      kpis: [
        { label: "Средняя загрузка", value: num(kpi.mean_load_pct), unit: "%", sub: "от установленной мощности, 48 ч" },
        { label: "Пиковый час", value: num(kpi.peak_mw, 2), unit: "МВт", sub: full(kpi.peak_hour_local) },
        { label: "Часы ниже 10%", value: kpi.hours_below_10_pct, unit: "ч", sub: "из 48 по данным выпуска" },
        { label: "Ожидаемая ошибка", value: num(kpi.expected_error_pct), unit: "%", sub: "nMAE по бэктесту" },
      ],
      cells: cells,
      xTicks: xTicks,
      yTicks: [0, 25, 50, 75, 100].map(function (v) {
        return { pos: v + "%", l: v + "%", w: Math.round((v / 100) * windMax) + "" };
      }),
      band: bandPath(p10, p90, 1),
      p50: linePath(p50, 1),
      act: hasActual && s.showActual ? linePath(view.map(function (r) { return r.actual === null ? 0 : r.actual; }), 1) : "",
      wind: linePath(winds, windMax),
      windOp: s.showWind ? 1 : 0,
      hasActual: hasActual,
      actOp: s.showActual && hasActual ? 0.85 : 0,
      h24bg: s.horizon === 24 ? "rgba(20,24,22,0.1)" : "transparent",
      h48bg: s.horizon === 48 ? "rgba(20,24,22,0.1)" : "transparent",
      windBg: s.showWind ? "oklch(0.6 0.16 215 / 0.14)" : "transparent",
      actBg: s.showActual ? "rgba(20,24,22,0.08)" : "transparent",
      cols: view.map(function (_, i) {
        return {
          on: function () {
            cmp.setState({ hover: i });
          },
        };
      }),
      hv:
        hover === null
          ? { has: false }
          : {
              has: true,
              left: hoverLeft + "%",
              tx: hoverLeft > 65 ? "translateX(calc(-100% - 12px))" : "translateX(12px)",
              time: full(view[hover].local),
              lead: view[hover].lead,
              p50: pct(view[hover].p50),
              band: pct(view[hover].p10) + "–" + pct(view[hover].p90) + "%",
              act: view[hover].actual === null ? "нет данных" : pct(view[hover].actual) + "%",
              wind: num(view[hover].wind),
              temp: num(view[hover].temp),
            },
      checks: [
        check("Расхождение источников", flagCount("source_spread") + " ч с флагом", flagCount("source_spread") === 0, flagCount("source_spread") ? "внимание" : "норма"),
        check("Резкие рампы", flagCount("ramp") + " ч с флагом", flagCount("ramp") === 0, flagCount("ramp") ? "внимание" : "норма"),
        check("Отсечка по ветру", "макс " + num(Math.max.apply(null, winds)) + " м/с", flagCount("cut_out_risk") === 0, flagCount("cut_out_risk") ? "риск" : "нет"),
        check("Обледенение", flagCount("icing_risk") + " ч с флагом", flagCount("icing_risk") === 0, flagCount("icing_risk") ? "риск" : "нет"),
      ],
      wx: [
        { l: "Ветер, средний", v: num(mean(winds)) + " м/с", s: "макс " + num(Math.max.apply(null, winds)) + " м/с" },
        { l: "Температура", v: num(Math.min.apply(null, temps), 0) + "…" + num(Math.max.apply(null, temps), 0) + " °C", s: "мин … макс" },
        { l: "Часов с флагами", v: issue.flagged_hours + "", s: "из " + rows.length * turbineNames(forecast).length + " строк выпуска" },
        { l: "Источник погоды", v: issue.source, s: "прогон " + full(view[0].run) + " UTC" },
      ],
      tb: {
        time: full(tipRow.local) + " · " + (hover === null ? "первый час" : "выбранный час"),
        wind: num(tipRow.wind),
        p: pct(tipRow.p50),
      },
      hero: view,
    };
  }

  function buildDispatch(s) {
    var dispatch = s.data.dispatch;
    if (!dispatch) return { hours: [], kpis: [], q: s.risk, qLabel: s.risk + "%", day: "—" };
    var cap = capacityOf(s) || Math.max.apply(null, dispatch.hours.map(function (h) { return h.p90_mw; })) || 1;
    var hours = dispatch.hours.map(function (h, i) {
      var gap = Math.max(0, h.bid_mw - h.p10_mw);
      return {
        t: hhmm(h.valid_time_local),
        h: i % 3 === 0 ? pad(i) : "",
        lo: (h.p10_mw / cap) * 100 + "%",
        band: ((h.p90_mw - h.p10_mw) / cap) * 100 + "%",
        bid: (h.bid_mw / cap) * 100 + "%",
        mid: (h.p50_mw / cap) * 100 + "%",
        p50: num(h.p50_mw, 2),
        range: num(h.p10_mw, 2) + "–" + num(h.p90_mw, 2),
        bidMw: num(h.bid_mw, 2),
        gap: num(gap, 2),
        gapC: gap > 0.15 * cap ? "oklch(0.55 0.16 40)" : GREY,
      };
    });
    var kpi = dispatch.kpi;
    return {
      day: ddmm(dispatch.hours.length ? dispatch.hours[0].valid_time_local : ""),
      q: s.risk,
      qLabel: s.risk + "%",
      hours: hours,
      kpis: [
        { label: "Заявка на сутки", value: num(kpi.day_bid_mwh), unit: "МВт·ч", sub: "сумма 24 часов при риске " + s.risk + "%" },
        { label: "Ожидаемая выработка", value: num(kpi.expected_mwh), unit: "МВт·ч", sub: "запас " + num(kpi.expected_mwh - kpi.day_bid_mwh) + " МВт·ч" },
        { label: "Часы недобора", value: Math.round(kpi.shortfall_hours_share * 100), unit: "%", sub: "доля часов, где факт ниже заявки" },
        { label: "Средний недобор", value: num(kpi.mean_shortfall_mwh, 2), unit: "МВт·ч", sub: "объем под штраф" },
      ],
    };
  }

  var STEP_META = {
    fetch_weather: ["Погода", "Прогнозы NWP по координатам, только прогоны, доступные на момент выпуска"],
    prepare: ["Подготовка", "Очистка, приведение к часу, признаки"],
    run_model: ["Модель", "Квантильный прогноз P10 / P50 / P90"],
    forecast: ["Прогноз", "48 часов по каждой турбине, публикация версии"],
    analyze: ["Анализ", "Флаги рисков: рампы, отсечка, расхождение источников"],
    recompute_on_update: ["Перерасчет", "Новый прогон или найденная проблема запускают цикл заново"],
  };
  var STEP_ORDER = ["fetch_weather", "prepare", "run_model", "forecast", "analyze", "recompute_on_update"];
  var LEVEL_COLOR = { TOOL: CYAN, THINK: VIOLET, OK: LIME, WARN: AMBER };

  function buildAgent(s) {
    var log = s.data.agentLog;
    var issue = currentIssue(s);
    if (!log) return { stages: [], logs: [], triggers: [], tools: [], asOf: "—", progress: "" };
    var byStep = {};
    log.forEach(function (d) {
      (byStep[d.step] = byStep[d.step] || []).push(d);
    });
    var stages = STEP_ORDER.map(function (step, i) {
      var meta = STEP_META[step];
      var rows = byStep[step] || [];
      var warned = rows.some(function (r) { return r.level === "WARN"; });
      var status = rows.length ? (warned ? "внимание" : "выполнен") : "не потребовался";
      var color = rows.length ? (warned ? AMBER : LIME) : GREY;
      return {
        num: pad(i + 1),
        name: meta[0],
        desc: meta[1],
        st: status,
        color: color,
        chipBg: warned ? "oklch(0.6 0.16 75 / 0.12)" : rows.length ? "oklch(0.6 0.16 135 / 0.1)" : "rgba(20,24,22,0.05)",
        border: warned ? "oklch(0.6 0.16 75 / 0.5)" : "rgba(20,24,22,0.07)",
      };
    });
    var logs = log.map(function (d) {
      var color = LEVEL_COLOR[d.level] || GREY;
      return {
        time: hhmmss(d.as_of_utc),
        tag: d.level,
        tagColor: color,
        tagBg: color.replace(")", " / 0.12)"),
        text: d.decision + " · " + d.reason,
        color: d.level === "THINK" ? "#3A413C" : "#141816",
      };
    });
    var seen = {};
    var triggers = [];
    log.forEach(function (d) {
      if (seen[d.reason_code]) return;
      seen[d.reason_code] = true;
      triggers.push(d.reason_code + ": " + d.reason);
    });
    return {
      stages: stages,
      logs: logs,
      triggers: triggers,
      tools: issue
        ? [
            { n: "источник", d: issue.source },
            { n: "версия", d: "v" + issue.version },
            { n: "статус", d: issue.status },
            { n: "запасной источник", d: issue.degraded ? "да" : "нет" },
            { n: "часов с флагами", d: issue.flagged_hours + "" },
            { n: "сутки поставки", d: ddmmyyyy(issue.target_date) },
          ]
        : [],
      asOf: log.length ? log[0].as_of_utc : issue ? issue.issue_time_utc : "—",
      progress: log.length + " записей",
    };
  }

  function buildBacktest(s, cmp) {
    var bt = s.data.backtest;
    if (!bt) return { kpis: [], cal: [], compare: [], lead: [], xTicks: [], fcPath: "", actPath: "", selLeft: "0%", selWidth: "0%", period: "—", runs: "" };
    var days = bt.by_day || [];
    var leads = bt.by_lead || [];
    var nmae = days.map(function (d) { return d.nmae_pct; });
    var bias = days.map(function (d) { return d.bias_pct; });
    var lo = Math.min(0, Math.min.apply(null, bias.concat([0])));
    var hi = Math.max.apply(null, nmae.concat(bias).concat([1]));
    var ourNmae = leads.length ? mean(leads.map(function (l) { return l.nmae_pct; })) : bt.nmae_d1_pct;
    var compare = (bt.baselines || []).map(function (b) { return [b.name, b.nmae_pct]; });
    compare.push(["Наш прогноз P50, 48 ч", ourNmae]);
    var cmax = Math.max.apply(null, compare.map(function (c) { return c[1]; }).concat([1]));
    var lmax = Math.max.apply(null, leads.map(function (l) { return l.nmae_pct; }).concat([1]));
    var selected = currentDate(s);
    var calColor = function (v) {
      return v < 9 ? "oklch(0.6 0.16 135 / 0.32)" : v < 13 ? "oklch(0.6 0.16 135 / 0.16)" : v < 17 ? "oklch(0.6 0.16 75 / 0.22)" : "oklch(0.6 0.18 25 / 0.28)";
    };
    var selIdx = days.findIndex(function (d) { return d.issue_date === selected; });
    var ticks = [];
    var step = Math.max(1, Math.floor(days.length / 4));
    for (var i = 0; i < days.length; i += step) {
      ticks.push({ left: (i / Math.max(1, days.length - 1)) * 100 + "%", t: ddmm(days[i].issue_date) });
    }
    return {
      period: ddmm(bt.period_start) + " – " + ddmmyyyy(bt.period_end),
      runs: days.length + "",
      kpis: [
        { label: "nMAE, D+1", value: num(bt.nmae_d1_pct), unit: "%", sub: "часы +1…+24" },
        { label: "nMAE, D+2", value: num(bt.nmae_d2_pct), unit: "%", sub: "часы +25…+48" },
        { label: "nRMSE, 48 ч", value: num(bt.nrmse_48_pct), unit: "%", sub: "штраф за крупные промахи" },
        { label: "Skill vs персистентность", value: num(bt.skill_vs_persistence_pct), unit: "%", sub: "снижение ошибки" },
        { label: "Покрытие P10–P90", value: num(bt.coverage_p10_p90_pct), unit: "%", sub: "доля фактов внутри интервала" },
      ],
      fcPath: linePath(nmae, hi, lo),
      actPath: linePath(bias, hi, lo),
      selLeft: selIdx < 0 ? "0%" : (selIdx / Math.max(1, days.length)) * 100 + "%",
      selWidth: selIdx < 0 ? "0%" : 100 / Math.max(1, days.length) + "%",
      xTicks: ticks,
      cal: days.map(function (d, i) {
        return {
          d: ddmm(d.issue_date),
          v: num(d.nmae_pct),
          bg: calColor(d.nmae_pct),
          border: d.issue_date === selected ? "#141816" : "transparent",
          on: function () {
            selectIssueByDate(cmp, d.issue_date, i);
          },
        };
      }),
      compare: compare.map(function (c, i) {
        var ours = i === compare.length - 1;
        return {
          n: c[0],
          v: num(c[1]),
          w: (c[1] / cmax) * 100 + "%",
          c: ours ? LIME : "rgba(20,24,22,0.22)",
          tc: ours ? "#141816" : "#4A524D",
        };
      }),
      lead: leads.map(function (l) {
        return { h: (l.nmae_pct / lmax) * 100 + "%", c: l.lead_h <= 24 ? "oklch(0.6 0.16 135 / 0.55)" : "oklch(0.6 0.16 215 / 0.45)" };
      }),
    };
  }

  var RUN_STATUS = {
    used: ["используется", LIME],
    stale: ["доступен, старее", "#4A524D"],
    after_issue: ["опубликован после выпуска", RED],
  };

  function buildWeather(s) {
    var wx = s.data.weather;
    if (!wx) return { count: "", delay: "", runs: [], srcs: [], yTicks: [], meta: [], blendPath: "", request: "", range: "—", spread: "—", tUtc: "—", run: "—" };
    var models = wx.models || [];
    var ensembleValues = wx.ensemble_wind_ms || [];
    var all = ensembleValues.slice();
    models.forEach(function (m) {
      all = all.concat(m.wind_ms || []);
    });
    var wmax = Math.max(5, Math.ceil(Math.max.apply(null, all.concat([0])) / 5) * 5);
    var date = currentDate(s);
    var delays = (wx.runs || []).map(function (r) {
      return (Date.parse(r.available_at_utc) - Date.parse(r.run_init_utc)) / 3600000;
    });
    return {
      count: models.length + " " + plural(models.length, "модель", "модели", "моделей"),
      delay: delays.length ? "Задержка публикации прогона " + num(mean(delays)) + " ч" : "",
      tUtc: full(wx.issue_time_utc) + " UTC",
      spread: num(wx.spread_ms) + " м/с",
      range: ensembleValues.length + " ч горизонта",
      run: wx.runs && wx.runs.length ? full(wx.runs[0].run_init_utc) : "—",
      runs: (wx.runs || []).map(function (r) {
        var meta = RUN_STATUS[r.status] || [r.status, GREY];
        var used = r.status === "used";
        return {
          l: r.source + " · " + full(r.run_init_utc) + " UTC",
          st: meta[0] + " · +" + r.lead_from_h + "…+" + r.lead_to_h + " ч",
          c: meta[1],
          border: used ? "oklch(0.6 0.16 135 / 0.6)" : "rgba(20,24,22,0.07)",
          bg: used ? "oklch(0.6 0.16 135 / 0.08)" : "#F4F5F2",
          op: r.status === "after_issue" ? 0.55 : 1,
        };
      }),
      srcs: models.map(function (m, i) {
        return {
          name: m.name,
          color: MODEL_COLORS[i % MODEL_COLORS.length],
          path: linePath(m.wind_ms || [], wmax),
          mae: num(m.wind_mae_ms, 2) + " м/с",
          w: num(m.weight, 2),
          avg: num(mean(m.wind_ms || [])) + " м/с",
        };
      }),
      blendPath: linePath(ensembleValues, wmax),
      yTicks: [0, 0.25, 0.5, 0.75, 1].map(function (v) {
        return { pos: v * 100 + "%", l: Math.round(v * wmax) + "" };
      }),
      request: "GET " + Api.base() + "/forecast/" + date + "/weather",
      meta: [
        { l: "Прогонов в ответе", v: (wx.runs || []).length + "" },
        { l: "Моделей в ансамбле", v: models.length + "" },
        { l: "Разброс, м/с", v: num(wx.spread_ms) },
      ],
    };
  }

  function buildModel(s) {
    var md = s.data.model;
    var site = s.data.site;
    if (!md) return { imp: [], steps: [], feats: [], xTicks: [], marks: [], yTicks: [], curve: "", walkForward: "", lead: "" };
    var curve = md.power_curve || [];
    var wmax = curve.length ? curve[curve.length - 1].wind_ms : 25;
    var impMax = Math.max.apply(null, (md.features || []).map(function (f) { return f.importance; }).concat([0.01]));
    var marks = [];
    if (site) {
      marks.push({ left: (site.cut_in_ms / wmax) * 100 + "%", l: "cut-in " + num(site.cut_in_ms, 1) });
      marks.push({ left: (site.rated_ms / wmax) * 100 + "%", l: "номинал " + num(site.rated_ms, 1) });
    }
    return {
      lead:
        "Модель: " + md.name + ". Квантили " + (md.quantiles || []).join(" / ") + ". Обучение до " + ddmmyyyy(md.trained_until) +
        ", строк в обучающей выборке " + md.train_rows + ".",
      steps: [
        { n: "01 · Модель", t: md.name, d: "Имя модели приходит из ответа /api/forecast/model." },
        { n: "02 · Квантили", t: (md.quantiles || []).join(" / "), d: "Диапазон P10–P90 нужен диспетчеру не меньше, чем точка P50." },
        { n: "03 · Обучение", t: ddmmyyyy(md.trained_until), d: "Строк в обучающей выборке: " + md.train_rows + "." },
        { n: "04 · Признаки", t: (md.features || []).length + " признаков", d: "Важность признаков показана справа." },
      ],
      curve: linePath(curve.map(function (p) { return p.power_norm; }), 1),
      yTicks: [0, 25, 50, 75, 100].map(function (v) {
        return { pos: v + "%", l: v + "%" };
      }),
      xTicks: [0, 0.25, 0.5, 0.75, 1].map(function (v, i) {
        return { l: Math.round(v * wmax) + (i === 4 ? " м/с" : "") };
      }),
      marks: marks,
      imp: (md.features || []).map(function (f, i) {
        return {
          n: f.name,
          v: num(f.importance, 2),
          w: (f.importance / impMax) * 100 + "%",
          c: i < 2 ? LIME : "oklch(0.6 0.16 135 / 0.45)",
        };
      }),
      walkForward: md.walk_forward,
      feats: (md.features || []).map(function (f) { return f.name; }),
    };
  }

  function haversine(a, b) {
    var R = 6371000;
    var dLat = ((b.lat - a.lat) * Math.PI) / 180;
    var dLon = ((b.lon - a.lon) * Math.PI) / 180;
    var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos((a.lat * Math.PI) / 180) * Math.cos((b.lat * Math.PI) / 180) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return Math.round(2 * R * Math.asin(Math.sqrt(h)));
  }

  function buildSite(s) {
    var site = s.data.site;
    if (!site) return { name: "—", headline: "", markers: [], facts: [], fields: [], center: "—" };
    var turbines = site.turbines || [];
    var zoom = 16;
    var toPixels = function (lat, lon) {
      var n = Math.pow(2, zoom);
      var rad = (lat * Math.PI) / 180;
      return [((lon + 180) / 360) * n * 256, ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * n * 256];
    };
    var center = toPixels(site.nwp_point_lat, site.nwp_point_lon);
    var markers = turbines.map(function (t) {
      var p = toPixels(t.lat, t.lon);
      return {
        name: t.name,
        coord: t.lat.toFixed(5) + ", " + t.lon.toFixed(5),
        left: "calc(50% + " + (p[0] - center[0]).toFixed(1) + "px)",
        top: "calc(50% + " + (p[1] - center[1]).toFixed(1) + "px)",
        map: "https://www.google.com/maps/search/" + t.lat.toFixed(6) + ",+" + t.lon.toFixed(6),
      };
    });
    var facts = [
      { l: "Установленная мощность", v: num(site.capacity_mw) + " МВт", s: turbines.length + " турбины по " + num(turbines.length ? turbines[0].rated_mw : 0) + " МВт" },
      { l: "Высота ступицы", v: site.hub_height_m + " м", s: "на этой высоте берется прогноз ветра" },
      { l: "Точка запроса погоды", v: site.nwp_point_lat.toFixed(4) + "° N, " + site.nwp_point_lon.toFixed(4) + "° E", s: "одна точка NWP на обе турбины" },
      { l: "Рабочий диапазон ветра", v: num(site.cut_in_ms) + " … " + num(site.cut_out_ms) + " м/с", s: "номинал с " + num(site.rated_ms) + " м/с" },
      { l: "Часовой пояс", v: "UTC+" + site.local_utc_offset_hours, s: "прогоны NWP в UTC, прогноз в местном времени" },
    ];
    if (turbines.length === 2) {
      facts.push({ l: "Расстояние между турбинами", v: haversine(turbines[0], turbines[1]) + " м", s: "обе попадают в одну ячейку сетки NWP" });
    }
    return {
      name: site.name,
      headline: turbines.length + " " + plural(turbines.length, "турбина", "турбины", "турбин") + ", одна точка прогноза погоды",
      markers: markers,
      center: site.nwp_point_lat.toFixed(4) + ", " + site.nwp_point_lon.toFixed(4),
      facts: facts,
      fields: turbines.map(function (t) {
        return { k: t.name, d: t.lat.toFixed(5) + ", " + t.lon.toFixed(5) + " · " + num(t.rated_mw) + " МВт" };
      }),
    };
  }

  function buildTurbines(s) {
    var forecast = s.data.forecast;
    if (!forecast) return { items: [], range: "—", capEach: "—" };
    var names = turbineNames(forecast);
    var hz = s.horizon;
    var series = {};
    names.forEach(function (n) {
      series[n] = [];
    });
    var ratedEach = 0;
    forecast.hours.forEach(function (h) {
      if (h.lead_h <= hz) {
        series[h.turbine].push(h);
        if (h.p50 > 0.01) ratedEach = Math.max(ratedEach, h.p50_mw / h.p50);
      }
    });
    names.forEach(function (n) {
      series[n].sort(function (a, b) { return a.lead_h - b.lead_h; });
    });
    var paths = {};
    names.forEach(function (n) {
      paths[n] = linePath(series[n].map(function (h) { return h.p50; }), 1);
    });
    var first = names.length ? series[names[0]][0] : null;
    return {
      range: first ? full(first.valid_time_local) + " → " + full(series[names[0]][series[names[0]].length - 1].valid_time_local) : "—",
      capEach: num(ratedEach),
      items: names.map(function (n, i) {
        var rows = series[n];
        var other = names[(i + 1) % names.length];
        return {
          name: n,
          note: "турбина " + n + " по данным прогноза",
          color: MODEL_COLORS[i % MODEL_COLORS.length],
          path: paths[n],
          other: names.length > 1 ? paths[other] : "",
          energy: num(sum(rows.map(function (h) { return h.p50_mw; }))),
          mean: pct(mean(rows.map(function (h) { return h.p50; }))),
          rotor: rotorEl(rotorDuration(rows.length ? rows[0].wind_ms : 0) || 3, "#141816", "none", MODEL_COLORS[i % MODEL_COLORS.length]),
        };
      }),
    };
  }


  // --- действия ------------------------------------------------------------

  function setPage(cmp, page) {
    try {
      window.location.hash = page;
    } catch (e) {
      /* адресная строка недоступна, раздел просто не попадет в ссылку */
    }
    cmp.setState({ page: page, hover: null }, function () {
      ensure(cmp);
    });
  }

  function setIssueIdx(cmp, idx) {
    var issues = cmp.state.data.issues || [];
    var next = clamp(idx, 0, Math.max(0, issues.length - 1));
    if (next === cmp.state.issueIdx) return;
    cmp.setState({ issueIdx: next, hover: null }, function () {
      ensure(cmp);
    });
  }

  function selectIssueByDate(cmp, date, fallbackIdx) {
    var issues = cmp.state.data.issues || [];
    var idx = issues.findIndex(function (i) { return i.issue_date === date; });
    setIssueIdx(cmp, idx >= 0 ? idx : fallbackIdx);
    setPage(cmp, "overview");
  }

  function setRisk(cmp, value) {
    cmp.setState({ risk: value });
    window.clearTimeout(cmp.riskTimer);
    cmp.riskTimer = window.setTimeout(function () {
      ensure(cmp);
    }, 300);
  }

  function recompute(cmp) {
    var date = currentDate(cmp.state);
    if (!date || cmp.state.busy) return;
    cmp.setState({ busy: true });
    Api.post("/forecast/" + date + "/recompute")
      .then(function (payload) {
        var d = {}, st = {}, e = {};
        d.forecast = payload;
        st.forecast = date;
        e.forecast = null;
        merge(cmp, { data: d, stamp: st, err: e });
        cmp.setState({ busy: false });
        invalidate(cmp, ["agentLog", "weather", "dispatch"]);
      })
      .catch(function (error) {
        if (error && error.unauthorized) {
          logout(cmp);
          return;
        }
        var e = {};
        e.forecast = error;
        merge(cmp, { err: e });
        cmp.setState({ busy: false });
      });
  }

  function toggleReplay(cmp) {
    if (cmp.state.playing) {
      window.clearInterval(cmp.replayTimer);
      cmp.setState({ playing: false });
      return;
    }
    var issues = cmp.state.data.issues || [];
    if (!issues.length) return;
    if (cmp.state.issueIdx >= issues.length - 1) setIssueIdx(cmp, 0);
    cmp.setState({ playing: true });
    cmp.replayTimer = window.setInterval(function () {
      var next = cmp.state.issueIdx + 1;
      if (next > (cmp.state.data.issues || []).length - 1) {
        window.clearInterval(cmp.replayTimer);
        cmp.setState({ playing: false });
      } else {
        setIssueIdx(cmp, next);
      }
    }, 2400);
  }

  function downloadCsv(cmp) {
    var forecast = cmp.state.data.forecast;
    if (!forecast) return;
    var header = "valid_time_local,turbine,p10,p50,p90,p50_mw,wind_ms,temp_c,flags\n";
    var body = forecast.hours
      .map(function (h) {
        return [h.valid_time_local, h.turbine, h.p10, h.p50, h.p90, h.p50_mw, h.wind_ms, h.temp_c, (h.flags || []).join("|")].join(",");
      })
      .join("\n");
    var link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob(["﻿" + header + body], { type: "text/csv;charset=utf-8" }));
    link.download = "forecast_" + forecast.issue.issue_date + ".csv";
    link.click();
  }

  function retry(cmp) {
    var keys = NEEDS[pageOf(cmp.state)] || [];
    invalidate(cmp, keys);
  }

  // --- состояние страницы: загрузка, ошибка, пусто -------------------------

  function emptinessOf(key, value) {
    if (key === "issues") return !value.length ? "Бэкенд не вернул ни одного выпуска прогноза." : null;
    if (key === "forecast") return !value.hours || !value.hours.length ? "В выпуске нет ни одного часа прогноза." : null;
    if (key === "agentLog") return !value.length ? "Журнал агента за этот выпуск пуст." : null;
    if (key === "weather") return !(value.models || []).length ? "В ответе нет ни одной модели погоды." : null;
    if (key === "dispatch") return !(value.hours || []).length ? "В заявке нет ни одного часа." : null;
    if (key === "backtest") return !(value.by_day || []).length ? "Метрики бэктеста пока не посчитаны." : null;
    if (key === "model") return !(value.features || []).length ? "Описание модели пришло без признаков." : null;
    return null;
  }

  function pageStatus(s) {
    var keys = NEEDS[pageOf(s)] || [];
    var status = { loading: false, error: false, empty: false, loadingText: "", loadingSub: "", errorText: "", errorDetail: "", emptyText: "" };
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var error = s.err[key];
      if (error) {
        status.error = true;
        status.errorText = error.message;
        status.errorDetail = (error.code ? error.code + " · " : "") + (error.detail || "");
        return status;
      }
    }
    for (var j = 0; j < keys.length; j++) {
      var k = keys[j];
      if (!s.data[k]) {
        status.loading = true;
        status.loadingText = LOADING_TEXT[k];
        status.loadingSub = "Запрос к " + Api.base();
        return status;
      }
      var empty = emptinessOf(k, s.data[k]);
      if (empty) {
        status.empty = true;
        status.emptyText = empty;
        return status;
      }
    }
    return status;
  }

  function stubBanner(s) {
    var labels = { forecast: "прогноз", weather: "погода", dispatch: "заявка", backtest: "бэктест", model: "модель" };
    var stubbed = Object.keys(labels).filter(function (k) {
      return s.data[k] && s.data[k].data_source === "stub";
    });
    if (!stubbed.length) return { show: false, text: "" };
    return {
      show: true,
      text:
        "Бэкенд отдает эти данные с пометкой data_source=stub: " +
        stubbed.map(function (k) { return labels[k]; }).join(", ") +
        ". Числа синтетические и нужны для проверки сценария, а не для принятия решений.",
    };
  }

  // --- сборка всех значений шаблона ---------------------------------------

  function view(cmp) {
    var s = cmp.state;
    var page = pageOf(s);
    var issues = s.data.issues || [];
    var issue = currentIssue(s);
    var status = pageStatus(s);
    var ready = !status.loading && !status.error && !status.empty;

    var ov = buildOverview(s, cmp);
    var heroWind = ov.hero && ov.hero.length ? ov.hero[0].wind : 0;
    var hoverWind = ov.hero && ov.hero.length ? ov.hero[s.hover === null ? 0 : Math.min(s.hover, ov.hero.length - 1)].wind : 0;
    var heroDur = rotorDuration(hoverWind);

    return {
      // шапка и навигация
      nav: PAGES.map(function (item, i) {
        return {
          label: item[1],
          desc: item[2],
          num: pad(i + 1),
          onClick: function () {
            setPage(cmp, item[0]);
          },
          bg: page === item[0] ? "rgba(20,24,22,0.06)" : "transparent",
          color: page === item[0] ? "#141816" : GREY,
          dot: page === item[0] ? LIME : "transparent",
        };
      }),
      issue: s.issueIdx,
      issueMax: Math.max(0, issues.length - 1),
      running: status.loading || s.busy,
      notRunning: !(status.loading || s.busy),
      top: {
        issueLabel: issue ? ddmmyyyy(issue.issue_date) + " " + hhmm(issue.issue_time_utc) + " UTC" : "—",
        horizonLabel: issue ? "сутки " + ddmmyyyy(issue.target_date) : "—",
        status: s.busy ? "Пересчитываем выпуск" : status.loading ? "Загружаем данные" : status.error ? "Ошибка запроса" : issue ? "Выпуск " + issue.status + " · v" + issue.version : "—",
        firstLabel: issues.length ? ddmm(issues[0].issue_date) : "—",
        lastLabel: issues.length ? ddmm(issues[issues.length - 1].issue_date) : "—",
      },
      prevIssue: function () {
        setIssueIdx(cmp, s.issueIdx - 1);
      },
      nextIssue: function () {
        setIssueIdx(cmp, s.issueIdx + 1);
      },
      onSlide: function (e) {
        setIssueIdx(cmp, +e.target.value);
      },
      toggleReplay: function () {
        toggleReplay(cmp);
      },
      replayLabel: s.playing ? "❚❚ Пауза" : "▶ Пройти все дни",
      replayBg: s.playing ? "oklch(0.6 0.16 75 / 0.14)" : "transparent",
      rerun: function () {
        recompute(cmp);
      },
      rerunLabel: s.busy ? "Пересчитываем…" : "Пересчитать",
      logout: function () {
        logout(cmp);
      },
      retry: function () {
        retry(cmp);
      },

      // состояния и честность данных
      st: status,
      stub: stubBanner(s),

      // страницы
      isOverview: ready && page === "overview",
      isDispatch: ready && page === "dispatch",
      isAgent: ready && page === "agent",
      isBacktest: ready && page === "backtest",
      isWeather: ready && page === "weather",
      isModel: ready && page === "model",
      isSite: ready && page === "site",

      // картинки
      heroTurbines: [
        [40, 150, 26, 0.6, 1.2],
        [62, 130, 40, 0.55, 0.9],
        [93, 140, 34, 0.7, 1.1],
        [84, 190, 14, 0.85, 0.95],
        [70, 250, -6, 0.95, 1.05],
        [53, 310, -34, 1, 1],
      ].map(function (item) {
        return {
          left: item[0] + "%",
          bottom: item[2] + "px",
          h: item[1] + "px",
          w: Math.round((item[1] * 220) / 330) + "px",
          op: item[3],
          el: turbineEl(heroDur ? +(heroDur * item[4]).toFixed(2) : 0),
        };
      }),
      rotorLogo: rotorEl(rotorDuration(heroWind) || 3, "#141816", "none", LIME),
      rotorMap: rotorEl(rotorDuration(heroWind) || 3, "#FFFFFF", "rgba(20,24,22,0.35)", LIME),

      // данные страниц
      ov: ov,
      dp: buildDispatch(s),
      ag: buildAgent(s),
      bt: buildBacktest(s, cmp),
      wx: buildWeather(s),
      md: buildModel(s),
      site: buildSite(s),
      tb: buildTurbines(s),

      // элементы управления страниц
      setH24: function () {
        cmp.setState({ horizon: 24, hover: null });
      },
      setH48: function () {
        cmp.setState({ horizon: 48, hover: null });
      },
      toggleWind: function () {
        cmp.setState({ showWind: !s.showWind });
      },
      toggleActual: function () {
        cmp.setState({ showActual: !s.showActual });
      },
      clearHover: function () {
        cmp.setState({ hover: null });
      },
      onQ: function (e) {
        setRisk(cmp, +e.target.value);
      },
      goAgent: function () {
        setPage(cmp, "agent");
      },
      goWeather: function () {
        setPage(cmp, "weather");
      },
      downloadCsv: function () {
        downloadCsv(cmp);
      },
      logRef: cmp.logRef,
    };
  }

  return { initialState: initialState, mount: mount, unmount: unmount, view: view };
})();
