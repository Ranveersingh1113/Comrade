# Frontend design inspiration — 21 Jul 2026

Scheduled-task output. Method: applied the [Impeccable](https://github.com/pbakaus/impeccable) design skill's guidance (v3.9.1, fetched from GitHub — skills can't be installed in a scheduled session) and the [ponytail](https://github.com/DietrichGebert/ponytail) philosophy (smallest change that works — it's a YAGNI coding skill, also not installed here). Browser screenshots were saved to your **Downloads** folder with `ss_*` filenames.

## Why the current frontend feels off (diagnosis)

Your tokens (`src/index.css`) are a warm cream canvas (`--canvas: #f6f2ee`, `--paper`, `--card-alt`) with terracotta/plum/sage accents, Instrument Serif display, and hard 4px offset shadows (neubrutalist).

Impeccable's current guidance flags this almost verbatim: **"The cream / sand / beige body bg is the saturated AI default of 2026"** — token names like `--paper` are called out as tells, and hard-offset-shadow cards are part of the same look. The palette isn't ugly; it's *generic now*. That's likely the itch. Second flag: several text tokens (`--muted: #8e8a96`, `--faint: #a9a5b0`) on tinted near-white will fail the 4.5:1 body-text contrast rule — "muted gray on tinted near-white" is Impeccable's #1 readability failure.

## Directions worth stealing from (screenshots in Downloads)

**1. Basecamp 5** — basecamp.com · `ss_2196dk6yb`
Most relevant: same feature set as Comrade (message board, chat, tasks, docs, schedule) rendered as a dark, warm, calm product shell. Shows how "warm and friendly" survives without cream — warmth via type, illustration, and muted card tints on dark.

**2. Twist** — twist.com · `ss_5573d5ni4`, `ss_1041hcksv`
Async team chat with a *committed* color strategy: saturated teal carrying large surfaces, serif display type, generous whitespace in the thread UI. The closest evolution of Comrade's editorial register — keep the serif, swap cream-default for a color the brand owns.

**3. Linear** — linear.app · `ss_5352w7n6c`
The restraint pole: near-black, one typeface, hierarchy purely from size/weight/spacing. If Comrade's problem is "too much decoration," this is the reference for deleting it.

**4. Are.na** — are.na · `ss_80087bhuo`
Stark editorial minimalism, near-zero chrome, content is the interface. Good reference for Wiki/Documents screens.

**5. HEY** — hey.com · `ss_2613mkpxf` (and Amie, amie.so · `ss_7221uh3qt`)
The loud pole: heavy grotesque type, saturated color, personality-first. Note HEY uses gradient text — an Impeccable "absolute ban" — steal the confidence, not that.

**6. Galleries for ongoing browsing** — recent.design (ex-godly.website) · `ss_1492021o4`, `ss_1648ycnyt`. Filter by "Interface." Also land-book.com and mobbin.com (app UI patterns, login required).

## Smallest-change recommendation (ponytail mode)

The token architecture is fine — change values, not structure:

1. Pick a color strategy first (Impeccable's ladder: restrained / committed / full / drenched). For Comrade a **committed** move fits: let terracotta or plum carry 30–60% of the shell (sidebar, headers) instead of cream carrying everything.
2. Replace `--canvas` cream with either a true off-white (chroma ~0) or a dark warm neutral (Basecamp direction). Kill the `--paper`/`--card-alt` band.
3. Darken `--muted`/`--faint` until body/placeholder text hits 4.5:1.
4. Swap 4px offset shadows for 1px full borders + subtle tint; keep Instrument Serif — it's the identity worth preserving.
5. One screen first (GroupRoom), then propagate tokens.

## Notes on the requested skills

- **impeccable**: not installed in this environment; I fetched and applied its SKILL.md directly. To install for real sessions: `npx impeccable` or add from github.com/pbakaus/impeccable via Settings → Capabilities.
- **/ponytail**: not installed; it's a code-minimalism (YAGNI) skill, not a design skill — applied its philosophy to keep recommendations minimal. Install from github.com/DietrichGebert/ponytail if wanted.

## Sources

- https://github.com/pbakaus/impeccable · https://impeccable.style/
- https://github.com/DietrichGebert/ponytail
- https://basecamp.com · https://twist.com · https://linear.app · https://www.are.na · https://www.hey.com · https://www.amie.so · https://recent.design
