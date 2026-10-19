# Skin: navy-slate dark UI (Better Stack style): Phase 1

Status: **proposal, awaiting approval (2026-10-19).** Skin only: no layout, navigation, routing, data, copy or behaviour changes.

## 1. Inventory (today)

| What | Where | Count |
|---|---|---|
| Colour tokens | `services/web/src/styles.css` `:root` (dark) and `:root[data-theme="light"]` | 31 dark, 21 light |
| Hardcoded colours in CSS, outside the token blocks | `styles.css` | **23** (8× `#fff`, 6× `#3fb68b`, 4× `rgba(0,0,0,…)` shadows, 5 one-offs) |
| Hardcoded colours in TS/TSX | `Dashboards.tsx` (10, series palette), `QueryBuilder.tsx` (4, palette), `icons.tsx` (3, logo), `Slos.tsx` / `Alerts.tsx` / `Insights.tsx` / `synthetics/shared.tsx` (6, `var(--ok, #3fb68b)` fallbacks) | **23** |
| **Total hardcoded colours** | | **46** |
| `--ok` (success) | used 6× with a fallback, **never defined** as a token | |
| Radii | `var(--radius)` 26×; literals 2px 7×, 3px 8×, 4px 21×, 6px 8×, 8px 8×, 9px 3×, 10px 5×, 999px 5×, 50% 6× | 10 distinct |
| Shadows | 4 literal `box-shadow`s with `rgba(0,0,0,…)` | 4 |
| Fonts | `--font-sans` (Inter → system UI) and `--font-mono` (JetBrains Mono → system mono); neither webfont is loaded, so system fonts render | 2 tokens |
| Inline `style={{…}}` | 330 in TSX, mostly layout or `var(--…)` | |

**Contrast problems today (WCAG AA, 4.5:1 for text):**
- faint text `--text-3`: 3.1–4.1:1 in dark, 3.2–3.5:1 in light;
- white on the primary button `--accent-2`: 3.6:1 in dark.

## 2. Proposed tokens (same names, so most of the UI follows automatically)

| Token | Dark | Light | Notes |
|---|---|---|---|
| `--bg` | `#111726` | `#f4f6fa` | page |
| `--surface` | `#171e2f` | `#ffffff` | panels, sidebar |
| `--surface-2` | `#1d2538` | `#f7f8fb` | panel headers, inputs |
| `--surface-3` | `#252f46` | `#eceff5` | hover, selected |
| `--well` | `#131a29` | `#f4f6fa` | inset areas |
| `--border` | `#252e43` | `#e2e6ee` | quiet |
| `--border-strong` | `#354058` | `#cdd3df` | |
| `--text` | `#e4e9f2` | `#151a26` | 13.7:1 / 17.4:1 |
| `--text-2` (muted) | `#a3adc2` | `#4a5468` | 7.4:1 / 7.6:1 |
| `--text-3` (faint) | `#8e98ae` | `#626b80` | ≥4.5:1 on surfaces (was 3.1–4.1) |
| `--accent` | `#8b95ff` | `#4f55e0` | indigo; links, selection |
| `--accent-2` | `#5257e5` | `#4349d1` | primary button; white text 5.5:1 (was 3.6) |
| `--focus` | `#a5b0ff` | `#4f55e0` | focus ring, 8:1 |
| `--ok` (new) | `#34d399` | `#0b7a52` | success |
| `--sev-warn` | `#f5b84a` | `#9a6700` | warning |
| `--sev-error` | `#f87171` | `#c9302c` | danger |
| `--sev-info` | `#60a5fa` | `#2563c9` | |
| `--series-1…6` | `#7aa8ff #e69f00 #2fbf8f #d98cc0 #56b4e9 #ef8a5b` | (Phase 2: a darker set for light) | Okabe-Ito-based, colour-blind safe |
| `--radius` | `6px` (unchanged) | | literals become `--radius-sm 4px`, `--radius-lg 10px`, `--radius-pill 999px` in Phase 2 |
| `--shadow-1` / `--shadow-pop` | `0 1px 2px rgba(3,7,18,.35)` / `0 10px 30px rgba(3,7,18,.55)` | | replace the 4 literal shadows |
| `--font-sans` / `--font-mono` | unchanged stacks | | loading Inter/JetBrains Mono would be a network dependency: not proposed |

## 3. Prototype: the Logs page

The tokens above were injected into the running app (mock data). In the same page load, every element's position and size was measured before and after: **0 of 2,745 elements moved or resized** (Logs, dark); likewise 0 on Logs light, Home, Alerts and a dashboard. Screenshots: before/after, dark and light, shared in the session.

Known for Phase 2: in light mode the darker `--sev-info` makes the INFO bars of the severity chart heavy; charts will get their own fill tokens.
