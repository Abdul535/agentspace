import { CircleDollarSign } from "lucide-react";

import type { Message } from "@/lib/api/client";

// Adds up what every model reply in the transcript cost (pi's `usage-totals.ts`); compaction keeps old replies, so this is the whole session.
export function sessionCost(messages: Message[]): { total: number; replies: number; estimated: boolean } {
  let total = 0;
  let replies = 0;
  let estimated = false;
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    const cost = message.usage.cost;
    total += cost.total;
    replies += 1;
    // Not billed: a list-price estimate, or $0 when the catalog had no price (or the reply predates step 34).
    if (!cost.from_provider) estimated = true;
  }
  return { total, replies, estimated };
}

// Dollars with enough digits that a cheap session doesn't read as $0.00.
function dollars(amount: number): string {
  const digits = amount === 0 ? 2 : amount < 0.01 ? 4 : amount < 1 ? 3 : 2;
  return `$${amount.toFixed(digits)}`;
}

/** The session's running cost in the chat header; hidden until the model has replied once. */
export function SessionCost({ messages }: { messages: Message[] }) {
  const { total, replies, estimated } = sessionCost(messages);
  if (replies === 0) return null;
  const source = estimated ? "some replies estimated from list prices" : "as billed by OpenRouter";
  return (
    <span
      className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground tabular-nums"
      title={`${replies} ${replies === 1 ? "reply" : "replies"}, ${source}`}
    >
      <CircleDollarSign className="size-3.5" aria-hidden />
      {estimated && "≈"}
      {dollars(total)}
    </span>
  );
}
