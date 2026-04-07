const form = document.getElementById("setup-form");
const steps = [...document.querySelectorAll(".step")];
const nextButton = document.getElementById("next");
const backButton = document.getElementById("back");
const activateButton = document.getElementById("activate");
const statusNode = document.getElementById("status");
const completionNode = document.getElementById("completion");
const completionActions = document.getElementById("completion-actions");
const providerSummary = document.getElementById("provider-summary");
const providerHint = document.getElementById("provider-hint");
const telegramSummary = document.getElementById("telegram-summary");
const whatsappSummary = document.getElementById("whatsapp-summary");
const whatsappQrNode = document.getElementById("whatsapp-qr");
const finishSummary = document.getElementById("finish-summary");
const connectTelegramButton = document.getElementById("connect-telegram");

let currentStep = 0;
let setupState = null;
let whatsappPoll = null;
const providerLabels = {
  minimax: "MiniMax",
  openrouter: "OpenRouter",
};
const providerHints = {
  minimax: "Use your MiniMax key from your MiniMax account.",
  openrouter: "Use your OpenRouter key from your OpenRouter account.",
};

function parseLines(value) {
  return (value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function selectedProvider() {
  return field("provider").value || "minimax";
}

function providerLabel(provider) {
  return providerLabels[provider] || "Your AI provider";
}

function refreshProviderHint() {
  const provider = selectedProvider();
  providerHint.textContent = providerHints[provider] || "Paste the API key for the AI service you want to use.";
}

function qrImageUrl(payload) {
  return `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(payload)}`;
}

function updateStep() {
  steps.forEach((step, index) => {
    step.classList.toggle("active", index === currentStep);
  });
  backButton.style.visibility = currentStep === 0 ? "hidden" : "visible";
  nextButton.style.display = currentStep === steps.length - 1 ? "none" : "inline-flex";
  activateButton.style.display = currentStep === steps.length - 1 ? "inline-flex" : "none";
}

function setStatus(message, isError = false) {
  statusNode.textContent = message;
  statusNode.classList.toggle("status-error", isError);
}

function field(name) {
  return form.querySelector(`[name="${name}"]`);
}

function setFieldValue(name, value) {
  const element = field(name);
  if (!element) {
    return;
  }
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
    return;
  }
  if (Array.isArray(value)) {
    element.value = value.join("\n");
    return;
  }
  element.value = value ?? "";
}

function fillProfile(profile) {
  if (!profile || !profile.phase1) {
    return;
  }
  Object.entries(profile.phase1).forEach(([name, value]) => {
    if (name === "consents") {
      form.querySelectorAll('input[name="consents"]').forEach((checkbox) => {
        checkbox.checked = (value || []).includes(checkbox.value);
      });
      return;
    }
    setFieldValue(name, value);
  });
  Object.entries(profile.phase2 || {}).forEach(([name, value]) => {
    if (name === "morning_check_in" || name === "weekly_summary") {
      const checkbox = field(name);
      if (checkbox) {
        checkbox.checked = Boolean(value);
      }
      return;
    }
    setFieldValue(name, value);
  });
}

function applyProviderSelection(provider) {
  const select = field("provider");
  if (select && provider) {
    select.value = provider;
  }
  refreshProviderHint();
}

function renderCompletion(links) {
  completionActions.innerHTML = "";
  Object.entries(links || {}).forEach(([name, url]) => {
    if (!url) {
      return;
    }
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener";
    anchor.className = `channel-link${name === "whatsapp" ? " secondary-link" : ""}`;
    anchor.textContent = name === "telegram" ? "Open Telegram" : "Open WhatsApp";
    completionActions.appendChild(anchor);
  });
  form.hidden = true;
  completionNode.hidden = false;
}

function profilePayload() {
  const checkboxGroups = new Map();
  form.querySelectorAll("[name]").forEach((element) => {
    if (element.type !== "checkbox") {
      return;
    }
    if (!checkboxGroups.has(element.name)) {
      checkboxGroups.set(element.name, []);
    }
    if (element.checked) {
      checkboxGroups.get(element.name).push(element.value);
    }
  });

  return {
    phase1: {
      full_name: field("full_name").value,
      email: field("email").value,
      phone: field("phone").value,
      timezone: field("timezone").value,
      language: field("language").value,
      preferred_channel: field("preferred_channel").value,
      age_range: field("age_range").value,
      sex: field("sex").value,
      gender: field("gender").value,
      height_cm: field("height_cm").value ? Number(field("height_cm").value) : null,
      weight_kg: field("weight_kg").value ? Number(field("weight_kg").value) : null,
      known_conditions: parseLines(field("known_conditions").value),
      medications: parseLines(field("medications").value),
      allergies: parseLines(field("allergies").value),
      wake_time: field("wake_time").value,
      sleep_time: field("sleep_time").value,
      consents: checkboxGroups.get("consents") || [],
    },
    phase2: {
      mood_interest: Number(field("mood_interest").value || 0),
      mood_down: Number(field("mood_down").value || 0),
      activity_level: field("activity_level").value,
      nutrition_quality: field("nutrition_quality").value,
      sleep_quality: field("sleep_quality").value,
      stress_level: field("stress_level").value,
      goals: parseLines(field("goals").value),
      current_concerns: field("current_concerns").value,
      reminder_preferences: parseLines(field("reminder_preferences").value),
      medication_reminder_windows: parseLines(field("medication_reminder_windows").value),
      morning_check_in: field("morning_check_in").checked,
      weekly_summary: field("weekly_summary").checked,
    },
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Request failed.");
  }
  return data;
}

async function refreshStatus() {
  const token = form.dataset.setupToken;
  setupState = await fetchJson(`/api/setup/${token}/status`);

  const provider = setupState.provider || {};
  applyProviderSelection(provider.provider || selectedProvider());
  providerSummary.textContent = provider.validated_at
    ? `${providerLabel(provider.provider)} is ready. ${provider.api_key_masked || ""}`.trim()
    : `${providerLabel(selectedProvider())} is not connected yet.`;

  const telegram = (setupState.channels || {}).telegram || {};
  telegramSummary.textContent = telegram.connected
    ? `Connected as @${telegram.bot_username || "your_bot"}.`
    : "Telegram is not connected yet.";
  if (telegram.connected && telegram.bot_username) {
    setFieldValue("preferred_channel", "telegram");
  }

  const whatsapp = (setupState.channels || {}).whatsapp || {};
  whatsappSummary.textContent = whatsapp.connected
    ? "WhatsApp is connected."
    : whatsapp.status === "qr_ready"
      ? "Scan the QR code with WhatsApp to finish connecting."
      : "Waiting for WhatsApp connection.";
  if (whatsapp.connected && whatsapp.phone) {
    whatsappSummary.textContent = `WhatsApp is connected on ${whatsapp.phone}.`;
  }

  const qr = whatsapp.qr || "";
  if (qr) {
    whatsappQrNode.innerHTML = `<img src="${qrImageUrl(qr)}" alt="WhatsApp QR code" />`;
  } else {
    whatsappQrNode.textContent = whatsapp.connected ? "WhatsApp connected." : "Waiting for QR code…";
  }

  fillProfile(setupState.profile);
  finishSummary.textContent = setupState.activationReady
    ? "Everything looks ready. Turn on your assistant when you’re ready."
    : "You can activate once your AI key is checked, at least one chat app is connected, and your profile is saved.";

  if (setupState.state === "active") {
    renderCompletion(setupState.channelLinks || {});
  }
}

async function saveProvider() {
  const provider = selectedProvider();
  const apiKey = field("api_key").value.trim();
  if (!apiKey) {
    throw new Error(`Paste your ${providerLabel(provider)} API key first.`);
  }
  const token = form.dataset.setupToken;
  setStatus(`Checking your ${providerLabel(provider)} API key…`);
  await fetchJson(`/api/setup/${token}/provider`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, api_key: apiKey }),
  });
  await refreshStatus();
  setStatus(`${providerLabel(provider)} is connected.`);
}

async function saveTelegram() {
  const botToken = field("telegram_bot_token").value.trim();
  if (!botToken) {
    throw new Error("Paste your Telegram bot token first.");
  }
  const token = form.dataset.setupToken;
  setStatus("Checking your Telegram bot token…");
  await fetchJson(`/api/setup/${token}/channels/telegram`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bot_token: botToken }),
  });
  await refreshStatus();
  setStatus("Telegram is connected.");
}

async function saveProfile() {
  const token = form.dataset.setupToken;
  setStatus("Saving your profile…");
  await fetchJson(`/api/setup/${token}/profile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profilePayload()),
  });
  await refreshStatus();
  setStatus("Your profile is saved.");
}

async function activateSetup() {
  const token = form.dataset.setupToken;
  setStatus("Turning on your assistant…");
  const data = await fetchJson(`/api/setup/${token}/activate`, {
    method: "POST",
  });
  setStatus("Your assistant is ready.");
  renderCompletion(data.channelLinks || {});
}

async function loadWhatsAppStatus() {
  const token = form.dataset.setupToken;
  try {
    const [status, qr] = await Promise.all([
      fetchJson(`/api/setup/${token}/channels/whatsapp/status`),
      fetchJson(`/api/setup/${token}/channels/whatsapp/qr`),
    ]);
    if (setupState) {
      setupState.channels = setupState.channels || {};
      setupState.channels.whatsapp = {
        ...(setupState.channels.whatsapp || {}),
        ...status,
        connected: (status.status || "") === "connected",
      };
    }
    if ((status.status || "") === "connected") {
      whatsappSummary.textContent = status.phone
        ? `WhatsApp is connected on ${status.phone}.`
        : "WhatsApp is connected.";
      whatsappQrNode.textContent = "WhatsApp connected.";
      finishSummary.textContent = "Your chat app is ready. Save your profile, then turn on your assistant.";
    } else if (qr.qr) {
      whatsappSummary.textContent = "Scan the QR code with WhatsApp to finish connecting.";
      whatsappQrNode.innerHTML = `<img src="${qrImageUrl(qr.qr)}" alt="WhatsApp QR code" />`;
    }
  } catch (_error) {
    // Keep the UI usable even if the bridge is offline.
  }
}

async function handleNext() {
  try {
    if (currentStep === 0) {
      if (!((setupState?.provider || {}).validated_at)) {
        await saveProvider();
      }
      currentStep += 1;
      updateStep();
      return;
    }
    if (currentStep === 1) {
      const hasTelegramToken = field("telegram_bot_token").value.trim();
      if (hasTelegramToken && !((setupState?.channels || {}).telegram || {}).connected) {
        await saveTelegram();
      }
      const channels = setupState?.channels || {};
      const hasConnectedChannel = Object.values(channels).some((channel) => channel && channel.connected);
      if (!hasConnectedChannel) {
        throw new Error("Connect Telegram, WhatsApp, or both before you continue.");
      }
      currentStep += 1;
      updateStep();
      return;
    }
    if (currentStep === 2) {
      await saveProfile();
      currentStep += 1;
      updateStep();
    }
  } catch (error) {
    setStatus(error.message || "Something went wrong.", true);
  }
}

connectTelegramButton.addEventListener("click", async () => {
  try {
    await saveTelegram();
  } catch (error) {
    setStatus(error.message || "Unable to connect Telegram.", true);
  }
});

nextButton.addEventListener("click", handleNext);
backButton.addEventListener("click", () => {
  currentStep = Math.max(currentStep - 1, 0);
  updateStep();
});
activateButton.addEventListener("click", async () => {
  try {
    await activateSetup();
  } catch (error) {
    setStatus(error.message || "Unable to activate your assistant.", true);
  }
});
field("provider").addEventListener("change", () => {
  refreshProviderHint();
  providerSummary.textContent = `${providerLabel(selectedProvider())} is not connected yet.`;
});

updateStep();
refreshProviderHint();
refreshStatus().catch(() => {});
whatsappPoll = window.setInterval(loadWhatsAppStatus, 3000);
window.addEventListener("beforeunload", () => {
  if (whatsappPoll) {
    window.clearInterval(whatsappPoll);
  }
});
