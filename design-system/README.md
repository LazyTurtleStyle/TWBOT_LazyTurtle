# TWB design system: Claude Design library

A self-contained component library for the dashboard's **"war-room command console"**
look, packaged so it can sync to a [Claude Design](https://claude.ai/design) design-system
project via `/design-sync` in Claude Code.

## What's here

- **`styles.css`**: the shared design system: tokens (forge/iron/parchment palette),
  type (Barlow Condensed · IBM Plex Sans · IBM Plex Mono) and every component class.
- **`*.html`**, one self-contained preview per component group. The first line of each
  is a `<!-- @dsCard group="…" name="…" -->` marker, which is what the Claude Design
  "Design System" pane indexes into cards.

| File | Group | Component |
|------|-------|-----------|
| `foundations.html` | Foundations | palette swatches + type scale |
| `buttons.html` | Controls | buttons (ember / secondary / danger, sizes) |
| `badges.html` | Controls | status badges (ember / blood / moss / gold / dim) |
| `forms.html` | Controls | inputs, select, textarea |
| `surfaces.html` | Surfaces | panel (head + body) |
| `data-stats.html` | Data | stat tiles |
| `data-tables.html` | Data | data table with alert row |
| `signature-situation.html` | Signature | the situation bar (clear / alert / warn) |
| `navigation.html` | Navigation | sidebar nav + bot-status pulse |

These mirror the live classes in `webmanager/templates/shell.html`, so refinements made
in Claude Design map straight back to the dashboard.

## Sync to Claude Design

From this repo, in Claude Code:

```
/design-sync
```

It connects through your claude.ai login (you'll be prompted once to grant design-system
access, or run `/design-login`), then pushes these files into a Claude Design design-system
project, incrementally, one component at a time. Requires a Claude **Pro / Max / Team /
Enterprise** plan with Claude Design enabled.

## Preview locally

Just open any `*.html` in a browser (they each link `styles.css`).
