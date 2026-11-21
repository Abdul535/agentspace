// Everything the landing page says, in one place: the page, its JSON-LD and /llms.txt all read from here so they never disagree.

// Read at build time and baked into the HTML; set both before deploying (ADR-023).
export const SITE_URL = process.env.SITE_URL ?? "http://localhost:3001";
export const APP_URL = process.env.APP_URL ?? "http://localhost:3000";

export const SITE = {
  name: "agentspace",
  title: "agentspace: see how an AI agent works, step by step",
  description:
    "agentspace is an AI agent built from scratch that shows you every step it takes: what it asks the model, which tools it calls, what comes back, and what it costs. Learn how agents work by watching one.",
  tagline: "Learn how AI agents work by watching one.",
};

// The agent loop, one step per cell.
export const LOOP_STEPS = [
  {
    title: "You ask",
    body: "Your message joins the conversation. The agent sends the whole conversation, plus a list of tools it may use, to the model.",
  },
  {
    title: "The model replies",
    body: "The reply streams back word by word. Sometimes it is an answer; sometimes it is a request: “run this tool with these arguments”.",
  },
  {
    title: "Tools run",
    body: "The agent runs each requested tool (read a file, search, run a command) inside the session’s own workspace folder.",
  },
  {
    title: "Results go back",
    body: "Each tool’s output is added to the conversation and sent to the model again. The loop repeats until a reply asks for no tools.",
  },
];

// What the app lets you watch, each one a real feature of agentspace today.
export const FEATURES = [
  {
    label: "Activity",
    title: "Every step on a timeline",
    body: "Each message and tool call appears as it happens, with its status: running, done or failed.",
  },
  {
    label: "Tools",
    title: "Real tools, in a sandboxed folder",
    body: "read, write, edit, grep, find, ls and bash, refused for any path outside the session’s workspace.",
  },
  {
    label: "Thinking",
    title: "The model’s reasoning",
    body: "Pick how hard the model thinks, then read its reasoning before the answer.",
  },
  {
    label: "Memory",
    title: "Compaction you can see",
    body: "When a conversation gets too long, older turns are summarized, and a marker shows where.",
  },
  {
    label: "Skills · MCP",
    title: "Skills and MCP servers",
    body: "Give the agent instructions it loads on demand, and connect outside tools through MCP.",
  },
  {
    label: "Cost",
    title: "Tokens and dollars",
    body: "What each reply cost and the running total for the session, so the price of a loop is never a mystery.",
  },
];

// Questions people ask a search engine or an AI assistant; answered in plain words, also sent as FAQPage JSON-LD.
export const FAQ = [
  {
    question: "What is an AI agent?",
    answer:
      "An AI agent is a language model in a loop with tools. Instead of answering once, the model can ask to use a tool, such as reading a file or running a command, see the result, and decide what to do next, until the task is done.",
  },
  {
    question: "How does an agent loop work?",
    answer:
      "The agent sends the conversation and a list of tools to the model. If the reply asks for tools, the agent runs them, adds their results to the conversation and calls the model again. When a reply asks for no tools, the loop stops and that reply is the answer.",
  },
  {
    question: "What is a tool call?",
    answer:
      "A tool call is a structured request from the model, such as a tool name and its arguments, that the agent program runs on the model’s behalf. The model never runs anything itself; it only asks, and the agent decides what is allowed.",
  },
  {
    question: "Is agentspace built on an agent framework?",
    answer:
      "No. The agent loop, tool calling, model providers, skills and sessions are written from scratch in Python, with open-source agents such as pi and opencode as reference designs, so every part can be read and understood.",
  },
  {
    question: "Which models does agentspace work with?",
    answer: "Any model behind an OpenAI-compatible API. OpenRouter works out of the box, so you can switch between hundreds of models.",
  },
  {
    question: "What will agentspace teach next?",
    answer:
      "Agents come first. The same learn-by-watching approach is planned for computer networks and system design, each as its own topic.",
  },
];
