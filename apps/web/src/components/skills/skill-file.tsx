"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { useSkill, useSkillFile } from "@/lib/api/queries";

import { Breadcrumbs, FileList, skillHref } from "./skill-detail";
import { SkillMarkdown } from "./skill-markdown";

// Markdown shows rendered; any other text file shows as a highlighted code block.
function asMarkdown(path: string, content: string) {
  if (/\.(md|markdown)$/i.test(path)) return content;
  const language = path.includes(".") ? path.split(".").pop() : "";
  // A fence longer than any backtick run inside the file, so the file can't close it early.
  const longest = Math.max(2, ...(content.match(/`+/g) ?? []).map((run) => run.length));
  const fence = "`".repeat(longest + 1);
  return `${fence}${language}\n${content}\n${fence}`;
}

// A file inside a skill's folder, with the folder's file list beside it.
export function SkillFile({ name, path }: { name: string; path: string }) {
  const file = useSkillFile(name, path);
  const skill = useSkill(name);
  const folder = path.includes("/") ? path.slice(0, path.lastIndexOf("/") + 1) : "";

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-6 py-10">
        <Breadcrumbs name={name} file={path} />
        <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <article className="min-w-0">
            {file.isPending && <Skeleton className="h-80 w-full" />}
            {file.isError && <p className="text-sm text-destructive">{file.error.message}</p>}
            {file.data && (
              <SkillMarkdown baseUrl={`${skillHref(name)}/${folder}`}>{asMarkdown(path, file.data.content)}</SkillMarkdown>
            )}
          </article>
          {skill.data && (
            <aside className="flex flex-col gap-2.5 lg:sticky lg:top-0 lg:self-start">
              <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                Files ({skill.data.files.length})
              </h2>
              <FileList name={name} files={skill.data.files} current={path} />
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
