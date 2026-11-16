import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Typed client for the FastAPI backend, reached through the `/api` rewrite in next.config.ts. */
export const api = createClient<paths>({ baseUrl: "/api" });

type Schemas = components["schemas"];
export type Session = Schemas["SessionPublic"];
export type SessionDetail = Schemas["SessionDetail"];
export type Message = Schemas["Message"];
export type UserMessage = Schemas["UserMessage"];
export type AssistantMessage = Schemas["AssistantMessage"];
export type ToolResultMessage = Schemas["ToolResultMessage"];
export type ToolCall = Schemas["ToolCall"];
export type AgentEvent = Schemas["AgentEvent"];

/** Turn an openapi-fetch error body into a readable message. */
export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return error instanceof Error ? error.message : "Request failed";
}
