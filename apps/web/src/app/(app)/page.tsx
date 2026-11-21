import { NewSessionButton } from "@/components/new-session-button";

export default function Home() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 className="text-2xl font-semibold tracking-tight text-balance">Start a session</h1>
      <p className="max-w-sm text-pretty text-muted-foreground">
        Each session runs an agent with its own workspace folder. It can read and write files there.
      </p>
      <NewSessionButton />
    </div>
  );
}
