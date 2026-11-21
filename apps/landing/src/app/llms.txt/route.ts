import { APP_URL, FAQ, FEATURES, LOOP_STEPS, SITE, SITE_URL } from "@/lib/site";

// Written to a plain file at build time, as a static export needs.
export const dynamic = "force-static";

// /llms.txt (llmstxt.org): the page's content as Markdown, so an AI assistant can read it without parsing HTML.
export function GET() {
  const lines = [
    `# ${SITE.name}`,
    "",
    `> ${SITE.description}`,
    "",
    "## How an agent loop works",
    "",
    ...LOOP_STEPS.map((step, i) => `${i + 1}. **${step.title}**: ${step.body}`),
    "",
    "## What agentspace shows",
    "",
    ...FEATURES.map((feature) => `- **${feature.title}**: ${feature.body}`),
    "",
    "## FAQ",
    "",
    ...FAQ.flatMap(({ question, answer }) => [`### ${question}`, "", answer, ""]),
    "## Links",
    "",
    `- [Landing page](${SITE_URL}): what agentspace is`,
    `- [The app](${APP_URL}): try the agent and watch each step`,
    "",
  ];
  return new Response(lines.join("\n"), { headers: { "Content-Type": "text/plain; charset=utf-8" } });
}
