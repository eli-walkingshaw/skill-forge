> [!info] skill-forge proposal
> **cluster:** `cl_ceb2ab1126` (3 observations)
> **top terms:** rhino, svg, suitelet, uri, %23, suitescript, encode, white
>
> Review this draft below. If it looks good, **drag this file to `approved/`** — the watcher will commit and push it. If it doesn't, drag it to `archive/`.
>
> The SKILL.md content starts at the next `---` divider. Everything below that divider is what gets written to the skills repo.

---

---
name: rhino-svg-uri-encoding
description: Fixes Suitelet white-screen-on-save by percent-encoding special characters in inline SVG data URIs under Rhino/ES5. Use whenever the user mentions a NetSuite Suitelet rendering blank, a SVG inline background, or Rhino crashing on a # character. Catches the issue early when working on any SuiteScript file that includes inline SVG styling.
---

# Rhino SVG URI Encoding

NetSuite's Rhino/ES5 engine chokes on literal `#` in SVG data URIs, causing Suitelets to render as a white screen with no console error.

## When to use

- Suitelet renders a white/blank screen after deploy
- Inline SVG `background-image: url("data:image/svg+xml,...")` in a SuiteScript file
- Rhino throws a parse error near a `#` character
- Working on any SuiteScript Suitelet that includes inline SVG styling

## The pattern

Replace `#` with `%23` and `"` with `%22` inside the data URI portion of the CSS value.

```js
// Before — Rhino errors on the literal #
'background: url("data:image/svg+xml,<svg fill=\"#006BFF\"...")'

// After — fully percent-encoded
'background: url("data:image/svg+xml,<svg fill=%22%23006BFF%22...")'
```

## Steps

1. Locate any inline SVG data URI in the SuiteScript file.
2. Percent-encode `#` → `%23` and `"` → `%22` inside the URI.
3. Redeploy the Suitelet and reload the page.
4. If the screen still renders blank, check for other unencoded characters (`<`, `>`, `&`).

## Gotchas

- Rhino does not support template literals — keep using single quotes throughout the file.
- The encoding only applies to the URI string; standalone SVG markup elsewhere doesn't need it.
- This issue is silent: there's no server-side error, just a blank page. Always test the page render after deploy when SVGs are involved.
