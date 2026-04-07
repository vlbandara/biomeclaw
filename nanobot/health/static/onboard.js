const form = document.getElementById("onboard-form");
const steps = [...document.querySelectorAll(".step")];
const nextButton = document.getElementById("next");
const backButton = document.getElementById("back");
const submitButton = document.getElementById("submit");
const statusNode = document.getElementById("status");
const completionNode = document.getElementById("completion");
const completionActions = document.getElementById("completion-actions");

let currentStep = 0;

function parseLines(value) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function updateStep() {
  steps.forEach((step, index) => {
    step.classList.toggle("active", index === currentStep);
  });
  backButton.style.visibility = currentStep === 0 ? "hidden" : "visible";
  nextButton.style.display = currentStep === steps.length - 1 ? "none" : "inline-flex";
  submitButton.style.display = currentStep === steps.length - 1 ? "inline-flex" : "none";
}

function availableChannelLinks() {
  return {
    telegram: form.dataset.telegramUrl || "",
    whatsapp: form.dataset.whatsappUrl || "",
  };
}

function channelLabel(name) {
  return name === "telegram" ? "Telegram" : "WhatsApp";
}

function renderCompletion(preferredChannel, links) {
  completionActions.innerHTML = "";
  const boundChannel = form.dataset.boundChannel || "";
  const ordered = [];
  const primaryChannel = boundChannel || preferredChannel;

  if (primaryChannel && links[primaryChannel]) {
    ordered.push(primaryChannel);
  }
  Object.keys(links).forEach((name) => {
    if (links[name] && !ordered.includes(name) && name === preferredChannel) {
      ordered.push(name);
    }
  });

  ordered.forEach((name) => {
    const anchor = document.createElement("a");
    anchor.href = links[name];
    anchor.target = "_blank";
    anchor.rel = "noopener";
    anchor.className = `channel-link${name === "whatsapp" ? " secondary-link" : ""}`;
    anchor.textContent = primaryChannel === name ? `Back to ${channelLabel(name)}` : `Also available in ${channelLabel(name)}`;
    completionActions.appendChild(anchor);
  });

  form.hidden = true;
  completionNode.hidden = false;
}

function sectionPayload(prefix) {
  const scope = steps[prefix === "phase1" ? 0 : 1];
  const payload = {};
  const checkboxGroups = new Map();

  scope.querySelectorAll("[name]").forEach((element) => {
    const { name, type } = element;
    if (!name) {
      return;
    }
    if (type === "checkbox") {
      if (!checkboxGroups.has(name)) {
        checkboxGroups.set(name, []);
      }
      if (element.checked) {
        checkboxGroups.get(name).push(element.value);
      }
      return;
    }
    payload[name] = element.value;
  });

  checkboxGroups.forEach((values, name) => {
    if (values.length <= 1) {
      payload[name] = values.length === 1 ? values[0] : "";
      return;
    }
    payload[name] = values;
  });

  ["known_conditions", "medications", "allergies", "goals", "reminder_preferences", "medication_reminder_windows"].forEach((key) => {
    if (key in payload) {
      payload[key] = parseLines(payload[key]);
    }
  });
  if (prefix === "phase1") {
    payload.consents = checkboxGroups.get("consents") || [];
    payload.height_cm = payload.height_cm ? Number(payload.height_cm) : null;
    payload.weight_kg = payload.weight_kg ? Number(payload.weight_kg) : null;
  } else {
    payload.mood_interest = Number(payload.mood_interest || 0);
    payload.mood_down = Number(payload.mood_down || 0);
    payload.morning_check_in = (checkboxGroups.get("morning_check_in") || []).length > 0;
    payload.weekly_summary = (checkboxGroups.get("weekly_summary") || []).length > 0;
  }
  return payload;
}

nextButton.addEventListener("click", () => {
  currentStep = Math.min(currentStep + 1, steps.length - 1);
  updateStep();
});

backButton.addEventListener("click", () => {
  currentStep = Math.max(currentStep - 1, 0);
  updateStep();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusNode.textContent = "Saving your profile…";
  const invite = form.dataset.invite;
  const payload = {
    phase1: sectionPayload("phase1"),
    phase2: sectionPayload("phase2"),
  };
  const response = await fetch(`/api/onboard/${invite}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Unable to submit onboarding." }));
    statusNode.textContent = error.detail || "Something went wrong. Please try again.";
    return;
  }
  const data = await response.json();
  const links = { ...availableChannelLinks(), ...(data.channelLinks || {}) };
  statusNode.textContent = "Setup complete.";
  renderCompletion(payload.phase1.preferred_channel, links);
});

updateStep();
