/**
 * Клиент API прогноза.
 *
 * Адрес берется из window.API_BASE, который задается одной строкой в index.html.
 * Токен лежит в localStorage и уходит в заголовке Authorization во всех запросах.
 * Любой отказ превращается в ApiError с текстом на русском: пустых экранов быть не должно.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "zhel.access_token";
  var TIMEOUT_MS = 20000;

  function base() {
    return String(window.API_BASE || "/api").replace(/\/+$/, "");
  }

  function readToken() {
    try {
      return window.localStorage.getItem(TOKEN_KEY);
    } catch (e) {
      return null;
    }
  }

  function writeToken(token) {
    try {
      if (token) window.localStorage.setItem(TOKEN_KEY, token);
      else window.localStorage.removeItem(TOKEN_KEY);
    } catch (e) {
      /* приватный режим браузера: работаем в пределах одной вкладки */
    }
  }

  /** Ошибка запроса: message для человека, detail для отладки. */
  function ApiError(message, detail, status, code) {
    var err = new Error(message);
    err.name = "ApiError";
    err.detail = detail || "";
    err.status = status || 0;
    err.code = code || "";
    err.unauthorized = status === 401;
    return err;
  }

  function describeNetwork(url) {
    return ApiError(
      "Бэкенд не отвечает. Проверьте, что стек поднят (docker compose up) и что адрес API указан верно.",
      "Не удалось выполнить запрос к " + url + ". Если страница открыта как файл, браузер может блокировать запрос к другому источнику.",
      0,
      "NETWORK",
    );
  }

  function parseError(status, payload, url) {
    var envelope = payload && payload.error ? payload.error : null;
    var code = envelope && envelope.code ? envelope.code : "HTTP_" + status;
    var message = envelope && envelope.message ? envelope.message : "";
    if (status === 401) {
      // На экране входа важен текст бэкенда («неверный логин»), внутри приложения — что сессия кончилась.
      return ApiError(message || "Сессия недействительна. Войдите заново.", url, status, code);
    }
    if (status === 404) {
      return ApiError(message || "Данных за выбранный день нет.", url, status, code);
    }
    if (status >= 500) {
      return ApiError("Бэкенд ответил ошибкой. Повторите запрос или посмотрите логи backend.", (message || "") + " " + url, status, code);
    }
    return ApiError(message || "Запрос отклонен бэкендом.", url, status, code);
  }

  function request(path, options) {
    options = options || {};
    var url = base() + path;
    var headers = { Accept: "application/json" };
    var token = readToken();
    if (options.auth !== false && token) headers.Authorization = "Bearer " + token;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";

    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = window.setTimeout(function () {
      if (controller) controller.abort();
    }, TIMEOUT_MS);

    return window
      .fetch(url, {
        method: options.method || "GET",
        headers: headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller ? controller.signal : undefined,
      })
      .catch(function (err) {
        window.clearTimeout(timer);
        if (err && err.name === "AbortError") {
          throw ApiError("Бэкенд не ответил за " + TIMEOUT_MS / 1000 + " секунд.", url, 0, "TIMEOUT");
        }
        throw describeNetwork(url);
      })
      .then(function (response) {
        window.clearTimeout(timer);
        return response.text().then(function (text) {
          var payload = null;
          if (text) {
            try {
              payload = JSON.parse(text);
            } catch (e) {
              payload = null;
            }
          }
          if (!response.ok) throw parseError(response.status, payload, url);
          if (payload === null) {
            throw ApiError("Бэкенд вернул не JSON.", url + ": " + text.slice(0, 120), response.status, "BAD_PAYLOAD");
          }
          return payload;
        });
      });
  }

  window.ZhelApi = {
    TOKEN_KEY: TOKEN_KEY,
    base: base,
    hasToken: function () {
      return !!readToken();
    },
    clearToken: function () {
      writeToken(null);
    },
    login: function (email, password) {
      return request("/auth/login", { method: "POST", auth: false, body: { email: email, password: password } }).then(function (data) {
        if (!data || !data.access_token) throw ApiError("Бэкенд не вернул токен доступа.", JSON.stringify(data).slice(0, 120), 0, "NO_TOKEN");
        writeToken(data.access_token);
        return data;
      });
    },
    get: function (path) {
      return request(path);
    },
    post: function (path, body) {
      return request(path, { method: "POST", body: body === undefined ? {} : body });
    },
  };
})();
