import { Suspense } from "react";

import { SkillFile } from "@/components/skills/skill-file";

// A file inside a skill's folder, such as one of its references.
export default function SkillFilePage({ params }: PageProps<"/skills/[name]/[...path]">) {
  return (
    <Suspense>
      <FileFromParams params={params} />
    </Suspense>
  );
}

// Reads the skill name and file path from the URL; params is a promise in this Next.js version.
async function FileFromParams({ params }: Pick<PageProps<"/skills/[name]/[...path]">, "params">) {
  const { name, path } = await params;
  return <SkillFile name={decodeURIComponent(name)} path={path.map(decodeURIComponent).join("/")} />;
}
