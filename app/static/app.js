// Small shared helpers for StudyBridge pages.

async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error("Request failed: " + r.status);
  return r.json();
}

async function apiPostForm(url, formData) {
  const r = await fetch(url, { method: "POST", body: formData });
  return r.json();
}

async function apiPostJson(url, obj) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
  return r.json();
}

let _toastTimer = null;
function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function esc(s) {
  return (s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
