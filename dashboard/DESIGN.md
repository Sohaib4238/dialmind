# Testing Dashboard — Design Spec

Reference feel: Linear, Vercel, Raycast. A quiet, precise tool. The interface should
disappear; the call is the content.

## Principles
1. **Borders, not shadows.** Surfaces are separated by 1px low-contrast borders and a
   slight step in background. No drop shadows except a faint one on floating layers
   (toasts, menus).
2. **One accent, used rarely.** The accent means "live / primary action". If more than
   one or two things on screen are accent-colored, something is wrong.
3. **Text hierarchy does the work.** Size, weight and the three text tones (primary,
   muted, faint) — not color or boxes.
4. **Motion explains state changes.** Nothing animates just to decorate.
5. **No AI clichés.** No purple/blue gradients, no glassmorphism, no emoji, no big
   rounded shadow cards, no centered hero layouts, no rainbow status colors.

## Color (dark, default)
Defined as CSS variables in `app/globals.css` and mapped into Tailwind/shadcn tokens.
In code the accent is named **`signal`** (`bg-signal`, `text-signal`, `bg-signal-soft`),
because shadcn already uses `accent` for hover backgrounds. Text tones: `text-foreground`,
`text-muted-foreground`, `text-faint`. Borders: `border-border`, `border-line-strong`.

| Token            | Value      | Use                                                  |
|------------------|------------|------------------------------------------------------|
| `--bg`           | `#0B0B0C`  | App background (near-black, never `#000`)            |
| `--surface`      | `#111113`  | Sidebar, panels, inputs                              |
| `--surface-2`    | `#17171A`  | Hover rows, active nav item, code/mono blocks        |
| `--border`       | `#202024`  | Default 1px borders and dividers                     |
| `--border-strong`| `#2C2C32`  | Focused/hovered inputs, selected rows                |
| `--text`         | `#EDEDEF`  | Primary text                                         |
| `--text-muted`   | `#9A9AA3`  | Secondary text, labels                               |
| `--text-faint`   | `#62626B`  | Hints, timestamps, placeholders                      |
| `--accent`       | `#3DD68C`  | THE accent: live call state, primary button, focus ring |
| `--accent-fg`    | `#04130B`  | Text on accent                                       |
| `--accent-soft`  | `rgba(61,214,140,0.12)` | Accent backgrounds (live pill, waveform glow) |
| `--danger`       | `#E5484D`  | Errors and "failed" only (semantic, not decorative)  |
| `--danger-soft`  | `rgba(229,72,77,0.12)`  | Error backgrounds                        |
| `--warning`      | `#E2A336`  | "Ringing" dot only                                   |

Call status colors (the only place several colors meet):
- `dialing` — muted text, faint dot
- `ringing` — warning dot, pulsing
- `answered` / live — accent dot + accent-soft pill
- `ended` — muted text, no dot color
- `failed` — danger text + danger-soft pill

Transcript speakers: **agent** lines in primary text with a small accent marker;
**callee** lines in primary text aligned the same way with a neutral marker. Speakers
are told apart by label and marker, not by colored bubbles.

## Typography
- **Geist Sans** for UI, **Geist Mono** for numbers in tables, timings, IDs, phone numbers
  (loaded locally from the `geist` package — no runtime font fetch).
- Scale (px / line-height): 12/16 caption · 13/20 body-sm (default UI) · 14/22 body ·
  16/24 title · 20/28 page heading · 28/34 display (call timer only).
- Headings: weight 600, letter-spacing `-0.02em`. Body: weight 400. Labels: 500.
- All times, durations, phone numbers and counters use `tabular-nums`.
- Sentence case everywhere. No ALL CAPS except 11px section labels in the sidebar
  (letter-spacing `0.04em`).

## Layout & spacing
- 4px base unit. Common steps: 4, 8, 12, 16, 24, 32, 48.
- Sidebar: fixed 232px, `--surface`, right border. Logo/name, nav (icon 16px + label),
  health indicator at the bottom.
- Content: padding 32px (24px under 768px), max-width 1080px, left-aligned (not centered
  hero). Page header = title + one-line description + optional actions on the right.
- 12-column grid, 24px gutters. Call page: 5 cols controls / 7 cols live transcript.
- Radius: 6px controls, 8px panels, 999px pills. Nothing larger.
- Row height 40px in lists; inputs 36px; buttons 32px (sm) / 36px (default).

## Components
- Built on **shadcn/ui** primitives, restyled with the tokens above (no default shadcn
  look left over).
- Icons: **Lucide** only, 16px, stroke 1.5, `--text-muted` unless active.
- Buttons: primary = accent bg + accent-fg; secondary = surface + border; ghost = text
  only; destructive = danger text on danger-soft. Press state scales to 0.98.
- Inputs: surface bg, border, focus = border-strong + 1px accent ring.
- Toasts (sonner): bottom-right, surface, border, small. Success uses a check icon, not
  green fill.
- Skeletons: `--surface-2` blocks with a slow 1.6s shimmer (none with reduced motion).
- Empty states: one short helpful line in muted text + optional action. No illustrations.
- Keyboard: `Ctrl/⌘ + Enter` starts a call; `Esc` hangs up (with the call page focused);
  `Ctrl/⌘ + S` saves settings. Shortcuts shown as small `kbd` hints.

## Motion (Motion library, `motion/react`)
- Easing: `[0.22, 1, 0.36, 1]` (ease-out). Springs for layout: `stiffness 420, damping 34`.
- Durations: 120ms hover/press · 180ms element enter · 240ms page transition.
- **Page transitions:** content fades in and rises 6px (240ms). No slide-across.
- **Transcript lines:** each new line fades in and rises 8px (180ms); list reflows with
  layout animation. Auto-scroll to newest unless the user scrolled up.
- **Status pill:** one pill that morphs (layout animation) between states; label
  cross-fades. Ringing: dot pulses (scale 1 → 1.6, opacity → 0, 1.2s loop).
- **Live waveform:** 24 thin bars next to the timer, only while a call is live. Bars move
  when someone is speaking (driven by agent/callee speaking-state events) and settle to a
  flat line when silent. Accent color for agent speech, muted for callee speech.
- **Hover/press:** rows tint to `--surface-2` (120ms); buttons scale 0.98 on press.
- **Reduced motion:** `<MotionConfig reducedMotion="user">` everywhere, and CSS
  `prefers-reduced-motion` disables pulses, shimmer and waveform movement (state is still
  shown by color/text).
