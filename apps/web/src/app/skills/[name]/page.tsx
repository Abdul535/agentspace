import { Suspense } from "react";

import { SkillDetail } from "@/components/skills/skill-detail";

// One skill's SKILL.md, rendered as Markdown.
export default function SkillPage({ params }: PageProps<"/skills/[name]">) {
  return (
    <Suspense>
      <SkillFromParams params={params} />
    </Suspense>
  );
}

// Reads the skill name from the URL; params is a promise in this Next.js version.
async function SkillFromParams({ params }: Pick<PageProps<"/skills/[name]">, "params">) {
  const { name } = await params;
  return <SkillDetail name={decodeURIComponent(name)} />;
}
