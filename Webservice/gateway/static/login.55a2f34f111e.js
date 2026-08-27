"use strict";
(function () {
  var pathMode = { "/signin": "login", "/signup": "register", "/forgot": "forgot", "/reset": "reset" };
  var mode = pathMode[window.location.pathname] || "login";
  var status = document.getElementById("auth-status");
  function el(id) { return document.getElementById(id); }
  function safeNext() {
    var candidate = new URLSearchParams(window.location.search).get("next") || "/";
    if (!candidate.startsWith("/") || candidate.startsWith("//") || candidate.includes("\\")) return "/";
    try {
      var parsed = new URL(candidate, window.location.origin);
      return parsed.origin === window.location.origin ? parsed.pathname + parsed.search + parsed.hash : "/";
    } catch (_) { return "/"; }
  }
  function setStatus(message, error) { status.textContent = message || ""; status.classList.toggle("error", Boolean(error)); }
  async function api(path, body) {
    var response = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.detail || payload.error || "Request failed (" + response.status + ")");
    return payload;
  }
  document.querySelectorAll("[data-auth-form]").forEach(function (form) { form.hidden = form.dataset.authForm !== mode; });
  document.querySelectorAll("[data-mode-link]").forEach(function (link) { link.classList.toggle("active", link.dataset.modeLink === mode); });
  el("auth-tabs").hidden = mode === "forgot" || mode === "reset";
  el("oauth-block").hidden = mode === "forgot" || mode === "reset";
  // The OAuth links are plain anchors in the markup: the gateway has no
  // templating, so the destination the password form already honours has to be
  // stamped onto them here or a desktop sign-in could only ever finish with a
  // password. The server re-validates and signs whatever arrives.
  (function () {
    var next = safeNext();
    if (next === "/") return;
    document.querySelectorAll("#oauth-block a[href^='/api/auth/oauth/']").forEach(function (link) {
      link.href = link.getAttribute("href").split("?")[0] + "?next=" + encodeURIComponent(next);
    });
  }());

  el("login-form").addEventListener("submit", async function (event) {
    event.preventDefault(); var button = this.querySelector("button[type=submit]"); button.disabled = true; setStatus("Signing in…", false);
    try {
      var result = await api("/api/auth/login", { email: el("login-email").value, password: el("login-password").value });
      setStatus(result.status === "active" ? "Signed in. Opening your account…" : "Signed in — one more step. Taking you there…", false);
      window.location.assign(safeNext());
    } catch (error) { setStatus(error.message, true); button.disabled = false; }
  });
  el("register-form").addEventListener("submit", async function (event) {
    event.preventDefault(); var button = this.querySelector("button[type=submit]"); button.disabled = true; setStatus("Creating your account…", false);
    try {
      await api("/api/auth/register", { email: el("register-email").value, password: el("register-password").value });
      this.reset(); setStatus("Check your email for the verification link. It expires in 24 hours.", false);
    } catch (error) { setStatus(error.message, true); }
    finally { button.disabled = false; }
  });
  el("forgot-form").addEventListener("submit", async function (event) {
    event.preventDefault(); var button = this.querySelector("button[type=submit]"); button.disabled = true; setStatus("Sending reset instructions…", false);
    try { await api("/api/auth/forgot", { email: el("forgot-email").value }); this.reset(); setStatus("If that account exists, reset instructions are on the way.", false); }
    catch (error) { setStatus(error.message, true); }
    finally { button.disabled = false; }
  });
  el("reset-form").addEventListener("submit", async function (event) {
    event.preventDefault(); var token = new URLSearchParams(window.location.search).get("token") || ""; var button = this.querySelector("button[type=submit]"); button.disabled = true; setStatus("Updating your password…", false);
    try { await api("/api/auth/reset", { token: token, password: el("reset-password").value }); setStatus("Password updated. Opening your account…", false); window.location.assign("/"); }
    catch (error) { setStatus(error.message, true); button.disabled = false; }
  });
}());
