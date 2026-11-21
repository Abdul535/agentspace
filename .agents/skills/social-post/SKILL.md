---
name: social-post
description: Turn things the owner learned while building agentspace into ready-to-post X threads and LinkedIn posts, three at a time, each a 3-card carousel (problem → obvious fix and why it breaks → what actually works) in the "build log" style. Use when the owner asks for posts, tweets, LinkedIn posts, a carousel, "share this learning", or "make a build log".
---

# Social post pipeline

Make posts in **batches of three**. All content lives in **`~/Desktop/oss/content/`** (its own local git repo, outside every worktree): never write posts inside the agentspace repo. One post = one folder in `~/Desktop/oss/content/posts/NNN-<slug>/` with three cards (HTML + PNG), `x.md` and `linkedin.md`. Number on from the highest existing folder. `001`–`003` are the worked examples: copy their shape.

## The format: problem → obvious fix → real fix

Every post has exactly three cards. The aim is to make the reader think "I'd do X", then show why X breaks, then show the answer:

1. **The problem** (`<b>?</b>` chip). A relatable pain in plain words ("My agent made its tools wait in line."), a diagram of it, and the note **"Before you swipe: How would *you* fix it?"** (`p.ask`).
2. **The obvious fix** (`<b class="bad">✗</b>` chip). The headline quotes what most people would try (“Just catch everything.”), a serif "…right?" line, the naive code (≤ 4 lines, `.code`), then three `.cons` rows: what breaks, each with a short serif reason. The note gives proof (a test result, a number). No TypeScript comparisons.
3. **What actually works** (`<b>✓</b>` chip). Two-line headline with the key half highlighted (`.hl`), credit the reference ("borrowed from the pi agent"), a diagram of the mechanism, optionally the real code. The note is the result or the rule to remember.

## 1. Pick three learnings

Offer the owner candidates and let them pick three, or pick when they say "you choose". Good sources, best first:

- `.agents/learnings/*.md`: symptom → cause → fix stories.
- `.agents/adr/`: each ADR's "Alternatives considered" section is a ready-made "obvious fix".
- `.agents/learning-progress.md`: what they got wrong in a prediction is what readers will get wrong too.
- Recent merged PRs (`git log dev`).

Skip anything already posted (check `~/Desktop/oss/content/posts/`). One post teaches **one** idea. The "obvious fix" must be something people really try, ideally what the owner tried or predicted.

## 2. Check the facts

Read the code each learning points at. Every claim, number and snippet must be true in the repo today, and claims about Python or libraries must be true too (e.g. `gather` *does* cancel its children when it is itself cancelled, so don't claim it doesn't). Don't invent stats. If the repo isn't public yet, don't link to it.

## 3. Outline, then confirm

Show the owner all three outlines in one `send_to_user` message: per post, the three headlines plus the cons. Wait for an OK unless they already said to go ahead.

## 4. Design the cards

- Copy a card from an existing post. All the styling comes from `~/Desktop/oss/content/brand/card.css`; don't add colours or fonts.
- Look: warm paper, ink, **one** yellow accent (fills only, never yellow text on paper; use `--yellow-ink`), Geist bold headline, Instrument Serif italic subtitle, Geist Mono uppercase labels, square corners, 2px ink lines.
- Diagrams are inline SVG: boxes, lanes, bars, trees. Yellow marks what the card is about.
- Header `Build log — NNN`; footer is the label "Agents from scratch, in Python" (no name) plus the page counter `1 / 3 →`, `2 / 3 →`, `3 / 3`.
- Plain words: a recruiter who doesn't code should get the headline; an engineer should learn from the diagram.

Render, then **look at every PNG** (Read it) and fix overlaps, cut-off footers, text off the edge, and diagrams that say something false (e.g. a tree that puts siblings under the wrong parent):

```bash
~/Desktop/oss/content/brand/render.sh ~/Desktop/oss/content/posts/NNN-<slug>
```

## 5. Write the text

In the owner's voice: first person, honest, learning in public. No TypeScript comparisons or mentions. Never overclaim; credit references by name (pi, opencode).

- `x.md`, three parts with the same arc as the cards, each ≤ 280 characters. Count them with the Node snippet below.
  - `# Post`: the problem + "How would you fix it? Guess before you swipe 👇", with the cards attached.
  - `# Reply 1`: the obvious fix + ✗ list.
  - `# Reply 2`: what works, optionally ending with a question.
- `linkedin.md`, 200–320 words: a hook question, the problem in plain words, the obvious fix with ❌ rows, what works with numbered or ✅ rows, a one-line lesson, a question to invite comments, and 4–6 hashtags.

```bash
node -e 'const t=require("fs").readFileSync(process.argv[1],"utf8");for(const p of t.split(/^# /m).slice(1)){const[h,...b]=p.split("\n");console.log(h,[...b.join("\n").trim()].length)}' ~/Desktop/oss/content/posts/NNN-<slug>/x.md
```

## 6. Hand off

Show the owner all nine PNGs and point to the text files. Suggest posting one every 2–3 days. They post by hand; nothing is published automatically. Commit the new posts in `~/Desktop/oss/content` (local only, no remote).

## Content calendar

The schedule lives in `~/Desktop/oss/content/calendar.md`. After a batch is drafted, fill its rows (title, folder, status `drafted`) and take its topics out of the backlog. Dates are every 2 days, weekends included; LinkedIn at 8:00 pm IST (9:30 am ET), X at 10:00 pm IST (11:30 am ET). Mark rows `scheduled` or `posted` only when the owner says so.
