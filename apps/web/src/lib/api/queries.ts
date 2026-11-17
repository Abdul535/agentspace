"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorMessage, type SessionDetail } from "./client";

export const queryKeys = {
  sessions: ["sessions"] as const,
  session: (id: string) => ["sessions", id] as const,
  models: ["models"] as const,
};

export function useSessions() {
  return useQuery({
    queryKey: queryKeys.sessions,
    queryFn: async () => {
      const { data, error } = await api.GET("/sessions");
      if (error) throw new Error(errorMessage(error));
      return data;
    },
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

// Switches a session's model; the cached session keeps its transcript and takes the new model.
export function useSetSessionModel(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (model: string) => {
      const { data, error } = await api.PATCH("/sessions/{session_id}", {
        params: { path: { session_id: id } },
        body: { model },
      });
      if (error) throw new Error(errorMessage(error));
      return data;
    },
    onSuccess: (session) => {
      queryClient.setQueryData<SessionDetail>(queryKeys.session(id), (old) => old && { ...old, ...session });
      return queryClient.invalidateQueries({ queryKey: queryKeys.sessions, exact: true });
    },
  });
}
