/**
 * Scroll-driven story panel visibility (IntersectionObserver).
 */
(function () {
  const panels = document.querySelectorAll(".story-panel[data-story]");
  if (!panels.length || !("IntersectionObserver" in window)) {
    panels.forEach((el) => el.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
        }
      });
    },
    { root: null, rootMargin: "0px 0px -12% 0px", threshold: 0.15 },
  );

  panels.forEach((panel) => observer.observe(panel));
})();

/**
 * Signup form (unchanged API contract).
 */
const form = document.getElementById("signup-form");
const statusNode = document.getElementById("signup-status");

if (form && statusNode) {
  function detectTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (e) {
      return "UTC";
    }
  }

  function setStatus(message, isError = false) {
    statusNode.textContent = message;
    statusNode.classList.toggle("status-error", isError);
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const ref = data.errorId || data.requestId || response.headers.get("X-Request-ID") || "";
      const baseMessage = data.detail || "Request failed.";
      throw new Error(ref ? `${baseMessage} Ref: ${ref}` : baseMessage);
    }
    return data;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const name = form.querySelector('[name="name"]').value.trim();
      const timezoneField = form.querySelector('[name="timezone"]');
      const timezone = (timezoneField?.value || "").trim() || detectTimezone();
      if (timezoneField) {
        timezoneField.value = timezone;
      }
      setStatus("Creating your setup link…");
      const data = await fetchJson("/api/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, timezone }),
      });
      if (!data.setupToken) {
        throw new Error("Missing setup token.");
      }
      window.location.href = `/setup/${encodeURIComponent(data.setupToken)}`;
    } catch (error) {
      setStatus(error.message || "Unable to start onboarding.", true);
    }
  });
}
