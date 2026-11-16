"use client";

import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { useCreateSession } from "@/lib/api/queries";

export function NewSessionButton({ className }: { className?: string }) {
  const router = useRouter();
  const create = useCreateSession();

  return (
    <Button
      className={className}
      disabled={create.isPending}
      onClick={() => create.mutate(undefined, { onSuccess: (session) => router.push(`/sessions/${session.id}`) })}
    >
      <Plus data-icon="inline-start" />
      New session
    </Button>
  );
}
