// Reports the open tabs of this browser profile to the yt-dlp Downloader app.
//
// Chrome, Brave and Edge keep their live session file locked, so the app cannot
// read the open tabs from disk while the browser runs. This extension sends the
// list instead: to the app's listener on 127.0.0.1 (this computer only), every
// time a tab changes and every 30 seconds. If the app is not running, the send
// simply fails and is tried again later.

const ENDPOINT = "http://127.0.0.1:47813/tabs";
const ALARM = "report-tabs";

async function instanceId() {
  // Tells two profiles of the same browser apart; random, made on first use.
  const { instance } = await chrome.storage.local.get("instance");
  if (instance) return instance;
  const fresh = crypto.randomUUID();
  await chrome.storage.local.set({ instance: fresh });
  return fresh;
}

async function browserName() {
  try {
    if (navigator.brave && await navigator.brave.isBrave()) return "Brave";
  } catch (e) { /* not Brave */ }
  const brands = ((navigator.userAgentData && navigator.userAgentData.brands) || [])
    .map((b) => b.brand);
  const known = [["Brave", "Brave"], ["Microsoft Edge", "Edge"], ["Opera GX", "Opera GX"],
                 ["Opera", "Opera"], ["Vivaldi", "Vivaldi"], ["Google Chrome", "Chrome"]];
  for (const [brand, name] of known) {
    if (brands.includes(brand)) return name;
  }
  const ua = navigator.userAgent;
  if (/ Edg\//.test(ua)) return "Edge";
  if (/ OPR\//.test(ua)) return "Opera";
  if (/ Vivaldi\//.test(ua)) return "Vivaldi";
  return brands.includes("Chromium") ? "Chromium" : "Chrome";
}

async function report() {
  const tabs = await chrome.tabs.query({});
  tabs.sort((a, b) => (a.windowId - b.windowId) || (a.index - b.index));
  const payload = {
    version: 1,
    browser: await browserName(),
    instance: await instanceId(),
    tabs: tabs
      .filter((t) => !t.incognito)
      .map((t) => ({ url: t.url || t.pendingUrl || "", title: t.title || "" })),
  };
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    // The app is not running; the alarm tries again.
  }
}

let pending = null;
function reportSoon() {
  // Several events arrive together when a page loads; send once they settle.
  clearTimeout(pending);
  pending = setTimeout(report, 400);
}

chrome.tabs.onCreated.addListener(reportSoon);
chrome.tabs.onRemoved.addListener(reportSoon);
chrome.tabs.onMoved.addListener(reportSoon);
chrome.tabs.onAttached.addListener(reportSoon);
chrome.tabs.onDetached.addListener(reportSoon);
chrome.tabs.onReplaced.addListener(reportSoon);
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url || change.title || change.status === "complete") reportSoon();
});
chrome.windows.onRemoved.addListener(reportSoon);
chrome.runtime.onStartup.addListener(reportSoon);
chrome.runtime.onInstalled.addListener(reportSoon);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) report();
});
chrome.alarms.get(ALARM).then((alarm) => {
  if (!alarm) chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
});

reportSoon();
