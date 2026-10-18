/* Напоминание показывается при каждом входе и не зависит от загрузки карты и Telegram SDK.
   Галочку согласия просим только до первого принятия текущей редакции условий. */
(() => {
  "use strict";
  const box = document.getElementById("consent");
  const form = document.getElementById("consent-form");
  const accept = document.getElementById("consent-accept");
  const submit = document.getElementById("consent-submit");
  const title = document.getElementById("consent-title");
  const reopen = document.getElementById("purpose-open");
  const check = document.getElementById("consent-check");
  const note = document.getElementById("consent-note");
  const version = (window.APP_CONFIG || {}).termsVersion || "1";
  const accepted = () => { try { return localStorage.getItem("termsAccepted") === version; } catch { return false; } };
  const background = Array.from(document.body.children).filter(el =>
    el !== box && el.tagName !== "SCRIPT" && !el.inert);
  let previousFocus;

  function show() {
    previousFocus = document.activeElement;
    const known = accepted();
    check.hidden = known;
    note.hidden = !known;
    accept.required = !known;
    accept.checked = known;
    submit.disabled = !known;
    submit.textContent = known ? "Понятно, открыть карту" : "Принять и открыть карту";
    box.hidden = false;
    document.body.classList.add("consent-open");
    background.forEach(el => { el.inert = true; });
    title.focus();
  }

  accept.addEventListener("change", () => { submit.disabled = !accept.checked; });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!accept.checked) return;
    box.hidden = true;
    document.body.classList.remove("consent-open");
    background.forEach(el => { el.inert = false; });
    // Сохранение согласия не отменяет уведомление при следующем входе.
    try { localStorage.setItem("termsAccepted", version); } catch { /* приватный режим */ }
    (previousFocus && previousFocus !== document.body ? previousFocus : reopen).focus();
  });
  box.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const controls = Array.from(box.querySelectorAll("a[href], summary, input, button"))
      .filter(el => !el.disabled && el.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === title)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  reopen.addEventListener("click", show);
  window.addEventListener("pageshow", event => { if (event.persisted) show(); });
  show();
})();
