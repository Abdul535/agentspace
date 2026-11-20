"use client";

import { code } from "@streamdown/code";
import Link from "next/link";
import { useMemo } from "react";
import { type Components, defaultRehypePlugins, Streamdown, type StreamdownProps } from "streamdown";

type Pluggable = NonNullable<StreamdownProps["rehypePlugins"]>[number];

// Shiki highlighting for fenced code; defined once so Streamdown's memo isn't broken by a new object each render.
const plugins = { code };

// Same-origin links go through Next's router; others open in a new tab.
const components: Components = {
  a: ({ href, children }) => {
    const url = href ? new URL(href, window.location.href) : null;
    if (url && url.origin === window.location.origin) {
      return (
        <Link href={url.pathname + url.hash} className="font-medium underline underline-offset-4">
          {children}
        </Link>
      );
    }
    return (
      <a href={href} target="_blank" rel="noreferrer" className="font-medium underline underline-offset-4">
        {children}
      </a>
    );
  },
};

// A skill's Markdown, with relative links (`references/x.md`) resolved against `baseUrl` instead of blocked.
export function SkillMarkdown({ children, baseUrl }: { children: string; baseUrl: string }) {
  // Streamdown's link hardening blocks relative links unless it knows the origin they're relative to.
  const rehypePlugins = useMemo(() => {
    const [harden, options] = defaultRehypePlugins.harden as unknown as [unknown, Record<string, unknown>];
    const defaultOrigin = new URL(baseUrl, window.location.origin).href;
    const configured = [harden, { ...options, defaultOrigin }] as unknown as Pluggable;
    return Object.values({ ...defaultRehypePlugins, harden: configured });
  }, [baseUrl]);

  return (
    <Streamdown plugins={plugins} mode="static" rehypePlugins={rehypePlugins} components={components}>
      {children}
    </Streamdown>
  );
}
