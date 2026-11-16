import { Suspense } from "react";

import { Chat } from "@/components/chat/chat";

export default function SessionPage({ params }: PageProps<"/sessions/[id]">) {
  return (
    <Suspense>
      <SessionChat params={params} />
    </Suspense>
  );
}

async function SessionChat({ params }: Pick<PageProps<"/sessions/[id]">, "params">) {
  const { id } = await params;
  // Keyed so switching sessions starts with fresh run state.
  return <Chat key={id} sessionId={id} />;
}
