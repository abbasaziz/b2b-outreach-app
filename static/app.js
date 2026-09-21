// Light polling for send progress
async function pollProgress() {
  try {
    const r = await fetch('/send/progress');
    const j = await r.json();
    const el = document.getElementById('sendStatus');
    if (!el) return;
    if (j.active) {
      el.textContent = `Sending… ${j.done}/${j.total} — current: ${j.current}`;
    } else if (j.total > 0) {
      el.textContent = `Done. Sent ${j.done} of ${j.total}.`;
    }
  } catch (e) {
    /* silent */
  }
}
setInterval(pollProgress, 3000);