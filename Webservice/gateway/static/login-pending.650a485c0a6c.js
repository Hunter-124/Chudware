(() => {
  "use strict";

  const button = document.querySelector("[data-resend-verification]");
  const status = document.getElementById("resend-verification-status");
  if (!button || !status) return;

  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Sending…";

    try {
      const response = await fetch("/api/auth/resend-verification", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: button.dataset.email || "" }),
      });
      if (!response.ok) throw new Error(`resend failed: ${response.status}`);
      status.textContent = "Check your inbox. We sent a fresh verification link.";
    } catch (_error) {
      status.textContent = "We couldn't send the email. Try again in a moment.";
    } finally {
      button.disabled = false;
    }
  });
})();
