"use client";

import { ChevronRight, FileText } from "lucide-react";
import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import { useSkill } from "@/lib/api/queries";
import { cn } from "@/lib/utils";

import { CopyButton, formatTokens, SkillTile } from "./skill-bits";
import { SkillMarkdown } from "./skill-markdown";

// The URL of a skill page, or of a file inside the skill.
export function skillHref(name: string, file?: string) {
  const base = `/skills/${encodeURIComponent(name)}`;
  return file ? `${base}/${file.split("/").map(encodeURIComponent).join("/")}` : base;
}

// One skill: header, its SKILL.md rendered, and a side panel with what the model sees and the folder's files.
export function SkillDetail({ name }: { name: string }) {
  const skill = useSkill(name);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-6 py-10">
        <Breadcrumbs name={name} />

        {skill.isPending && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-12 w-64" />
            <Skeleton className="h-4 w-full max-w-xl" />
            <Skeleton className="h-80 w-full" />
          </div>
        )}
        {skill.isError && <p className="text-sm text-destructive">{skill.error.message}</p>}

        {skill.data && (
          <>
            <header className="flex flex-col gap-4 sm:flex-row sm:items-start">
              <SkillTile name={skill.data.name} className="size-12 text-lg" />
              <div className="flex min-w-0 flex-col gap-1.5">
                <h1 className="font-mono text-2xl font-semibold tracking-tight break-words">{skill.data.name}</h1>
                <p className="max-w-2xl text-pretty text-muted-foreground">{skill.data.description}</p>
              </div>
            </header>

            <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
              <article className="min-w-0">
                <SkillMarkdown baseUrl={`${skillHref(skill.data.name)}/`}>{skill.data.content}</SkillMarkdown>
              </article>

              <aside className="flex flex-col gap-6 lg:sticky lg:top-0 lg:self-start">
                <Panel title="In the system prompt">
                  <pre className="rounded-lg bg-muted p-3 font-mono text-[11px] leading-relaxed wrap-anywhere whitespace-pre-wrap">
                    {skill.data.prompt_entry.replace(/^ {2}/gm, "")}
                  </pre>
                  <p className="text-xs text-pretty text-muted-foreground">
                    All the model knows about this skill until it reads the file: about{" "}
                    <span className="tabular-nums">{formatTokens(skill.data.tokens)}</span> tokens more.
                  </p>
                </Panel>

                <Panel title={`Files (${skill.data.files.length})`}>
                  <FileList name={skill.data.name} files={skill.data.files} />
                </Panel>

                <Panel title="Location">
                  <div className="flex items-start gap-1">
                    <span className="min-w-0 flex-1 font-mono text-xs break-all text-muted-foreground">
                      {skill.data.location}
                    </span>
                    <CopyButton text={skill.data.location} label="Copy path" className="-mt-1.5" />
                  </div>
                </Panel>
              </aside>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// "Skills › name › file", each step a link but the last.
export function Breadcrumbs({ name, file }: { name: string; file?: string }) {
  const steps = [
    { label: "Skills", href: "/skills" },
    { label: name, href: skillHref(name), mono: true },
    ...(file ? [{ label: file, href: skillHref(name, file), mono: true }] : []),
  ];
  return (
    <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1 text-sm text-muted-foreground">
      {steps.map((step, index) => {
        const last = index === steps.length - 1;
        return (
          <span key={step.href} className={cn("flex min-w-0 items-center gap-1", last && "flex-1")}>
            {index > 0 && <ChevronRight className="size-3.5 shrink-0" strokeWidth={1.5} />}
            {last ? (
              <span aria-current="page" className={cn("truncate text-foreground", step.mono && "font-mono")}>
                {step.label}
              </span>
            ) : (
              <Link
                href={step.href}
                className={cn("truncate transition-colors hover:text-foreground", step.mono && "font-mono")}
              >
                {step.label}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}

// A titled block of the side panel.
function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h2>
      {children}
    </section>
  );
}

// The skill folder's files; SKILL.md is this page, the rest open in the file viewer.
export function FileList({ name, files, current }: { name: string; files: string[]; current?: string }) {
  return (
    <ul className="-mx-2 flex flex-col">
      {files.map((file) => {
        const active = (current ?? "SKILL.md") === file;
        return (
          <li key={file}>
            <Link
              href={file === "SKILL.md" ? skillHref(name) : skillHref(name, file)}
              aria-current={active ? "page" : undefined}
              className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground aria-[current=page]:bg-muted aria-[current=page]:text-foreground"
            >
              <FileText className="size-3.5 shrink-0" strokeWidth={1.5} />
              <span className="truncate font-mono text-xs">{file}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
