"use client";

import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

// "1.2k" for 1234: token counts are estimates, so a rounded figure says enough.
export function formatTokens(tokens: number) {
  return tokens < 1000 ? `${tokens}` : `${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k`;
}

// A hue picked from the name, so each skill keeps the same color everywhere.
function hueOf(name: string) {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return hash;
}

// A colored square with the skill's initial: tells skills apart at a glance in lists and headers.
export function SkillTile({ name, className }: { name: string; className?: string }) {
  const hue = hueOf(name);
  return (
    <span
      aria-hidden
      className={cn("grid shrink-0 place-items-center rounded-lg font-mono font-semibold uppercase", className)}
      style={{ backgroundColor: `oklch(0.95 0.035 ${hue})`, color: `oklch(0.45 0.11 ${hue})` }}
    >
      {name.charAt(0)}
    </span>
  );
}

// Copies text, swapping its icon for a check for a moment as the cue that it worked.
export function CopyButton({ text, label, className }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  // Both icons stay mounted and cross-fade, so the swap animates both ways without a motion library.
  const icon = "absolute inset-0 m-auto size-3.5 transition-[opacity,scale,filter] duration-200 ease-[cubic-bezier(0.2,0,0,1)]";
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={label}
      onClick={() => navigator.clipboard.writeText(text).then(() => setCopied(true))}
      className={cn(
        "relative grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground transition-[color,background-color,scale] hover:bg-muted hover:text-foreground active:scale-[0.96]",
        "after:absolute after:-inset-1.5",
        className,
      )}
    >
      <Copy className={cn(icon, copied && "scale-25 opacity-0 blur-[4px]")} strokeWidth={1.5} />
      <Check className={cn(icon, !copied && "scale-25 opacity-0 blur-[4px]")} strokeWidth={2} />
    </button>
  );
}
