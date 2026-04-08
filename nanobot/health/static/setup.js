const form = document.getElementById("setup-form");
const steps = [...document.querySelectorAll(".step")];
const nextButton = document.getElementById("next");
const backButton = document.getElementById("back");
const activateButton = document.getElementById("activate");
const statusNode = document.getElementById("status");
const completionNode = document.getElementById("completion");
const completionActions = document.getElementById("completion-actions");
const completionGreeting = document.getElementById("completion-greeting");
const webChatLink = document.getElementById("web-chat-link");
const spawnOverlay = document.getElementById("spawn-overlay");
const spawnLine = document.getElementById("spawn-line");
const spawnBar = document.getElementById("spawn-bar");
const telegramSummary = document.getElementById("telegram-summary");
const finishSummary = document.getElementById("finish-summary");
const connectTelegramButton = document.getElementById("connect-telegram");
const openBotFatherButton = document.getElementById("open-botfather");

const TONE_PREFERENCES = {
  gentle: "Warm, gentle nudges",
  direct: "Direct, motivating pushes",
  calm: "Calm grounding support",
};

let currentStep = 0;
let setupState = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runSpawnSequence() {
  if (!spawnOverlay || !spawnLine || !spawnBar) {
    return;
  }
  const lines = [
    "Setting up your private space.",
    "Saving your name and local timing…",
    "Almost there. Getting your companion ready…",
  ];
  let pct = 0;
  for (let i = 0; i < lines.length; i += 1) {
    spawnLine.textContent = lines[i];
    const target = Math.round(((i + 1) / lines.length) * 92);
    while (pct < target) {
      pct += 3;
      spawnBar.style.width = `${Math.min(pct, target)}%`;
      await sleep(40);
    }
    await sleep(320);
  }
  spawnBar.style.width = "100%";
  spawnLine.textContent = "Ready when you are.";
  await sleep(500);
  const companion = spawnOverlay.querySelector(".companion-wrap");
  if (companion) {
    companion.setAttribute("data-state", "idle");
  }
  spawnOverlay.classList.add("is-done");
  spawnOverlay.setAttribute("aria-hidden", "true");
}

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
  const token = form?.dataset.setupToken || "";
  const displayName = (form?.dataset.displayName || "").trim();
  const tone = form.querySelector('input[name="tone_style"]:checked')?.value || "gentle";
  const toneLabel = {
    gentle: "warmly",
    direct: "with a push",
    calm: "with calm energy",
  }[tone] || "warmly";
  if (completionGreeting) {
    const who = displayName || "there";
    completionGreeting.textContent = `Hey ${who}. I’m awake and I’ll show up ${toneLabel}.`;
  }
  if (webChatLink && token) {
    webChatLink.href = `/chat/${encodeURIComponent(token)}`;
    webChatLink.hidden = false;
  }
  form.hidden = true;
  completionNode.hidden = false;
}

function profilePayload() {
  const tone = form.querySelector('input[name="tone_style"]:checked')?.value || "gentle";
  return {
    phase1: {
      // Server will fill sensible defaults if missing.
      preferred_channel: (field("preferred_channel")?.value || "telegram"),
    },
    phase2: {
      goals: [],
      reminder_preferences: [TONE_PREFERENCES[tone] || TONE_PREFERENCES.gentle],
      morning_check_in: Boolean(field("morning_check_in")?.checked),
      weekly_summary: Boolean(field("weekly_summary")?.checked),
    },
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const ref = data.errorId || data.requestId || response.headers.get("X-Request-ID") || "";
    const detail = data.detail;
    if (Array.isArray(detail)) {
      const message = detail
        .map((item) => {
          const path = Array.isArray(item.loc) ? item.loc.join(".") : "";
          const msg = item.msg || "Invalid value";
          return path ? `${path}: ${msg}` : msg;
        })
        .join("; ");
      const fullMessage = message || "Request failed.";
      throw new Error(ref ? `${fullMessage} Ref: ${ref}` : fullMessage);
    }
    const baseMessage = detail || "Request failed.";
    throw new Error(ref ? `${baseMessage} Ref: ${ref}` : baseMessage);
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
  const reminderPreferences = (setupState.profile?.phase2 || {}).reminder_preferences || [];
  if (Array.isArray(reminderPreferences) && reminderPreferences.length) {
    const reminder = reminderPreferences[0];
    const toneInput = {
      [TONE_PREFERENCES.gentle]: "gentle",
      [TONE_PREFERENCES.direct]: "direct",
      [TONE_PREFERENCES.calm]: "calm",
    }[reminder];
    if (toneInput) {
      const toneField = form.querySelector(`input[name="tone_style"][value="${toneInput}"]`);
      if (toneField) {
        toneField.checked = true;
      }
    }
  }
  finishSummary.textContent = setupState.activationReady
    ? "Everything is lined up. Turn it on when you’re ready."
    : "Connect Telegram first, then we can continue.";

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
  setStatus("Starting your companion…");
  const data = await fetchJson(`/api/setup/${token}/activate`, {
    method: "POST",
  });
  setStatus("Your companion is ready.");
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
runSpawnSequence().catch(() => {
  if (spawnOverlay) {
    spawnOverlay.classList.add("is-done");
  }
});
