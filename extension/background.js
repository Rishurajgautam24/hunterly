// Clicking the toolbar icon opens the side panel. Nothing runs on LinkedIn in the background.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
