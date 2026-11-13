You are building ONE design-* site for the MegaWoB web environment: `{site}` (task `{task}`, scenario
`{scenario}`). Your working directory is the site folder; you may only write inside it.

Goal: make the page look as faithful to the attached design image as you can, while it keeps working as a task.
Time budget: about {minutes} minutes; keep the site rendering at all times, since the builder stops you at the limit.

Design image: attached (native size {width}x{height}). What it was drawn to show:
```
{intent}
```

Files in your site folder:
```
{file_map}
```

Tools (MCP server `megawob-site`): `render` shows the design image beside your render, or other views of the
page, with any page, console and layout errors; `check` runs the lint and the oracle checker. Their descriptions
list every option.

# How a MegaWoB design site works (read this instead of the kit, checker or docs)

A site lives in `html/sites/<site>/` and is loaded by
`html/play.html?task=..&site=..&widget=design&scenario=..&seed=N`. The kit (`html/kit/`) builds the episode and the
task logic (`html/tasks/<task>/logic.js`) samples the goal, scores it and drives the oracle. Your site only supplies
DOM, CSS and visuals. Terms: a *task* is one kind of job (set a slider, use an email app); a *scenario* is a sub-case
of a task; a *site* is a page design; a *widget* is how one site draws one task's interactive part; the *frame* is
the UI the site draws around the stage; *randomization parameters* are the per-episode settings drawn from the seed
(instruction display, finish by message, start state, popups, ...).

Files you own (and the only files you may write):
- `site.js`: a design on a site template, `env.site({name, template:'site/<name>', design})` (set the template's
  tokens and replace its parts; `html/templates/README.md`, never copy the template), or an older whole
  frame, `env.site({name, styles:['site.css'], scripts, frame(ctx), overlay(ctx)})`. Usually starts with a
  config object (`C`/`D`/`cfg`) holding design tokens: colors, fonts, layout boxes, labels. `frame(ctx)` builds
  the page frame (menu bars, title bars, sidebars, the task note where `ctx.display` says, and an end control that
  calls `ctx.submit()`; none in finish mode, `ctx.finish`; see "Episode settings" below). `overlay(ctx)` builds
  the popups the kit asks for (cookie, toast, chat, promo, banner), each with a visible way to dismiss it.
- `widgets/<task>.js`: `env.widget({task, site, name:'design', split:'train', scenarios:[...], template, design})`,
  a design on the task's shared widget template (`html/templates/<task>/<name>.js`; the spec is
  `html/templates/README.md`): set the template's tokens and replace the parts your design draws
  differently (`parts: {name: (base, S) => replacement}`); never copy the template into the site. The template's
  `render(ctx)` builds the task's interactive part inside `ctx.stage` from `ctx.size` and its `locate(target)`
  answers the oracle; a part you replace keeps those duties. Restyle the template's classes in `site.css`.
- `site.css`, plus any images under the site dir (`assets/`).
- `manifest.json` (already correct) and `KIT_REQUESTS.md` (write only if the kit truly blocks you).

The scaffold you start from is a working clone of a sibling site, so the wiring, model updates and `locate()` already work.
**Your job is the look.** Restyle and re-layout so the page matches YOUR design image: window or page
structure and positions, frame (menus, toolbars, title bars, tabs, status bars), typography (typeface, size,
weight), colors, borders, shadows, iconography (inline SVG or CSS; no external fonts or network), density and
spacing, decorative elements, and any obstruction or tour shown in the design image (with a dismiss control). Keep
the existing task wiring working while you change markup. Don't strip out logic you don't understand. Move it
or re-skin it instead.

The task's widget (the table, list, form, board or editor the task acts on) comes from the task's template in a
neutral modern style: restyle it into the design image's era and toolkit as well, not only the frame around it. A
modern widget inside a period design (rounded chips, avatar initials, pill buttons, light cards, two-line cells,
a search or filter bar the design image does not have) is the most common reason a built site fails review, and
fidelity 4 needs the widget to match as well as the frame.

Keep the wiring intact (checkers replay seeds, so these break the task silently):
- Keep every `ctx.rng` call, in the same order. Removing or adding a draw shifts every later seeded value.
- Keep `env.widgetDef.clear`, `locate`, the keys of the returned `anchors`, `sync`, and any wrapper around
  `env.widget` (`registerWidget`, oracle helpers). Change their markup and styles, not their logic.
- Don't call `ctx.update` during render or force an initial state the task didn't set (a popup opened, a value
  pre-filled). Don't hardcode the task text: the instruction comes from `ctx`.
- Page copy never names the end control ("then click Submit"): its label varies per episode and finish mode has
  none. Drop such sentences from the design image's help text; `megawob.lint` fails them (`names-end`).
- Widget code reads the task only through `env.taskDef.helpers.<name>` and contract hooks (`plan`, `error`, ...):
  other task members read as undefined (tasks are closed; each logic.js header lists its helpers).
- List every change you made to wiring in `deviations`.

If the design image shows an obstruction (cookie or consent dialog, toast, chat bubble, promo or newsletter modal,
top banner, tip window or tour), build it in `overlay(ctx)` for the closest kind and check it with
`render(popups="<kind>")` (also with `crop`): plain renders never show it. That is not a kit request.

If the design image shows something else the kit can't produce from your site (a panel open on load that the task
never shows, a shape it never samples, cards starting outside their columns), don't fake it. Add one line per
need to `KIT_REQUESTS.md` and list it in `remaining_differences`. First check the task's `logic.js` header
(`html/tasks/<task>/logic.js`, read-only) and the traits table in `docs/architecture.md`: many such needs are already
met by a widget `traits` entry or by the `startState=scene` mode (a documented mid-task state, e.g. a menu open or a
query already submitted; only for tasks listing `scene` in their start modes). Preview it with
`render(params={"startState": "scene"})`.

Never request (and never do in the widget) a change to what the task means, even if the design image differs. List it
in `remaining_differences` with cause `seed` and move on:
- Renaming or removing controls, actions, folders, tabs or options that instructions name or scoring checks
  (menu actions like Follow or Hide, mail folders like Archive, button verbs). Instructions ask for them by name.
- Dropping a required step: an in-app Sign in / Save / Apply / Send the task requires, or the episode's own
  end control (outside finish mode). Keep the control even if the design image has none.
- Removing the instruction, decoys or distractors the task generates, or fixing seeded content to the
  design image's words or values.
- Building an always-on obstruction in `frame()` or patching `env.oracle` to dismiss it: declare it with
  `initialOverlay: {kind}` in `site.js` and draw it in `overlay(ctx)`.

## Episode settings your frame must handle

The same site is trained with per-episode randomization parameters the design image shows only one setting of. A
policy must not learn "the goal is printed top-left" or "done = the blue button, bottom-right", so every setting
below has to look like a natural part of YOUR design, and none may break the layout.

- **Instruction display, `ctx.display`** (parameter `instructionDisplay`): `header` | `panel` | `toast` | `hidden`
  (training mix about 45/20/10/25).
  - `header`: a task note in the page's own header or title area.
  - `panel`: a side card in the design's style (a ticket, an info box, a sidebar section), clearly different from
    the header form.
  - `toast`: a small notification the design would plausibly show (corner card, snackbar, status-bar message).
  - `hidden`: no instruction anywhere on the page; the agent gets the goal from outside it. Don't leave an empty
    box where the note was.
  - In every mode, the note must wrap long instructions (2-3 lines; some templates are long) instead of
    overflowing. It must never cover the task stage, the end control or a click target, and the stage must still
    fit. If a panel or toast can't fit at a small viewport, fall back to the header form at that size, as the
    `base` site does.
- **Finish mode, `ctx.finish`** (parameter `finishByMessage`, about 20% of episodes): render no end control and
  return `submit: null`; the agent ends the episode itself with a message. The footer or toolbar the button lived in
  must still look complete (no empty slot, no orphan divider).
- **End control variation** (when not in finish mode), picked per episode:
  - Placement: at least 3 forms that fit the design, chosen from a header/toolbar button, a footer or form-footer
    button, a plain text link ("Done", "I'm finished"), an icon button with a tooltip (`title` and `aria-label`),
    and on mobile-style designs a bottom bar.
  - Label: from `ctx.content.END_LABELS` (Submit, Done, Finish, Continue, Save, OK, Confirm, Apply, ...).
  - Keep the design image's own placement and label the most common pick (about half of episodes).
  - Whatever is rendered must call `ctx.submit()`, sit inside the viewport at every training size, and stay
    uncovered by the task note.
- **Decoy end controls**: in about 30% of episodes, add one inert button whose label is another end word from
  `END_LABELS` (never this episode's own label), e.g. "Save" in the toolbar while the end control says "Done".
  Optionally add 0-2 inert filler buttons from `ctx.content.DECOY_LABELS` (Help, Save draft, ...). Decoys must
  never call `ctx.submit()`; log clicks with `env.log('decoy', {label, collide: true})` (`collide` only for the
  end-word kind). Don't touch in-app buttons the task itself renders ("Send", "Buy", "Apply filters"): they
  are part of the task. Give decoys neutral DOM names: no class, id, `data-*` name or `data-role` value with
  `decoy`, `collide` or `collision` in it (the shared frame uses `ref-xtra`), or a DOM agent skips them for free;
  `megawob.lint` fails them (`decoy-marker`). On `site/base`, call the template's parts instead of copying
  its blocks: `S.chromeBrand(_c, header)`, `S.chromeSidebar(_c, body)`, `const { collide, cr } =
  S.chromeCollide(_c, label)`, `S.chromeAlign(_c, stage)`.
- **Randomness for all of the above**: use forked streams, e.g. `const r = ctx.rng.fork('chrome/end')`, never new
  draws on `ctx.rng` itself, so existing frame draws keep their values for a seed (the stream names keep their
  old `chrome/` prefix; it salts the seeds). The `base` site's code in
  `html/templates/site/base.js` (`endLabel`, `chrome/collide`) shows one way to do it; reading it is allowed.
- **Check it** with `render(view="modes")`, a 2x2 grid at 1024x640 (header, panel, toast, hidden + finish by
  message), at two or three seeds (`seed=3`, `seed=7`): the end control's placement and label should change between
  seeds. `params={"instructionDisplay": "<mode>"}` and `params={"finishByMessage": "1"}` force one setting and
  combine with `crop` and `view="small"`. Renders report `ERROR layout: ...` when the end control is off-screen or
  covered, or the stage runs past the viewport. Fix every such line.

Content is seeded: names, rows, values and the instruction text come from the task and vary by seed, so they
won't match the words in the image. Match the visual treatment, not the literal text. Static frame text
(app names, column headers, decorative captions) should match the design image, except the labels of controls the
instructions name (above).

Rules (the page must keep working as an RL environment):
- Deterministic: randomness only from `ctx.rng`. No `Math.random`, `Date`, network, timers or CSS
  animations/transitions that change pixels after load.
- Fits every training viewport (1024x640 up to 1536x864, plus the design image's native size). Size from
  `ctx.size` and relative units, never the window. No page scroll unless the widget already declares `scrollable`.
- Keep the end control, the task note and any recovery or back controls visible and not covering click
  targets, in every instruction mode. Anchors must not move after render.
- No native `<select>` or date/time/color pickers as the interaction path. Build them from DOM.
- Use `ctx.util.el(tag, cls, parent, text)` for elements if the scaffold does. Inline SVG is fine for icons.

Reading and editing:
- Read only your site's files, the design image, the design-sheet crops and spec the prompt lists (if any),
  the images `render` returns, the task's `logic.js` header and, for the end-control and decoy pattern,
  `html/templates/site/base.js`. The brief above replaces the kit, other sites and the sitegen source; don't open
  them. Fonts: use system stacks (serif, sans-serif, monospace, Georgia, Arial, Courier New, Segoe UI-like
  fallbacks), don't search for font files.
- Don't edit `manifest.json`, and don't run git: the builder does the bookkeeping.
- Make targeted edits to existing files instead of rewriting them whole (no `cat >`/heredoc rewrites). A patch
  edits each file once: put all hunks for a file under one file header. Read big files in ranges.

Workflow:
1. Look at the design image (attached) and read its intent text in the prompt.
2. `render()` shows the design image beside your render. Write down every difference you see, region by
   region (frame, task widget, side panels, popups), then fix a whole batch in one round: all the edits for
   one or more regions (layout, sizes, colors, typography, icons) in one patch per file, and only then render again.
   Don't render after each small edit: a round should change a lot. Use `crop=[x, y, w, h]` (design image pixel
   coords) to check details such as fonts, icons and borders, at least twice on the densest regions.
   Expect 4-8 rounds.
3. Build the episode settings (instruction modes, finish mode, end-control variation, decoys) in the design's
   style once the frame matches.
4. Before finishing, render `view="small"` (1280x720 and 1024x640), `seed=3`, and `view="modes"` at two seeds
   to make sure nothing overflows or breaks. Fix any ERROR lines.
5. Last, call `check()` (the lint plus the oracle at three viewports) and fix what it prints until it says
   `check passed`; call it again after any later edit. The final gate runs the same checks on more seeds and every
   training viewport, and a failed gate costs a whole extra round. When it fails, look before editing: reproduce
   the printed seed and parameters with `render(seed=N, params={...}, play=K)` (K = actions before the stuck one;
   params such as `{"popups": "chat"}` or `{"pageFaults": "revert"}`) to see what the stuck click hits, and change
   only what it shows; don't call `check()` again on a guess.

Don't write generator scripts, don't run `megawob.check`, BrowserGym, Docker or a browser of your own (the
sandbox blocks them; `render` and `check` run outside it), and don't browse the kit or other sites unless something
above is unclear. Each look at a large file and each render costs time: batch your edits, then render to check the
batch. Every tool call re-sends your whole context, so make few, full calls, and find code with `rg -n` then read
only the lines you need (at most ~150 at a time), never whole files.

Reporting: `fidelity` 5 = a viewer would mistake the render for the design image at a glance; 4 = same layout,
palette, typography and frame, only small details differ; 3 = recognisably the same design but with missing or
misplaced elements; 2 = theme and palette only; 1 = unrelated. Each `remaining_differences` item names where and
what, e.g. "toolbar: design image has 9 distinct icons, render uses 5 generic squares", not "minor spacing", and
its `cause`: `site` if editing your site files could close it (layout, sizes, row count, density, scrolling,
empty space, icons, colors), `kit` if it needs a kit or task change (listed in `KIT_REQUESTS.md`), `seed` if it
comes from seeded content (names, values, how many items the task generates; how that content looks, its fonts,
row height and cell, table or card styling, is `site`), `font` if only the exact
typeface differs. Fix every `site` difference before you finish; leave one only if time runs out.

When you are done (or out of ideas), reply with only one JSON object (no code fence, no other text) that validates
against this JSON Schema, and be honest about `fidelity`:
{schema}
