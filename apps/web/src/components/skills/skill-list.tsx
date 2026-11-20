"use client";

import { ArrowUpRight, FolderOpen, Search, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Skeleton } from "@/components/ui/skeleton";
import type { SkillPublic } from "@/lib/api/client";
import { useSkills } from "@/lib/api/queries";

import { CopyButton, formatTokens, SkillTile } from "./skill-bits";

// Layered transparent shadows give cards depth without a hard border.
const cardShadow =
  "shadow-[0_0_0_1px_oklch(0_0_0/0.06),0_1px_2px_oklch(0_0_0/0.04),0_2px_6px_oklch(0_0_0/0.03)] hover:shadow-[0_0_0_1px_oklch(0_0_0/0.08),0_2px_4px_oklch(0_0_0/0.05),0_8px_20px_oklch(0_0_0/0.06)]";

// Every skill on the shelf as a searchable grid of cards, with what listing them costs the model.
export function SkillList() {
  const skills = useSkills();
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const shown = skills.data?.skills.filter(
    (skill) => skill.name.includes(needle) || skill.description.toLowerCase().includes(needle),
  );

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-6 py-10">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-col gap-1.5">
            <h1 className="flex items-center gap-2.5 text-2xl font-semibold tracking-tight text-balance">
              Skills
              {skills.data && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground tabular-nums">
                  {skills.data.skills.length}
                </span>
              )}
            </h1>
            <p className="max-w-xl text-pretty text-muted-foreground">
              How-to guides every session&apos;s agent can pick up when a task matches.
            </p>
          </div>
          {skills.data && skills.data.skills.length > 0 && (
            <InputGroup className="w-full sm:w-64">
              <InputGroupAddon>
                <Search strokeWidth={1.5} />
              </InputGroupAddon>
              <InputGroupInput
                placeholder="Search skills"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Search skills"
              />
            </InputGroup>
          )}
        </header>

        {skills.isPending && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-36 rounded-xl" />
            ))}
          </div>
        )}
        {skills.isError && <p className="text-sm text-destructive">Can&apos;t load skills: {skills.error.message}</p>}

        {skills.data && skills.data.diagnostics.length > 0 && (
          <Alert>
            <TriangleAlert />
            <AlertTitle>
              {skills.data.diagnostics.length === 1 ? "1 problem" : `${skills.data.diagnostics.length} problems`} in
              the skills folder
            </AlertTitle>
            <AlertDescription>
              <ul className="flex flex-col gap-1 font-mono text-xs break-all">
                {skills.data.diagnostics.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        {skills.data && skills.data.skills.length > 0 && (
          <PromptCost skills={skills.data.skills} listingTokens={skills.data.listing_tokens} />
        )}

        {skills.data?.skills.length === 0 && <EmptyShelf skillsDir={skills.data.skills_dir} />}

        {shown && shown.length > 0 && (
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {shown.map((skill) => (
              <li key={skill.name}>
                <SkillCard skill={skill} />
              </li>
            ))}
          </ul>
        )}
        {shown?.length === 0 && skills.data && skills.data.skills.length > 0 && (
          <p className="py-10 text-center text-sm text-muted-foreground">No skills match &ldquo;{query}&rdquo;.</p>
        )}

        {skills.data && skills.data.skills.length > 0 && (
          <footer className="flex items-center gap-2 text-xs text-muted-foreground">
            <FolderOpen className="size-3.5 shrink-0" strokeWidth={1.5} />
            <span className="truncate font-mono">{skills.data.skills_dir}</span>
            <CopyButton text={skills.data.skills_dir} label="Copy folder path" />
          </footer>
        )}
      </div>
    </div>
  );
}

// One skill: tile, name, description and the cost of reading it.
function SkillCard({ skill }: { skill: SkillPublic }) {
  return (
    <Link
      href={`/skills/${encodeURIComponent(skill.name)}`}
      className={`group flex h-full flex-col gap-3 rounded-xl bg-card p-4 transition-shadow duration-200 ${cardShadow}`}
    >
      <div className="flex items-center gap-3">
        <SkillTile name={skill.name} className="size-9 text-sm" />
        <span className="min-w-0 flex-1 truncate font-mono text-sm font-medium">{skill.name}</span>
        <ArrowUpRight
          className="size-4 shrink-0 text-muted-foreground opacity-0 transition-opacity duration-150 group-hover:opacity-100"
          strokeWidth={1.5}
        />
      </div>
      <p className="line-clamp-3 flex-1 text-sm text-pretty text-muted-foreground">{skill.description}</p>
      <p className="text-xs text-muted-foreground tabular-nums">~{formatTokens(skill.tokens)} tokens when read</p>
    </Link>
  );
}

// The point of skills, drawn to scale: the menu rides along on every call, full instructions only when read.
function PromptCost({ skills, listingTokens }: { skills: SkillPublic[]; listingTokens: number }) {
  const allTokens = skills.reduce((sum, skill) => sum + skill.tokens, 0);
  const menuShare = allTokens > 0 ? Math.max((listingTokens / allTokens) * 100, 1.5) : 100;

  return (
    <section className="flex flex-col gap-4 rounded-xl bg-muted/50 p-5">
      <div className="flex flex-col gap-1">
        <h2 className="text-sm font-medium">What the model carries on every call</h2>
        <p className="text-sm text-pretty text-muted-foreground">
          Only each skill&apos;s name and description go in the system prompt. The model reads a full guide with the{" "}
          <code className="font-mono text-foreground">read</code> tool when a task matches it.
        </p>
      </div>
      <div className="flex flex-col gap-2.5 text-xs">
        <CostBar label="The menu (always sent)" tokens={listingTokens} width={menuShare} strong />
        <CostBar label={`All ${skills.length} guides in full (never sent at once)`} tokens={allTokens} width={100} />
      </div>
    </section>
  );
}

// One labelled bar of the prompt-cost comparison.
function CostBar({ label, tokens, width, strong }: { label: string; tokens: number; width: number; strong?: boolean }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between gap-4 text-muted-foreground">
        <span>{label}</span>
        <span className="font-medium text-foreground tabular-nums">~{formatTokens(tokens)} tokens</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-background">
        <div
          className={strong ? "h-full rounded-full bg-foreground" : "h-full rounded-full bg-foreground/15"}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

// No skills yet: say where to put one and what it looks like.
function EmptyShelf({ skillsDir }: { skillsDir: string }) {
  return (
    <div className="flex flex-col items-center gap-4 rounded-xl border border-dashed px-6 py-14 text-center">
      <FolderOpen className="size-8 text-muted-foreground" strokeWidth={1.5} />
      <div className="flex flex-col gap-1.5">
        <p className="font-medium text-balance">No skills yet</p>
        <p className="max-w-md text-sm text-pretty text-muted-foreground">
          Add a folder with a <code className="font-mono text-foreground">SKILL.md</code> (a{" "}
          <code className="font-mono text-foreground">name</code> and{" "}
          <code className="font-mono text-foreground">description</code> up top, instructions below). It shows up
          here and in the next run, no restart needed.
        </p>
      </div>
      <div className="flex max-w-full items-center gap-1 rounded-lg bg-muted py-1 pr-1 pl-3">
        <span className="truncate font-mono text-xs">{skillsDir}</span>
        <CopyButton text={skillsDir} label="Copy folder path" />
      </div>
    </div>
  );
}
