const form = document.getElementById("setup-form");
const steps = [...document.querySelectorAll(".step")];
const nextButton = document.getElementById("next");
const backButton = document.getElementById("back");
const activateButton = document.getElementById("activate");
const statusNode = document.getElementById("status");
const completionNode = document.getElementById("completion");
const completionActions = document.getElementById("completion-actions");
const telegramSummary = document.getElementById("telegram-summary");
const finishSummary = document.getElementById("finish-summary");
const connectTelegramButton = document.getElementById("connect-telegram");
const openBotFatherButton = document.getElementById("open-botfather");

let currentStep = 0;
let setupState = null;

function parseLines(value) {
  return (value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function normalizeTelegramBotToken(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, "");
}

function validateTelegramBotTokenFormat(value) {
  const token = normalizeTelegramBotToken(value);
  if (!token) {
    throw new Error("Paste your Telegram bot token first.");
  }
  // Typical token format: 123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjj
  // Keep it permissive enough for Telegram, strict enough to catch copy mistakes.
  const looksRight = /^\d{5,}:[A-Za-z0-9_-]{20,}$/.test(token);
  if (!looksRight) {
    throw new Error(
      "That token doesn’t look right. It should look like “123456:ABC-DEF...” (numbers, colon, then letters).",
    );
  }
  return token;
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
  // Minimal onboarding: we only keep a few optional fields in the wizard.
  if (!profile) {
    return;
  }
  const phase2 = (profile.phase2 || {});
  if (typeof phase2.morning_check_in === "boolean") {
    setFieldValue("morning_check_in", phase2.morning_check_in);
  }
  if (typeof phase2.weekly_summary === "boolean") {
    setFieldValue("weekly_summary", phase2.weekly_summary);
  }
  if (Array.isArray(phase2.goals)) {
    setFieldValue("goals", phase2.goals);
  }
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
  return {
    phase1: {
      // Server will fill sensible defaults if missing.
      preferred_channel: (field("preferred_channel")?.value || "telegram"),
    },
    phase2: {
      goals: parseLines(field("goals")?.value || ""),
      morning_check_in: Boolean(field("morning_check_in")?.checked),
      weekly_summary: Boolean(field("weekly_summary")?.checked),
    },
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    if (Array.isArray(detail)) {
      const message = detail
        .map((item) => {
          const path = Array.isArray(item.loc) ? item.loc.join(".") : "";
          const msg = item.msg || "Invalid value";
          return path ? `${path}: ${msg}` : msg;
        })
        .join("; ");
      throw new Error(message || "Request failed.");
    }
    throw new Error(detail || "Request failed.");
  }
  return data;
}

async function refreshStatus() {
  const token = form.dataset.setupToken;
  setupState = await fetchJson(`/api/setup/${token}/status`);

  const telegram = (setupState.channels || {}).telegram || {};
  telegramSummary.textContent = telegram.connected
    ? `Connected as @${telegram.bot_username || "your_bot"}.`
    : "Telegram is not connected yet.";
  if (telegram.connected && telegram.bot_username) {
    setFieldValue("preferred_channel", "telegram");
  }

  fillProfile(setupState.profile);
  finishSummary.textContent = setupState.activationReady
    ? "Everything looks ready. Turn on your coach when you’re ready."
    : "You can activate once Telegram is connected.";

  if (setupState.state === "active") {
    renderCompletion(setupState.channelLinks || {});
  }
}

async function saveTelegram() {
  const botToken = validateTelegramBotTokenFormat(field("telegram_bot_token").value);
  setFieldValue("telegram_bot_token", botToken);
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
  setStatus("Saving…");
  await fetchJson(`/api/setup/${token}/profile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profilePayload()),
  });
  await refreshStatus();
  setStatus("Saved.");
}

async function activateSetup() {
  const token = form.dataset.setupToken;
  setStatus("Spinning up your coach…");
  const data = await fetchJson(`/api/setup/${token}/activate`, {
    method: "POST",
  });
  setStatus("Your assistant is ready.");
  renderCompletion(data.channelLinks || {});
}

async function handleNext() {
  try {
    if (currentStep === 0) {
      const hasTelegramToken = field("telegram_bot_token").value.trim();
      if (hasTelegramToken && !((setupState?.channels || {}).telegram || {}).connected) {
        await saveTelegram();
      }
      const channels = setupState?.channels || {};
      const hasConnectedChannel = Object.values(channels).some((channel) => channel && channel.connected);
      if (!hasConnectedChannel) {
        throw new Error("Connect Telegram before you continue.");
      }
      currentStep += 1;
      updateStep();
      return;
    }
    if (currentStep === 1) {
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

if (openBotFatherButton) {
  openBotFatherButton.addEventListener("click", () => {
    window.open("https://t.me/BotFather", "_blank", "noopener");
  });
}

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

updateStep();
refreshStatus().catch(() => {});

