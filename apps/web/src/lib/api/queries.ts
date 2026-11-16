"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorMessage } from "./client";

export const queryKeys = {
  sessions: ["sessions"] as const,
  session: (id: string) => ["sessions", id] as const,
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
