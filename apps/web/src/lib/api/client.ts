import createClient from "openapi-fetch";

import { authHeaders, onSessionEnded } from "@/lib/auth/browser";

import type { components, paths } from "./schema";

/** Typed client for the FastAPI backend, reached through the `/api` rewrite in next.config.ts. */
export const api = createClient<paths>({ baseUrl: "/api" });

// Every call carries the signed-in user's token; a 401 means the session is over, so sign in again (ADR-024).
api.use({
  onRequest: async ({ request }) => {
    for (const [name, value] of Object.entries(await authHeaders())) request.headers.set(name, value);
    return request;
  },
  onResponse: ({ response }) => {
    if (response.status === 401) onSessionEnded();
    return response;
  },
});

type Schemas = components["schemas"];
export type Session = Schemas["SessionPublic"];
export type SessionDetail = Schemas["SessionDetail"];
// A transcript message: the three the model reads plus the app's own, such as notices (ADR-008).
export type Message = Schemas["AgentMessage"];
export type NoticeMessage = Schemas["NoticeMessage"];
export type CompactionSummaryMessage = Schemas["CompactionSummaryMessage"];
export type UserMessage = Schemas["UserMessage"];
export type AssistantMessage = Schemas["AssistantMessage"];
export type ToolResultMessage = Schemas["ToolResultMessage"];
export type ToolCall = Schemas["ToolCall"];
export type AgentEvent = Schemas["AgentEvent"];
export type RunSnapshot = Schemas["RunSnapshot"];
export type ModelInfo = Schemas["ModelInfo"];
export type ThinkingLevel = Schemas["ThinkingLevel"];
export type SkillPublic = Schemas["SkillPublic"];
// MCP servers saved on the settings page (ADR-017).
export type McpServer = Schemas["McpServerPublic"];
export type McpServerCreate = Schemas["McpServerCreate"];
export type McpServerUpdate = Schemas["McpServerUpdate"];
export type McpServerTest = Schemas["McpServerTest"];
// The user's own model API keys, one entry per provider (ADR-026).
export type ProviderKey = Schemas["ProviderKeyPublic"];
export type ProviderKeySaved = Schemas["ProviderKeySaved"];

/** Turn an openapi-fetch error body into a readable message. */
export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    // A refusal with a code (ADR-026) carries its text in `message`.
    if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
    return JSON.stringify(detail);
  }
  return error instanceof Error ? error.message : "Request failed";
}
