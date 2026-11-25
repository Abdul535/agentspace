# Coding standards

Every line is **production** code: thousands of users will run it, at the same time, on data you never saw, and someone gets paged when it breaks. Write it for the person who changes it next year too: a human or an agent reading it cold, with no memory of today. A change is done when your report answers these four questions, each answer pointing at code or a test:

1. **Sad path**: what happens when the input is wrong, a call fails, or the user leaves halfway?
2. **Race**: what happens when two of these run at the same moment, on two machines?
3. **Proof**: which test goes red if this behaviour breaks, and have you seen it go red?
4. **Findability**: where does the next reader look for this, and will they find it on the first guess?

## Vocabulary

Use these terms exactly, so design discussions stay precise.

- **Module**: anything with an interface and an implementation, at any scale: a function, a class, a package, a slice across tiers.
- **Interface**: everything a caller must know to use the module correctly: the signature, plus invariants, ordering constraints, error modes, required configuration and performance characteristics.
- **Implementation**: the code inside a module.
- **Depth**: leverage at the interface; how much behaviour a caller or test can exercise per unit of interface it has to learn. **Deep** means a lot of behaviour behind a small interface; **shallow** means the interface is nearly as complex as what it hides. Depth is measured in leverage, not in lines of code: padding the implementation never makes a module deeper.
- **Seam**: a place where behaviour can change without editing that place; the location where a module's interface lives.
- **Port**: an interface the module declares at a seam for something it depends on, so the dependency is passed in rather than created inside.
- **Adapter**: a concrete thing that satisfies an interface or port at a seam (a production client, an in-memory fake).
- **Leverage**: what callers get from depth: more capability per thing learned, one implementation paying back across many call sites and tests.
- **Locality**: what maintainers get from depth: change, bugs, knowledge and verification concentrate in one place. Fix once, fixed everywhere.

## Modules

**Deep module** = small interface + lots of implementation:

```
┌─────────────────────┐
│   Small Interface   │  ← Few methods, simple params
├─────────────────────┤
│                     │
│  Deep Implementation│  ← Complex logic hidden
│                     │
└─────────────────────┘
```

**Shallow module** = large interface + little implementation (avoid):

```
┌─────────────────────────────────┐
│       Large Interface           │  ← Many methods, complex params
├─────────────────────────────────┤
│  Thin Implementation            │  ← Just passes through
└─────────────────────────────────┘
```

- Build **deep** modules. For every interface ask: can it have fewer functions, simpler parameters, more complexity hidden inside? Fewer functions mean fewer tests; fewer parameters mean simpler test setup.
- Depth belongs to the interface, not the implementation. A deep module can be built from small swappable parts inside; they stay private to it.
- Run the **deletion test** on every new module: delete it in your head. If its complexity would reappear across its callers, it earns its keep; if it would vanish, it is a pass-through, so fold it into its caller.
- One module, one job, named after that job in the domain's own words. A reader finds code by guessing the file name from the concept.
- **Accept dependencies, don't create them.** A module receives what it talks to (database handle, HTTP client, clock, payment gateway) as a parameter, so a test can pass in a fake.

  ```typescript
  // Testable: the gateway is passed in
  async function processOrder(order: Order, paymentGateway: PaymentGateway): Promise<Receipt> {
    /* … */
  }

  // Hard to test: the real gateway is created inside
  async function processOrder(order: Order): Promise<Receipt> {
    const gateway = new StripeGateway();
    /* … */
  }
  ```

- **Return results, don't produce side effects.** Decisions return a value; the caller applies it.

  ```typescript
  // Testable: returns the decision
  function calculateDiscount(cart: Cart): Discount {
    const amount = cart.items.length >= 3 ? 5 : 0;
    return { amount };
  }

  // Hard to test: mutates its input
  function applyDiscount(cart: Cart): void {
    const discount = cart.items.length >= 3 ? 5 : 0;
    cart.total -= discount;
  }
  ```

- Dependencies point one way, from entry points (routes, handlers, UI) toward the core domain. An import cycle means the cut is in the wrong place.
- Aim for **locality**: a bug fix lands in one module and its tests. When the same kind of change keeps touching the same scattered files, report that cluster as a deepening candidate for its own change.

### Seams

- One adapter is a hypothetical seam; two adapters make a real one. Introduce a seam only where something actually varies, typically production plus a test fake.
- A module can have internal seams, used by its own tests, besides the external seam at its interface. Keep internal seams out of the interface.
- Choose the seam by the kind of dependency:
  1. **In-process** (pure logic, in-memory state): no seam; merge and test through the interface directly.
  2. **Local-substitutable** (a database or filesystem you can run locally): keep the seam internal and run tests against a local instance of the same engine and version as production.
  3. **Remote but owned** (your own services over the network): define a port at the seam; production uses a network adapter, tests use an in-memory adapter, and the logic stays in one deep module.
  4. **True external** (third-party APIs you don't control): inject it as a port; tests provide a fake adapter.

### Design it twice

Your first idea for an interface is unlikely to be the best. When a change adds an interface that is costly to change later (a public API, a persisted format, a widely imported module), offer to design it three radically different ways first:

1. **Frame the problem space**: the constraints any interface must satisfy, the dependencies it relies on and which seam category each falls into, and a rough code sketch that makes the constraints concrete (not a proposal).
2. **Design three or more interfaces**, each in a fresh context (a separate sub-agent when you have them, so one design cannot anchor the next), each under a different constraint:
   - Minimise the interface: 1–3 entry points, maximum leverage per entry point.
   - Maximise flexibility: support many use cases and extension.
   - Optimise for the most common caller: make the default case trivial.
   - Design around ports and adapters for cross-seam dependencies (when they exist).

   Each design states:
   1. Interface (types, methods, params, plus invariants, ordering, error modes)
   2. Usage example showing how callers use it
   3. What the implementation hides behind the seam
   4. Dependency strategy and adapters
   5. Trade-offs: where leverage is high, where it's thin
3. **Compare and recommend**: lay the designs side by side and contrast them by **depth**, **locality** and **seam placement**. Pick the strongest and say why; when parts of different designs combine well, propose a hybrid. Commit to one recommendation rather than a menu.
4. **Let the user choose**: show the user the designs and your pick, and build once they choose.

## Types and errors

- Make illegal states unrepresentable: a discriminated union or enum instead of booleans and optional fields that must agree with each other.
- Validate at the boundary where data enters (request, file, queue, third-party response, model output), then trust the typed value inside.
- Fail loudly and early. Raise a specific error carrying the ids and state that explain it, never secrets or personal data. Users get a safe message; the detail is logged once, where the error is handled. Each catch names what it catches and either handles it completely or re-raises with context.
- Errors are part of the interface: the caller knows what can fail and maps each case to a visible outcome (a status code, an error state in the UI).

## Sad path

Walk this list for every input and every call that leaves the process, and handle each case that applies:

- **Data**: empty, one, many, huge; missing or null; duplicate; out of order; unicode and whitespace; malformed.
- **Network**: every outbound call has a timeout, and you expect slow, partial and cut-off responses. Retry only transient failures (timeouts, connection errors, 429, 5xx), honouring `Retry-After`, with backoff, jitter, an attempt cap and a total that fits within the caller's deadline, at one layer only. Retry a non-idempotent call only with an idempotency key; without one, a timeout leaves the outcome unknown, so look it up before retrying.
- **Third parties**: rate limits, changed or malformed responses, outages, quota exhausted.
- **User**: cancels, closes the tab, double-clicks, reloads mid-operation, uses two tabs or two devices at once.
- **Deploy and crash**: the process is killed mid-operation. Leave state that a restart can resume or clean up. An email, message or event that must follow a write is recorded in the same transaction and sent after commit.

## Concurrency

Assume every handler runs twice at once, on two different machines.

- Shared state lives in the database or another shared store. Process memory is a cache that can vanish on any restart.
- A read-modify-write is one atomic statement, a row lock, or a conditional update (`WHERE version = :seen`) whose affected-row count you check: zero means another writer won. Check-then-act across two steps is a race.
- Make writes **idempotent**: a unique constraint or idempotency key makes a retry or double-submit return the first call's result. Handle the conflict and return the existing record.
- Let the database enforce invariants with unique, foreign-key and check constraints. The application check gives a friendly error; the constraint is the truth.
- Keep async event loops free: async I/O for I/O, a worker thread or process for blocking or CPU-heavy work.
- Every background task has an owner that awaits or cancels it, and cleanup lives in a `finally` or scoped resource so cancellation releases locks, connections and files.
- Events carry an id and a sequence number; consumers tolerate duplicates, gaps and reordering, and can replay.
- In UIs, the response to the latest request wins and responses to superseded requests are ignored; a pending action is disabled or deduped; an optimistic update rolls back visibly on error.

## Scale and operations

Size every path for thousands of users and years of data.

- Growing lists page by cursor over a unique key such as `(created_at, id)`. Queries on growing tables use an index, checked in the query plan at realistic row counts. Load related records in one query, not one per item.
- Every input, upload, stream, loop and retry has an upper bound: max length, size, steps, time and cost per user.
- Every data access is scoped to the caller's permissions; a test proves one user cannot read or change another user's data.
- Input reaches SQL, shell and HTML only as parameters or through the library's escaping. A path or URL from input is checked against what it may reach: inside its root directory, no private or internal addresses.
- Secrets and personal data come from their proper store and stay out of code, logs, errors, traces and responses.
- Log and trace with the ids that let you follow one request end to end, so a bug report becomes a lookup instead of a guess.
- A slow or failing dependency degrades one feature with a clear message; the rest of the system keeps working. A failure users would notice also moves a metric that alerts.

## Tests

- The **interface is the test surface**: tests cross the same seam callers do and assert on observable outcomes, so a refactor that keeps behaviour keeps them green. A test that must change when only the implementation changes is testing past the interface; rewrite it at the interface.
- When you deepen a cluster of modules, replace its old unit tests with tests at the new interface instead of keeping both layers.
- Every sad-path case and race you handled gets a test.
- Test a race against the same database engine as production, each call on its own connection: pause the first call after its read with a test hook while the second runs, and confirm the test goes red with the fix removed.
- Name a test after the behaviour it proves: `second_approval_of_same_request_is_ignored`.
- A bug fix starts with a test that goes **red** on the bug, then turns green with the fix.

## Readable for humans and agents

- One name per concept across database, API and UI; reuse the domain's word everywhere it appears.
- Functions read top to bottom, with early returns instead of deep nesting. Split out a helper when it hides a named decision, and keep it private to its module.
- Explicit wiring: dependencies passed in, each constant defined once in the module that owns it, plain imports. A reader or agent can follow every call with search and go-to-definition.
- Comments explain why, not what: a one-line purpose for each module and function, and a one-line reason before any non-obvious block.
- When you knowingly defer something, leave a `shortcut: <the limit>, <when to upgrade>` comment so it is searchable later.

## Long term

- When unsure between two designs, pick the one that is easier to undo; write down the reasoning for any hard-to-undo choice.
- Change schemas, APIs and stored formats across separate deploys, each compatible with the previous code so a rollback is safe: add the new shape, write both and backfill in batches, switch reads, then remove the old. Build indexes concurrently and set a short lock timeout on migrations.
- Add a dependency only when it saves real work: it becomes code you own.
- Delete dead code, unused flags and finished `shortcut:` notes in the same change that makes them dead.
