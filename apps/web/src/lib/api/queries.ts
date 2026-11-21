"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  errorMessage,
  type McpServerCreate,
  type McpServerUpdate,
  type SessionDetail,
  type ThinkingLevel,
} from "./client";
import { mcpAdminHeaders } from "./mcp-admin-token";

export const queryKeys = {
  sessions: ["sessions"] as const,
  session: (id: string) => ["sessions", id] as const,
  models: ["models"] as const,
  skills: ["skills"] as const,
  skill: (name: string) => ["skills", name] as const,
  mcpServers: ["mcp-servers"] as const,
};

// The sidebar's session list; polled so a run started or finished elsewhere shows up, faster while one runs.
export function useSessions() {
  return useQuery({
    queryKey: queryKeys.sessions,
    queryFn: async () => {
      const { data, error } = await api.GET("/sessions");
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    refetchInterval: (query) => (query.state.data?.some((s) => s.running) ? 2000 : 10000),
  });
}

// Hides a session from the list; the API refuses (409) while it runs.
export function useArchiveSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.POST("/sessions/{session_id}/archive", { params: { path: { session_id: id } } });
      if (error) throw new Error(errorMessage(error));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.sessions, exact: true }),
  });
}

export function useSession(id: string) {
  return useQuery({
    queryKey: queryKeys.session(id),
    queryFn: async () => {
      const { data, error } = await api.GET("/sessions/{session_id}", {
        params: { path: { session_id: id } },
      });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
  });
}

export function useCreateSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/sessions", { body: {} });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.sessions }),
  });
}

// The model catalog; the API caches it for an hour, so there's no point refetching sooner.
export function useModels() {
  return useQuery({
    queryKey: queryKeys.models,
    queryFn: async () => {
      const { data, error } = await api.GET("/models");
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    staleTime: 60 * 60 * 1000,
  });
}

/** Session settings a PATCH can change; fields left out stay as they are. */
export type SessionChange = { model?: string; thinking_level?: ThinkingLevel };

// Switches a session's model or thinking level; the cached session keeps its transcript and takes the change.
export function useUpdateSession(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (change: SessionChange) => {
      const { data, error } = await api.PATCH("/sessions/{session_id}", {
        params: { path: { session_id: id } },
        body: change,
      });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    onSuccess: (session) => {
      queryClient.setQueryData<SessionDetail>(queryKeys.session(id), (old) => old && { ...old, ...session });
      // Refetched too: a switch adds a notice to the transcript, which the PATCH response doesn't carry.
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.session(id) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.sessions, exact: true }),
      ]);
    },
  });
}

// Skills on the shared shelf; the API rescans on every request, so refetch on each mount and window focus.
export function useSkills() {
  return useQuery({
    queryKey: queryKeys.skills,
    queryFn: async () => {
      const { data, error } = await api.GET("/skills");
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    staleTime: 0,
  });
}

// One skill's instructions, read fresh from its SKILL.md.
export function useSkill(name: string) {
  return useQuery({
    queryKey: queryKeys.skill(name),
    queryFn: async () => {
      const { data, error } = await api.GET("/skills/{name}", { params: { path: { name } } });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    staleTime: 0,
    retry: false,
  });
}

// A text file inside a skill's folder, e.g. one of its references.
export function useSkillFile(name: string, path: string) {
  return useQuery({
    queryKey: [...queryKeys.skill(name), "files", path] as const,
    queryFn: async () => {
      const { data, error } = await api.GET("/skills/{name}/files/{path}", { params: { path: { name, path } } });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    staleTime: 0,
    retry: false,
  });
}

// The MCP servers on the settings page, and whether the API starts them at all (ADR-017).
export function useMcpServers() {
  return useQuery({
    queryKey: queryKeys.mcpServers,
    queryFn: async () => {
      const { data, error } = await api.GET("/mcp-servers");
      if (error) throw new Error(errorMessage(error));
      return data;
    },
  });
}

// Saves a new MCP server; sessions pick it up on their next message.
export function useCreateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: McpServerCreate) => {
      const { data, error } = await api.POST("/mcp-servers", { body, headers: mcpAdminHeaders() });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.mcpServers }),
  });
}

// Changes an MCP server; the API restarts its processes with the new settings when next needed.
export function useUpdateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, change }: { id: string; change: McpServerUpdate }) => {
      const { data, error } = await api.PATCH("/mcp-servers/{server_id}", {
        params: { path: { server_id: id } },
        body: change,
        headers: mcpAdminHeaders(),
      });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.mcpServers }),
  });
}

// Deletes an MCP server and stops its processes.
export function useDeleteMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/mcp-servers/{server_id}", {
        params: { path: { server_id: id } },
        headers: mcpAdminHeaders(),
      });
      if (error) throw new Error(errorMessage(error));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.mcpServers }),
  });
}

// Starts a server once and lists its tools; a server that fails still answers 200 with `ok: false`.
export function useTestMcpServer() {
  return useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.POST("/mcp-servers/{server_id}/test", {
        params: { path: { server_id: id } },
        headers: mcpAdminHeaders(),
      });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
  });
}
