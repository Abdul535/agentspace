import { ArrowDown, ArrowRight, Check, FileText, LoaderCircle, Search, Terminal } from "lucide-react";

import { APP_URL, FAQ, FEATURES, LOOP_STEPS, SITE, SITE_URL } from "@/lib/site";

// Structured data: tells search engines and AI assistants this is a piece of software and which questions the page answers.
const JSON_LD = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebSite",
      name: SITE.name,
      url: SITE_URL,
      description: SITE.description,
    },
    {
      "@type": "SoftwareApplication",
      name: SITE.name,
      url: SITE_URL,
      applicationCategory: "EducationalApplication",
      operatingSystem: "Web",
      description: SITE.description,
      offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
    },
    {
      "@type": "FAQPage",
      mainEntity: FAQ.map(({ question, answer }) => ({
        "@type": "Question",
        name: question,
        acceptedAnswer: { "@type": "Answer", text: answer },
      })),
    },
  ],
};

// The one landing page: rendered to static HTML at build time, so crawlers read every word without running JavaScript.
export default function Home() {
  return (
    <>
      <script
        type="application/ld+json"
        // Escape "<" so no string in the data can close the script tag early.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD).replace(/</g, "\\u003c") }}
      />
      <SiteHeader />
      <main>
        <Hero />
        <LoopSection />
        <FeatureSection />
        <ScratchSection />
        <FaqSection />
        <FinalCta />
      </main>
      <SiteFooter />
    </>
  );
}

// Wordmark, in-page links and a quiet way into the app.
function SiteHeader() {
  return (
    <header className="sticky top-0 z-10 border-b bg-sidebar">
      <div className="mx-auto flex h-12 max-w-6xl items-center gap-6 px-4 sm:px-6">
        <a href="#top" className="flex items-center gap-2 text-[13px] font-bold tracking-tight">
          <span aria-hidden className="size-3 bg-primary" />
          {SITE.name}
        </a>
        <nav aria-label="Sections" className="hidden items-center gap-5 text-[12.5px] text-muted-foreground sm:flex">
          <a href="#how-it-works" className="transition-colors duration-150 hover:text-foreground">
            How it works
          </a>
          <a href="#features" className="transition-colors duration-150 hover:text-foreground">
            What you see
          </a>
          <a href="#faq" className="transition-colors duration-150 hover:text-foreground">
            FAQ
          </a>
        </nav>
        <a
          href={APP_URL}
          className="ml-auto inline-flex h-8 items-center gap-1.5 border border-input bg-card px-3 text-[12.5px] font-semibold transition-[background-color,transform] duration-150 hover:bg-accent active:scale-[0.97]"
        >
          Try the app
        </a>
      </div>
    </header>
  );
}

// The promise in one line, the main button, and a picture of a run so the idea lands before any reading.
function Hero() {
  return (
    <section id="top" className="border-b">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-14 sm:px-6 md:grid-cols-[1fr_minmax(0,440px)] md:py-20">
        <div className="flex flex-col justify-center">
          <Label>Learn agents by watching one</Label>
          <h1 className="mt-3 text-4xl font-bold tracking-[-0.02em] text-balance sm:text-5xl">
            See how an AI agent works, step by step.
          </h1>
          <p className="mt-4 max-w-xl text-[15px] text-pretty text-muted-foreground">
            agentspace is an AI agent built from scratch that shows its work: what it sends the model, which tools it
            calls, what comes back and what each step costs. No black box, no framework.
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <PrimaryButton />
            <a
              href="#how-it-works"
              className="inline-flex h-10 items-center gap-1.5 px-2 text-[13px] font-semibold text-muted-foreground transition-colors duration-150 hover:text-foreground"
            >
              How it works
              <ArrowDown aria-hidden className="size-4" strokeWidth={1.75} />
            </a>
          </div>
        </div>
        <RunPreview />
      </div>
    </section>
  );
}

// A still picture of one run in the app's own style: the user's ask, the tool calls, and the answer arriving.
function RunPreview() {
  return (
    <figure aria-label="Example of an agent run in agentspace" className="border bg-card">
      <div className="flex items-center justify-between border-b bg-muted px-3 py-2">
        <Label>session · fix the failing test</Label>
        <span className="flex items-center gap-1.5 font-mono text-[10.5px] text-link uppercase">
          <span aria-hidden className="pulse-dot size-1.5 rounded-full bg-primary" />
          running
        </span>
      </div>
      <div className="space-y-3 p-3">
        <Message role="user">Why does test_parse fail?</Message>
        <Message role="assistant">
          <p>Let me look at the test and the code it calls.</p>
          <div className="mt-2 space-y-1.5">
            <ToolRow icon={<Search className="size-3.5" strokeWidth={1.75} />} name="grep" arg='"def parse"' status="done" />
            <ToolRow icon={<FileText className="size-3.5" strokeWidth={1.75} />} name="read" arg="src/parse.py" status="done" />
            <ToolRow icon={<Terminal className="size-3.5" strokeWidth={1.75} />} name="bash" arg="pytest -q" status="running" />
          </div>
        </Message>
      </div>
      <figcaption className="flex items-center justify-between border-t px-3 py-2 font-mono text-[10.5px] text-muted-foreground uppercase">
        <span>turn 2 · 3 tool calls</span>
        <span className="tabular-nums">$0.0042</span>
      </figcaption>
    </figure>
  );
}

// One chat message: a mono role strip on top, the content underneath (DESIGN.md "Chat message").
function Message({ role, children }: { role: "user" | "assistant"; children: React.ReactNode }) {
  return (
    <div className="border">
      <div className="border-b bg-muted px-2 py-1">
        <Label>{role}</Label>
      </div>
      <div className="p-2 text-[13px]">{children}</div>
    </div>
  );
}

// A tool call in a dashed box, since it comes from the machine and not the person.
function ToolRow({ icon, name, arg, status }: { icon: React.ReactNode; name: string; arg: string; status: "done" | "running" }) {
  return (
    <div className="flex items-center gap-2 border border-dashed border-input px-2 py-1 font-mono text-[11.5px]">
      <span aria-hidden className="text-muted-foreground">
        {icon}
      </span>
      <span className="font-semibold">{name}</span>
      <span className="truncate text-muted-foreground">{arg}</span>
      <span className="ml-auto">
        {status === "done" ? (
          <Check aria-label="done" className="size-3.5 text-link" strokeWidth={2} />
        ) : (
          <LoaderCircle aria-label="running" className="size-3.5 animate-spin text-muted-foreground" strokeWidth={1.75} />
        )}
      </span>
    </div>
  );
}

// The agent loop as four boxes in a row, with the "repeat" rule written out underneath.
function LoopSection() {
  return (
    <Section id="how-it-works" label="How it works" title="An agent is a model in a loop with tools">
      <ol className="grid border-t border-l sm:grid-cols-2 lg:grid-cols-4">
        {LOOP_STEPS.map((step, i) => (
          <li key={step.title} className="border-r border-b bg-card p-4">
            <span className="font-mono text-[10.5px] text-link tabular-nums">0{i + 1}</span>
            <h3 className="mt-2 text-[13px] font-semibold">{step.title}</h3>
            <p className="mt-1 text-[13px] text-muted-foreground">{step.body}</p>
          </li>
        ))}
      </ol>
      <p className="mt-4 flex items-center gap-2 font-mono text-[11.5px] text-muted-foreground">
        <ArrowRight aria-hidden className="size-3.5 rotate-180" strokeWidth={1.75} />
        Back to step 02, until the model answers without asking for a tool.
      </p>
    </Section>
  );
}

// What the app shows you, as a dense grid of labeled cells.
function FeatureSection() {
  return (
    <Section id="features" label="What you see" title="Every part of the loop, on screen">
      <ul className="grid border-t border-l sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature) => (
          <li key={feature.title} className="border-r border-b bg-card p-4">
            <Label>{feature.label}</Label>
            <h3 className="mt-2 text-[13px] font-semibold">{feature.title}</h3>
            <p className="mt-1 text-[13px] text-muted-foreground">{feature.body}</p>
          </li>
        ))}
      </ul>
    </Section>
  );
}

// Why it's worth learning from: nothing is hidden inside a library.
function ScratchSection() {
  return (
    <Section id="from-scratch" label="Built from scratch" title="No framework between you and the loop">
      <div className="grid gap-6 md:grid-cols-2">
        <p className="text-[14px] text-pretty text-muted-foreground">
          Most agents hide the loop inside a framework. agentspace writes every piece itself: the loop, tool calling,
          model providers, skills and sessions. When you want to know why the agent did something, the answer is a
          short file you can read, not a library you have to trust.
        </p>
        <dl className="grid grid-cols-2 border-t border-l">
          {[
            ["Backend", "Python · FastAPI"],
            ["Frontend", "Next.js · TypeScript"],
            ["Models", "Any OpenAI-compatible API"],
            ["Frameworks", "None"],
          ].map(([term, value]) => (
            <div key={term} className="border-r border-b bg-card p-3">
              <dt>
                <Label>{term}</Label>
              </dt>
              <dd className="mt-1 text-[13px] font-semibold">{value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </Section>
  );
}

// Plain questions with plain answers, always visible so people and crawlers read the same text.
function FaqSection() {
  return (
    <Section id="faq" label="FAQ" title="Questions people ask about agents">
      <dl className="grid border-t border-l md:grid-cols-2">
        {FAQ.map(({ question, answer }) => (
          <div key={question} className="border-r border-b bg-card p-4">
            <dt className="text-[13px] font-semibold">{question}</dt>
            <dd className="mt-1 text-[13px] text-muted-foreground">{answer}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

// The last ask, repeated where the reader ends up.
function FinalCta() {
  return (
    <section className="border-b">
      <div className="mx-auto flex max-w-6xl flex-col items-start gap-5 px-4 py-14 sm:px-6 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-[-0.01em]">Watch an agent think, then read how it did it.</h2>
          <p className="mt-1 text-[14px] text-muted-foreground">Open the app, give it a task, and follow every step.</p>
        </div>
        <PrimaryButton />
      </div>
    </section>
  );
}

function SiteFooter() {
  return (
    <footer className="mx-auto flex max-w-6xl items-center justify-between px-4 py-6 font-mono text-[10.5px] text-muted-foreground uppercase sm:px-6">
      <span>{SITE.name}</span>
      <span>{SITE.tagline}</span>
    </footer>
  );
}

// The yellow button: the single most important action, one per view (DESIGN.md).
function PrimaryButton() {
  return (
    <a
      href={APP_URL}
      className="inline-flex h-10 items-center gap-2 bg-primary px-4 text-[13px] font-semibold text-primary-foreground transition-[filter,transform] duration-150 hover:brightness-95 active:scale-[0.97]"
    >
      Try the app
      <ArrowRight aria-hidden className="size-4" strokeWidth={1.75} />
    </a>
  );
}

// A page section with a mono label, a heading with a rule under it, and its content.
function Section({ id, label, title, children }: { id: string; label: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="scroll-mt-12 border-b">
      <div className="mx-auto max-w-6xl px-4 py-14 sm:px-6">
        <Label>{label}</Label>
        <h2 className="mt-2 border-b pb-3 text-xl font-bold tracking-[-0.01em] text-balance">{title}</h2>
        <div className="mt-6">{children}</div>
      </div>
    </section>
  );
}

// Small uppercase mono text that names things (DESIGN.md "Label").
function Label({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-[10.5px] tracking-[0.06em] text-muted-foreground uppercase">{children}</span>;
}
