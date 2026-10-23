/* Add <script src="https://your-support-host/static/widget.js" defer></script>. */
(() => {
  "use strict";
  const script = document.currentScript;
  if (!script || document.getElementById("auto-support-launcher")) return;
  const origin = new URL(script.src, document.baseURI).origin;
  const button = document.createElement("button");
  button.id = "auto-support-launcher";
  button.type = "button";
  button.textContent = script.dataset.label || "Need a hand? ↗";
  button.setAttribute("aria-label", "Open customer support");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", "auto-support-frame");
  Object.assign(button.style, { position: "fixed", right: "22px", bottom: "22px", zIndex: "2147483000", border: "1px solid #29343b", borderRadius: "4px", background: "#242f36", color: "#fff", font: "13px Helvetica, Arial, sans-serif", padding: "14px 19px", cursor: "pointer", boxShadow: "0 4px 20px #0002" });
  const frame = document.createElement("iframe");
  frame.id = "auto-support-frame";
  frame.title = "Customer support conversation";
  frame.allow = "microphone";
  frame.referrerPolicy = "strict-origin-when-cross-origin";
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
  Object.assign(frame.style, { position: "fixed", right: "22px", bottom: "80px", zIndex: "2147483001", width: "min(390px, calc(100vw - 32px))", height: "min(660px, calc(100dvh - 110px))", maxHeight: "calc(100vh - 110px)", border: "1px solid #d8e0e5", borderRadius: "7px", background: "#fff", boxShadow: "0 12px 65px #0002", display: "none" });
  let opened = false;
  function setOpen(value) {
    opened = value;
    if (value && !frame.src) frame.src = `${origin}/widget?embedded=1&parent_origin=${encodeURIComponent(location.origin)}`;
    frame.style.display = value ? "block" : "none";
    button.setAttribute("aria-expanded", String(value));
    button.textContent = value ? "Close support ×" : script.dataset.label || "Need a hand? ↗";
    if (!value) { frame.contentWindow?.postMessage({ type: "auto-support-hidden" }, origin); button.focus(); }
  }
  button.addEventListener("click", () => setOpen(!opened));
  button.addEventListener("keydown", (event) => { if (event.key === "Escape") setOpen(false); });
  window.addEventListener("message", (event) => { if (event.origin === origin && event.source === frame.contentWindow && event.data?.type === "auto-support-close") setOpen(false); });
  document.body.append(button, frame);
})();
