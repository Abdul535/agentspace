// The MCP admin token the user typed on the settings page, kept in this browser only (ADR-017).
const KEY = "agentspace.mcpAdminToken";

// The saved token, or "" when none is saved or storage is blocked.
export function getMcpAdminToken(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

// Saves the token for later visits (an empty one removes it) and tells subscribers on this page.
export function setMcpAdminToken(token: string): void {
  try {
    if (token) localStorage.setItem(KEY, token);
    else localStorage.removeItem(KEY);
  } catch {
    // Storage blocked (private mode): nothing is kept, so the user enters it again next time.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

const CHANGE_EVENT = "agentspace:mcp-admin-token";

// For useSyncExternalStore: re-reads the token when it changes here or in another tab.
export function subscribeMcpAdminToken(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

// The header every MCP route that saves or starts a command requires.
export function mcpAdminHeaders(): Record<string, string> {
  return { "X-MCP-Admin-Token": getMcpAdminToken() };
}
