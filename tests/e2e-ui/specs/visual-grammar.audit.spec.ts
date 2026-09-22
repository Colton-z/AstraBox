/**
 * E2E: the visual grammar, checked by a machine rather than by eye.
 *
 * `docs/frontend-design.md` §8 says these rules should be a failing build and
 * not a habit. Until this spec existed they were a habit, and the habit lost:
 * a font-size utility that tailwind-merge silently dropped was wrong on 56 of
 * its 99 call sites for as long as anyone can remember, and nobody saw it
 * because the page is wrong in PATCHES — which reads as sloppy craft rather
 * than as a bug, so it gets tolerated instead of filed.
 *
 * Every assertion here is decidable without first agreeing on what looks good:
 *
 *   fractional   a computed font-size or radius that is not a whole pixel was
 *                not chosen by anyone. It comes from multiplying a token
 *                (`calc(--radius * 0.8)` → 6.4px) or from a decimal rem
 *                (`0.8438rem` → 13.5008px), and it can never line up with the
 *                rule beside it because no other rule can land on it.
 *   chrome       the shell is the same shell on every page. If the wordmark is
 *                15px here and 14px there, one of them is wrong — and which
 *                one does not have to be settled to know that.
 *   case         uppercase chrome is the house style of every admin template
 *                since Bootstrap (§6, §0) — whether it shouts through
 *                `text-transform` or by being typed that way, which is how
 *                nearly every offender here was actually written.
 *   keys         a snake_case or dotted-key string on screen is an internal
 *                spelling that escaped (§6). A missing translation degrades to
 *                the key itself, so nothing fails and nobody is told.
 *   mono         both directions of §6. Mono means "you will type or paste
 *                this": a relative time, a date or a bare count in it is the
 *                terminal voice spent on something nobody types, and a
 *                SENTENCE in it is that voice spent on English.
 *   aria         an `aria-controls` / `-labelledby` / `-describedby` naming an
 *                id that is not on the page. Worse than absent: it tells
 *                assistive tech there is somewhere to go, and there is not.
 *   ragged       blocks stacked in one column reach one right edge. A
 *                conversation is the case with many, and the assistant's stack
 *                was capped where the user's bubble was not.
 *   overflow     a page must render at 1280 with no horizontal body scroll.
 *   create       the page that makes a record obeys the same rules as the page
 *                that reads one — the walk opens records, so this is the one
 *                console surface it would otherwise never see.
 *   missing      a record page asked for a record that does not exist still
 *                names its subject. It cannot echo the URL's key, and two
 *                record TYPES cannot land on the same heading — whichever
 *                placeholder produced that collision is naming neither of them.
 *
 * NOT covered here: what a page does when it is touched. Both defects this spec
 * was extended for after it existed — an underline that left its tab, two list
 * rows whose fills merged — were interaction states, and every check in this
 * file reads a page at rest. Driving a browser to catch them failed twice under
 * mutation: `:focus-visible` is deliberately not matched by programmatic focus,
 * and a hover has to be aimed at the right element out of hundreds.
 *
 * The size-and-shape half of that is covered by reading the built stylesheet
 * instead — `scripts/check_interaction_states.py`, run by `make test-web`. What
 * remains uncovered is what an interaction PAINTS: a shadow that merges with
 * its neighbour, a focus ring nobody can see against the ground.
 *
 * Scope is every route reachable by clicking, in both themes, at two widths.
 *
 * The console lives at /manage/* against a real deployment and against `npm run
 * dev` alike: the console is part of the main bundle's router, and Vite's dev
 * server answers an unknown path with index.html, which mounts it.
 * ASTRABOX_UI_AUDIT_CONSOLE_ENTRY stays as an override for a deployment that
 * mounts the console somewhere else; with the console in the main bundle there
 * is no separate entry point for the default setup to name.
 *
 * The app walk needs a signed-in context. AstraBox ships its own identity
 * service (containers/casdoor), so this is not an external dependency to work
 * around — sign in once and hand the result over with
 * ASTRABOX_E2E_STORAGE_STATE. Without a session the app renders its shell and
 * an error body, and the walk stops at the first page with no heading rather
 * than reporting on one (see assertRendered).
 */
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath, refuseIfNotTheDeployment } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const CONSOLE_ENTRY = process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents';

const WIDTHS = [1280, 1440];
const THEMES = ['light', 'dark'] as const;

const sessions = trackSessions();

/** A record id no deployment can hold, for asking a record page for nothing. */
const MISSING_KEY = '__no_such_record__';

/** Move the console router to a URL nothing on the page links to. */
async function routeTo(page: Page, to: string) {
  await page.evaluate((target) => {
    window.history.pushState({}, '', target);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, to);
}

type Finding = { where: string; what: string; detail: string };

/**
 * What the page is made of, measured rather than described.
 *
 * Runs in the page because every question here is about COMPUTED style: the
 * class list says `text-13` whether or not anything applied it.
 */
async function measure(page: Page) {
  return page.evaluate(() => {
    const KEYISH = /^[a-z][a-z0-9]*([._][a-z0-9]+)+$/;

    /**
     * Classes that are present and have no effect.
     *
     * This catches ONE of the two mechanisms — the one where the class survives
     * into the DOM and loses in the cascade: a variant-prefixed base utility
     * outranks a plain call-site one, tailwind-merge cannot see that as a
     * conflict, both survive, and the prefixed one wins (`h-11` on a list whose
     * component says `group-data-horizontal/tabs:h-8` — 32px list, 44px
     * children hanging out of both ends).
     *
     * It cannot catch the other: when tailwind-merge drops a mis-grouped
     * utility, the class never reaches the DOM, so reading the DOM finds
     * nothing to compare. `frontend/src/lib/utils.test.ts` covers that merge
     * behavior directly.
     *
     * Only plain utilities are checked:
     * a prefixed or arbitrary one may legitimately not apply at this viewport
     * or state, and a check with false positives is a check nobody runs.
     */
    const FS: Record<string, number> = { 'text-xs': 12, 'text-sm': 14, 'text-base': 16, 'text-lg': 18, 'text-xl': 20, 'text-2xl': 24, 'text-10': 10, 'text-11': 11, 'text-13': 13, 'text-15': 15 };
    const FW: Record<string, number> = { 'font-normal': 400, 'font-medium': 500, 'font-semibold': 600, 'font-bold': 700 };
    // Tailwind's own steps, not the project's token scale: bare `rounded` is 4px.
    const RAD: Record<string, number> = { 'rounded-none': 0, rounded: 4, 'rounded-sm': 4, 'rounded-md': 6, 'rounded-lg': 8, 'rounded-xl': 12 };
    const PAD: Record<string, string[]> = { p: ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'], px: ['paddingLeft', 'paddingRight'], py: ['paddingTop', 'paddingBottom'], pt: ['paddingTop'], pr: ['paddingRight'], pb: ['paddingBottom'], pl: ['paddingLeft'] };
    const asNum = (v: string) => { const x = Number.parseFloat(v); return Number.isFinite(x) ? x : null; };
    const inert: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>('*')) {
      const rect = el.getBoundingClientRect();
      if (!rect.width && !rect.height) continue;
      const raw = el.className;
      const cls = typeof raw === 'string' ? raw : (raw as unknown as SVGAnimatedString)?.baseVal;
      if (typeof cls !== 'string' || !cls) continue;
      const cs = getComputedStyle(el);
      const file = (c: string, want: number, got: number) =>
        inert.push(`${c} asks ${want}, renders ${Math.round(got * 100) / 100} ("${(el.textContent || '').trim().slice(0, 22)}")`);
      const tokens = cls.split(/\s+/);
      // A plain utility may be overridden on purpose, and those overrides are
      // not defects — a check that reports them is a check nobody reads.
      // Measured: two of the first three findings were an input's `px-2.5`
      // beside a call site's `pl-8` (a narrower utility on the same axis, which
      // is how you make room for an icon) and shadcn's own `text-base md:text-sm`
      // (16px on a phone so iOS does not zoom the page, 14 from md up).
      // Same FAMILY, not same prefix: `hover:text-foreground` is a colour and
      // cannot override a font size, so matching on `^text-` here suppressed
      // every real font-size finding on any element with a hover colour — which
      // is most of them. Measured by mutation: with this guard too wide, the
      // detector went green against a deliberately reintroduced bug.
      const overriddenBy = (sameFamily: (u: string) => boolean) =>
        tokens.some((t) => t.includes(':') && sameFamily(t.split(':').pop() || ''));
      // The same tolerance, one element up. A registry component ships the pair:
      // `input-group` writes `has-[>[data-align=inline-start]]:[&>input]:pl-1.5`
      // and `input` writes `px-2.5`, so the child's plain utility renders
      // something else BY CONTRACT rather than by mistake. `[&>` is a
      // direct-child selector, so only the immediate parent can reach in — the
      // walk stops there rather than tolerating anything an ancestor declares.
      // Without this the only way to keep the gate green is to edit a vendored
      // file, which is the thing the drift ledger forbids.
      const parentTokens = (() => {
        const raw = el.parentElement?.className;
        const s = typeof raw === 'string' ? raw : (raw as unknown as SVGAnimatedString)?.baseVal;
        return typeof s === 'string' && s ? s.split(/\s+/) : [];
      })();
      const declaredByParent = (sameFamily: (u: string) => boolean) =>
        parentTokens.some((t) => t.includes('[&>') && sameFamily(t.split(':').pop() || ''));
      const overruled = (sameFamily: (u: string) => boolean) =>
        overriddenBy(sameFamily) || declaredByParent(sameFamily);
      // Excluding the token itself: `gap-0.5`.split('-')[0] is `gap`, so a
      // self-matching check silently switches the whole family off.
      const narrower = (self: string, names: string[]) =>
        tokens.some((t) => t !== self && names.includes(t.split('-')[0]));
      for (const c of tokens) {
        if (!c || c.includes(':') || c.includes('[')) continue;
        let m: RegExpExecArray | null;
        if (c in FS) { if (overruled((u) => u in FS)) continue; const g = asNum(cs.fontSize); if (g != null && Math.abs(g - FS[c]) > 0.5) file(c, FS[c], g); }
        else if (c in FW) { if (overruled((u) => u in FW)) continue; const g = asNum(cs.fontWeight); if (g != null && g !== FW[c]) file(c, FW[c], g); }
        else if (c in RAD) { if (overruled((u) => u in RAD)) continue; const g = asNum(cs.borderTopLeftRadius); if (g != null && g < 100 && Math.abs(g - RAD[c]) > 0.5) file(c, RAD[c], g); }
        else if ((m = /^(h|w)-(\d+(?:\.5)?)$/.exec(c))) { if (overruled((u) => new RegExp(`^${m![1]}-\\d`).test(u)) || narrower(c, [`min-${m[1]}`, `max-${m[1]}`])) continue; const want = Number.parseFloat(m[2]) * 4; const g = m[1] === 'w' ? rect.width : rect.height; if (Math.abs(g - want) > 1) file(c, want, g); }
        else if ((m = /^(p|px|py|pt|pr|pb|pl)-(\d+(?:\.5)?)$/.exec(c))) {
          const NARROWER: Record<string, string[]> = { p: ['px', 'py', 'pt', 'pr', 'pb', 'pl'], px: ['pl', 'pr'], py: ['pt', 'pb'], pt: [], pr: [], pb: [], pl: [] };
          if (overruled((u) => /^p[xytrbl]?-\d/.test(u)) || narrower(c, NARROWER[m[1]])) continue;
          const want = Number.parseFloat(m[2]) * 4;
          for (const prop of PAD[m[1]]) { const g = asNum(cs[prop as keyof CSSStyleDeclaration] as string); if (g != null && Math.abs(g - want) > 0.5) { file(`${c}(${prop})`, want, g); break; } }
        }
        else if ((m = /^gap-(\d+(?:\.5)?)$/.exec(c))) { if (overruled((u) => /^gap(-[xy])?-\d/.test(u)) || narrower(c, ['gap', 'gap-x'])) continue; const want = Number.parseFloat(m[1]) * 4; // Row gap, not column: `gap-x-*` overriding the column axis is ordinary,
          // and reading only that axis reported a working `gap-0.5` (rowGap 2px)
          // as inert because a sibling rule had widened the columns.
          const g = asNum(cs.rowGap); if (g != null && Math.abs(g - want) > 0.5) file(c, want, g); }
      }
    }
    /**
     * An indicator shares an edge with the thing it indicates.
     *
     * The active-tab underline is drawn on the trigger as a `::after` pinned to
     * one of its edges. Pin it with a number tuned for one geometry and it
     * survives exactly until either box changes height — the session panel's
     * hung 11px clear of both the tab above it and the strip's hairline,
     * reading as a bar that belonged to nothing.
     *
     * `data-active`, not `data-state="active"`: the tab kit is Base UI now and
     * it marks the selected tab with a valueless `data-active`. The Radix
     * spelling matched nothing, so `active` was null on every strip and this
     * whole check skipped — a check that reads clean because it found nothing
     * to read is the failure this file exists to prevent.
     */
    const detached: string[] = [];
    for (const strip of document.querySelectorAll('[role="tablist"]')) {
      const active = strip.querySelector<HTMLElement>('[role="tab"][data-active]');
      if (!active) continue;
      const after = getComputedStyle(active, '::after');
      const h = Number.parseFloat(after.height);
      if (!Number.isFinite(h) || h <= 0 || after.opacity === '0') continue;
      const offset = Number.parseFloat(after.bottom);
      if (!Number.isFinite(offset)) continue;
      if (Math.abs(offset) > 1) {
        detached.push(
          `active tab "${(active.textContent || '').trim().slice(0, 16)}" indicator sits ` +
            `${-offset}px off its own bottom edge`,
        );
      }
    }

    /**
     * A filled row has room to be its own object, and two states do not share
     * one fill.
     *
     * Measured on the session rail: rows 56px tall at `gap: 0`, each with a 6px
     * radius and a fill. Every row's bottom edge WAS the next row's top edge,
     * so the selected row and the row under the pointer merged into a single
     * shape with pinched corners — and both used the same `--accent`, leaving a
     * 2px bar as the only thing saying which one you had chosen.
     *
     * Both halves are decidable: a radius needs a gap to show, and "chosen" and
     * "pointed at" are different facts that cannot look identical.
     */
    const collisions: string[] = [];
    const opaque = (c: string) => !/^(transparent$|rgba?\(.*,\s*0\)$)/.test(c);
    for (const list of document.querySelectorAll('ul, [role="listbox"], [role="menu"]')) {
      const kids = [...list.children]
        .map((li) => (li.querySelector('a,button') ?? li) as HTMLElement)
        .filter((el) => el.getBoundingClientRect().height > 0);
      for (let i = 1; i < kids.length; i++) {
        const a = kids[i - 1].getBoundingClientRect();
        const b = kids[i].getBoundingClientRect();
        const sa = getComputedStyle(kids[i - 1]);
        const sb = getComputedStyle(kids[i]);
        if (!opaque(sa.backgroundColor) || !opaque(sb.backgroundColor)) continue;
        const gap = b.top - a.bottom;
        const radius = Math.max(Number.parseFloat(sa.borderBottomLeftRadius) || 0, Number.parseFloat(sb.borderTopLeftRadius) || 0);
        // Touching, not merely close. A 2px gap already separates two 6px
        // corners — the console rail has used that for as long as it has looked
        // right, and a check that calls it a defect is a check that gets
        // switched off. What cannot work is gap 0: one row's bottom edge IS the
        // next row's top edge, the two corner arcs meet, and the pair reads as
        // a single pinched shape.
        if (radius > 0 && gap <= 0) {
          collisions.push(`two filled ${radius}px-rounded rows share an edge (gap ${gap}px) — their corners cancel`);
        }
      }
    }

    /**
     * Text that renders past the window's right edge without a scrollbar.
     *
     * Horizontal body scroll is the visible version of this and is checked
     * separately. The quiet version is worse: a clipping ancestor swallows the
     * overflow, so nothing scrolls, nothing is elided, and the reader has no
     * signal that what they are looking at is incomplete. Measured: a
     * 32-character trigger id in a fact rail ended 81px past the viewport, cut
     * mid-value, on a page whose whole purpose is handing that id to someone.
     */
    const clipped: string[] = [];
    const viewport = document.documentElement.clientWidth;
    for (const el of document.querySelectorAll<HTMLElement>('*')) {
      if ([...el.children].some((c) => (c.textContent || '').trim())) continue;
      const text = (el.textContent || '').trim();
      if (!text) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.right <= viewport + 1) continue;
      // A pane the reader can scroll is not clipping anything.
      let scrollable = false;
      for (let p = el.parentElement; p; p = p.parentElement) {
        const ov = getComputedStyle(p).overflowX;
        if ((ov === 'auto' || ov === 'scroll') && p.scrollWidth > p.clientWidth) { scrollable = true; break; }
      }
      if (!scrollable) clipped.push(`"${text.slice(0, 24)}" ends ${Math.round(r.right - viewport)}px past the window`);
    }

    /**
     * A control whose label runs out of its own box.
     *
     * Equal-split rows (`flex-1`) hand every segment the same width whatever
     * its label measures, and a flex item shrinks below its content without
     * complaint: the label then paints past the padding, against the
     * neighbour, and — on the last segment — over the control's border.
     * Measured on the user menu's theme control: three 68px segments for a
     * zh "跟随系统" label that needs 84px with its icon. The page-level
     * checks above cannot see it, because nothing leaves the window and the
     * control itself is not a scroll container.
     */
    const overflowingLabels: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>('button, [role="tab"], [role="radio"], [role="menuitem"], [role="option"]')) {
      const box = el.getBoundingClientRect();
      if (!box.width || el.offsetParent === null) continue;
      const cs = getComputedStyle(el);
      const innerLeft = box.left + (Number.parseFloat(cs.paddingLeft) || 0) + (Number.parseFloat(cs.borderLeftWidth) || 0);
      const innerRight = box.right - (Number.parseFloat(cs.paddingRight) || 0) - (Number.parseFloat(cs.borderRightWidth) || 0);
      let contentLeft = Number.POSITIVE_INFINITY;
      let contentRight = Number.NEGATIVE_INFINITY;
      for (const node of el.childNodes) {
        let r: DOMRect | null = null;
        if (node.nodeType === Node.TEXT_NODE) {
          if (!(node.textContent || '').trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          r = range.getBoundingClientRect();
        } else if (node instanceof HTMLElement || node instanceof SVGElement) {
          r = node.getBoundingClientRect();
        }
        if (!r || !r.width) continue;
        contentLeft = Math.min(contentLeft, r.left);
        contentRight = Math.max(contentRight, r.right);
      }
      if (!Number.isFinite(contentLeft)) continue;
      const spill = Math.max(innerLeft - contentLeft, contentRight - innerRight);
      if (spill > 1) {
        overflowingLabels.push(`"${(el.textContent || '').trim().slice(0, 24)}" spills ${Math.round(spill)}px past its padding`);
      }
    }

    const visible = [...document.querySelectorAll<HTMLElement>('*')].filter(
      (el) => el.offsetParent !== null && (el.textContent || '').trim(),
    );
    // Only leaves carry text of their own; an ancestor "contains" its
    // children's text and would report their case and their spelling as well
    // as its own.
    const leaves = visible.filter(
      (el) => ![...el.children].some((child) => (child.textContent || '').trim()),
    );
    const isMono = (el: Element) => /mono/i.test(getComputedStyle(el).fontFamily);
    const px = (v: string) => Number.parseFloat(v);
    const fractional = (v: string) => Number.isFinite(px(v)) && Math.abs(px(v) - Math.round(px(v))) > 0.01;

    const fractionalSizes = new Map<string, string>();
    const fractionalRadii = new Map<string, string>();
    for (const el of visible) {
      const s = getComputedStyle(el);
      if (fractional(s.fontSize)) fractionalSizes.set(s.fontSize, (el.textContent || '').trim().slice(0, 24));
      for (const corner of [s.borderTopLeftRadius, s.borderBottomRightRadius]) {
        // `rounded-full` resolves to an enormous px value on purpose; it is a
        // shape, not a step on the scale.
        if (fractional(corner) && px(corner) < 100) {
          fractionalRadii.set(corner, el.tagName.toLowerCase());
        }
      }
    }

    // The shell, so two pages can be compared element for element. Found by
    // what it says rather than by a class, because a class is the thing under
    // test — but scoped to the rail, because the product name appears twice on
    // every page: once as the wordmark and once as the first breadcrumb. An
    // unscoped search takes whichever comes first in the DOM, which is a
    // different element in the two shells, so the fingerprint would have been
    // comparing a logo against a breadcrumb and calling them equal.
    const rail = document.querySelector('[data-slot="sidebar-header"]');
    const wordmark = rail
      ? [...rail.querySelectorAll<HTMLElement>('*')].find(
          (el) => !el.children.length && (el.textContent || '').trim() === 'AstraBox',
        )
      : undefined;
    const mark = rail?.querySelector('svg') ?? null;
    const h1 = document.querySelector('h1');
    // Type only, never measured height: a heading that wraps at one width is
    // taller without being styled differently, and a check that called that a
    // defect would be answered by ignoring the check.
    const shape = (el: Element | null) => {
      if (!el) return null;
      const s = getComputedStyle(el);
      return `${s.fontSize}/${s.fontWeight}/${s.letterSpacing}`;
    };

    return {
      fractionalSizes: [...fractionalSizes.entries()].map(([v, sample]) => `${v} (${sample})`),
      fractionalRadii: [...fractionalRadii.keys()],
      // Both ways a screen can shout, because the CSS one was the only one
      // checked and almost every offender was written the other way: a literal
      // `eyebrow="AGENT SESSIONS"` has no text-transform to find.
      //
      // Two exclusions, and they are not conveniences. A mono run is machine
      // identity, where ANTHROPIC_API_KEY is the string the reader will paste.
      // A status pill carries the backend's own state word verbatim — READY is
      // the deployment's spelling, and re-casing it would put the console's
      // vocabulary at odds with the log line beside it (CLAUDE.md: engine
      // semantics belong to the engine).
      uppercase: leaves
        .filter((el) => {
          const text = (el.textContent || '').trim();
          if (getComputedStyle(el).textTransform === 'uppercase') return true;
          if (isMono(el)) return false;
          if (el.closest('[data-testid="status-pill"]')) return false;
          // And anything the console QUOTES rather than writes: a server's
          // error message, a backend's refusal reason. Same ground as the pill
          // — the vocabulary is the deployment's — and marked rather than
          // guessed at, because one of those messages is the single word
          // READY and no pattern tells it from a shouting label.
          if (el.closest('[data-slot="verbatim"], [data-slot="error-state"]')) return false;
          // Transcript prose is the model's, not the console's: a turn that
          // answers READY, or names S&P 500 tickers, is quoted content on the
          // same ground as the verbatim slot. Chrome never renders inside
          // the markdown container.
          if (el.closest('.markdown')) return false;
          // A questionnaire's title, prompt, and choice texts are the
          // engine's AskUserQuestion strings rendered verbatim — a model
          // that offers options named DIRECT and AUTO chose that casing,
          // and these four slots carry engine-authored text by definition
          // (ui/questionnaire's only consumer is the interaction card). The
          // card's own chrome — step chips, question counters, actions —
          // lives outside these slots and stays checked.
          if (el.closest(
            '[data-slot="questionnaire-title"], [data-slot="questionnaire-description"], '
            + '[data-slot="questionnaire-choice-label"], [data-slot="questionnaire-choice-description"]',
          )) return false;
          // CJK has no case, so a Chinese sentence quoting one Latin acronym
          // (MCP 数据源) equals its own toUpperCase and reads to this check as
          // shouting. The rule is about Latin admin-template chrome; text
          // that is substantially CJK cannot be that.
          if (/[\u3040-\u30ff\u4e00-\u9fff]/.test(text)) return false;
          return /[A-Z]{3}/.test(text) && text === text.toUpperCase() && /[A-Z]/.test(text);
        })
        .map((el) => (el.textContent || '').trim().slice(0, 24)),
      keyLeaks: leaves
        .filter((el) => !isMono(el) && KEYISH.test((el.textContent || '').trim()))
        .map((el) => (el.textContent || '').trim().slice(0, 32)),
      // §6 in the direction it was written: mono is for what a reader will type
      // or paste, so a SENTENCE in it is the terminal voice spent on English.
      // Three or more purely alphabetic words is what tells prose from an
      // identity — `assistant · claude-code` has one, because a hyphen makes a
      // token a name rather than a word, and `Public — visible to and usable
      // by all signed-in users` has seven.
      // `option` explicitly: a native select renders its list outside the page,
      // so its options have no box and never reach `leaves` — which is how a
      // mono select hid six sentences from a check written to find exactly
      // that. They inherit the select's face, so the question is the same one.
      proseInMono: [...leaves, ...document.querySelectorAll('option')]
        .filter((el) => {
          if (!isMono(el)) return false;
          if (el.closest('pre, code, [data-slot="verbatim"], [data-slot="error-state"]')) return false;
          const text = (el.textContent || '').trim();
          if (!text || text.length > 80) return false;
          return text.split(/\s+/).filter((w) => /^[A-Za-z]{2,}$/.test(w)).length >= 3;
        })
        .map((el) => (el.textContent || '').trim().slice(0, 34)),
      // Mono the other way round: it means "you will type or paste this", not
      // "this is data" and not "these digits align" (§6, settled). A relative
      // time, a date and a bare count are none of those, and `tabular-nums` is
      // what lines digits up — the session rail set eleven `1d ago`s and its
      // own count in mono, which is the terminal voice spent on nothing.
      monoNonIdentity: leaves
        .filter((el) => {
          if (!isMono(el)) return false;
          const text = (el.textContent || '').trim();
          return (
            text.length < 30 &&
            /^(\d+[smhdw] ago|just now|\d+ (second|minute|hour|day|week|month|year)s? ago|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?|\d{1,3}(,\d{3})*)$/.test(
              text,
            )
          );
        })
        .map((el) => (el.textContent || '').trim().slice(0, 24)),
      // An ARIA reference that resolves to nothing is worse than an absent one:
      // it tells assistive tech there is a thing to go to, and there is not.
      // The session's workspace tabs pointed all four `aria-controls` at panel
      // ids Radix had minted for content rendered outside its context, so the
      // tablist had no `role="tabpanel"` behind it at all.
      danglingAria: (() => {
        const dangling: string[] = [];
        for (const attr of ['aria-controls', 'aria-labelledby', 'aria-describedby']) {
          for (const el of document.querySelectorAll(`[${attr}]`)) {
            for (const id of (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean)) {
              if (!document.getElementById(id)) {
                dangling.push(`${el.tagName.toLowerCase()} ${attr}="${id}"`);
              }
            }
          }
        }
        return [...new Set(dangling)];
      })(),
      // Blocks stacked in one column reach one right edge. A conversation is
      // the case that has many: the assistant's stack was capped at
      // `max-w-[95%]` while the user's bubble was not, so a tool row stopped
      // 37px short of the message above it — two columns a reader takes as one.
      raggedColumn: (() => {
        const groups = new Map<Element, number[]>();
        for (const el of document.querySelectorAll<HTMLElement>('[data-testid$="-message"]')) {
          const parent = el.parentElement;
          if (!parent) continue;
          groups.set(parent, [
            ...(groups.get(parent) || []),
            Math.round(el.getBoundingClientRect().width),
          ]);
        }
        const ragged: string[] = [];
        for (const widths of groups.values()) {
          const distinct = [...new Set(widths)];
          if (widths.length > 1 && distinct.length > 1) {
            ragged.push(distinct.sort((a, b) => a - b).join(' vs ') + 'px');
          }
        }
        return [...new Set(ragged)];
      })(),
      hScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      overflowingLabels,
      // The SHELL, and only the shell: what every page wears regardless of what
      // it holds. The heading belongs to the page, so it is reported on its own
      // below — folding it in here made a page that simply has no heading read
      // as "the shell renders differently", which is a different defect with a
      // different fix.
      /**
       * The type a record's cards are set in, so two record pages can be
       * compared the way two shells already are.
       *
       * The shell check has caught a wordmark that was 15px on one page and
       * 14px on another since it was written; the same drift inside the cards
       * went unnoticed three times in a row, because it needs someone to look
       * at a page as a design rather than as a feature — and the page had just
       * been looked at as a feature. One role, one size, on every record.
       */
      cardType: (() => {
        // One slot, measured: the column a field's value renders in. Comparing
        // the SET of sizes a card uses instead reported "this page has no help
        // text" as drift — absence is not disagreement, and a check that cries
        // about it gets switched off.
        const slot = document.querySelector('[data-slot="field-value"]');
        return slot ? getComputedStyle(slot).fontSize : '';
      })(),
      chrome: {
        // A shell that lost its rail is not "consistent with itself"; it is
        // unmeasured, and null compared against null would pass.
        railFound: Boolean(rail),
        wordmark: shape(wordmark ?? null),
        mark: mark ? `${Math.round(mark.getBoundingClientRect().width)}x${Math.round(mark.getBoundingClientRect().height)}` : null,
        // Where the mark SITS, not only how big it is. The two shells
        // share one BrandBlock and still differed: it was centred against
        // a column whose height depended on the subtitle, and only the app
        // passes one, so crossing between them moved the corner six pixels.
        markAt: mark
          ? `${Math.round(mark.getBoundingClientRect().x)},${Math.round(mark.getBoundingClientRect().y)}`
          : null,
        // A rail item, as the shell builds it. The console's carried
        // `py-[7px]` where the app's carries `py-1.5`: one pixel off a rung,
        // so the console's items stood 34px against the app's 32 and the whole
        // list shifted on every crossing between the two.
        railItem: (() => {
          const item = [...document.querySelectorAll('nav a')].find(
            (a) => a.getBoundingClientRect().height > 8,
          );
          if (!item) return null;
          const cs = getComputedStyle(item);
          return `${Math.round(item.getBoundingClientRect().height)}/${cs.fontSize}/${cs.paddingTop}/${cs.borderRadius}`;
        })(),
      },
      inert: [...new Set(inert)],
      detached,
      collisions: [...new Set(collisions)],
      clipped: [...new Set(clipped)],
      headingCount: document.querySelectorAll('h1').length,
      headingType: shape(h1),
    };
  });
}

/**
 * Every console section, taken from the rail rather than from a list kept here.
 *
 * Deduplicated because the trail is a `nav` too: standing on a record, the
 * trail links back to the section the record belongs to, and that section then
 * appears twice.
 */
/**
 * Sections, plus the record and the create page behind each.
 *
 * The heading band is the same band on all three, and a reader crosses between
 * them in one flow — which is why a size that changes there is the loudest
 * inconsistency the console can have, and why this walks them together.
 */
async function routesFromRail(page: Page): Promise<string[]> {
  const sections = await consoleRoutes(page);
  const out: string[] = [];
  for (const section of sections) {
    out.push(section);
    await page.goto(appPath(section));
    await page.waitForTimeout(2400);
    const row = page.locator('tr[tabindex], [role="row"][tabindex]').first();
    await row.waitFor({ state: 'visible', timeout: 3_000 }).catch(() => undefined);
    if (await row.count()) {
      await row.click();
      await page.waitForTimeout(1200);
      const path = new URL(page.url()).pathname;
      if (path.split('/').filter(Boolean).length === 3) out.push(path);
    }
    out.push(`${section}/new`);
  }
  return [...new Set(out)];
}

async function consoleRoutes(page: Page): Promise<string[]> {
  return page.evaluate(() => [
    ...new Set(
      [...document.querySelectorAll<HTMLAnchorElement>(
        '[data-slot="sidebar-content"] a[href^="/manage/"]',
      )].map(
        (a) => a.getAttribute('href') || '',
      ),
    ),
  ]);
}

/**
 * The create surface behind a section, if it has one.
 *
 * A create page is the one console surface the row-walk cannot reach — it opens
 * records, and a record that does not exist yet has no row. Both defects found
 * when these stopped being drawers had been sitting in front of an audit that
 * ran clean: an eyebrow repeating its own title word for word, and a title
 * carrying a word its section heading already said.
 *
 * `/manage/<section>/new` is a record page asked for a record called "new"
 * wherever a section has no create page, so the page says which it is rather
 * than the walk guessing from what rendered.
 */
async function createSurface(page: Page, route: string): Promise<boolean> {
  await routeTo(page, `${route}/new`);
  // Let the page finish fetching before asking what it is. A create page that
  // loads something first — the assistants one asks which environments its
  // engine can run in — is its own loading state for a moment, and reading the
  // marker there reported the page as absent. Measured: one run in four
  // covered three of the four create pages and said so.
  await page
    .locator('[data-slot="loading-state"]')
    .waitFor({ state: 'detached', timeout: 15_000 })
    .catch(() => undefined);
  const marker = page.locator('[data-slot="create-page"]');
  await marker.waitFor({ state: 'attached', timeout: 8_000 }).catch(() => undefined);
  return (await marker.count()) > 0;
}

/**
 * What a record page calls itself when it is asked for a record that does not
 * exist.
 *
 * A heading names the page's subject (§1). When no record can name it — the
 * fetch is in flight, or it is over and failed — the page names its own TYPE.
 * That makes two things decidable without agreeing on wording: no record page
 * can arrive at the key from the URL, which the trail's last segment is
 * already showing, and no two record TYPES can arrive at the same heading.
 *
 * There is no link to a record that does not exist, and the dev server does
 * not route /manage/* to the console bundle (see the note above), so the URL
 * moves the way the router itself moves it.
 *
 * The wait is for the failure to be ON SCREEN, not for the heading to change.
 * Measured: waiting for a change returned the moment the router committed,
 * which is the LOADING state — so a run with the defect deliberately put back
 * read the loading heading, never reached the failed one, and passed. The
 * error card is the only signal that says the fetch is over and it lost.
 */
async function missingRecordHeading(page: Page, route: string, key: string): Promise<string> {
  const failure = page.locator('[data-slot="error-state"]');
  // Off this record and back to its section first. "A failure card is visible"
  // cannot tell a fresh one from the one still standing from the PREVIOUS
  // record, so without this the probe reads the previous page's heading as
  // this page's — measured, as two record types reported as sharing a name.
  await routeTo(page, route);
  await failure.waitFor({ state: 'detached', timeout: 15_000 });
  await routeTo(page, `${route}/${key}`);
  try {
    await failure.waitFor({ state: 'visible', timeout: 20_000 });
  } catch {
    const heads = await page.locator('h1').allTextContents();
    throw new Error(
      `${route}/${key}: asked for a record that cannot exist and no failure was ` +
        `reported within 20s. The page is at ${new URL(page.url()).pathname} under the ` +
        `heading “${(heads[0] || '').trim()}”. Either the record page never took the ` +
        `route, or it answers a missing record with something other than an error.`,
    );
  }
  const heads = await page.locator('h1').allTextContents();
  await assertRendered(page, `${route}/${key}`, heads.length);
  return (heads[0] || '').trim();
}

/**
 * A walk that never arrived reports a clean page.
 *
 * Measured: against a deployment with OIDC, a fresh Playwright context has no
 * session, every navigation redirects to /login, and the audit passes four
 * ways — having measured the login form and nothing else. The rest of this
 * suite targets ASTRABOX_LOCAL_MODE, which has no login; anywhere else this
 * needs a storageState, and it must say so instead of going green.
 */
async function assertArrived(page: Page, where: string) {
  const path = new URL(page.url()).pathname;
  expect(
    path.includes('/login'),
    `${where}: redirected to the sign-in page — this audit needs an authenticated ` +
      `context (the suite's other specs assume ASTRABOX_LOCAL_MODE). Nothing was measured.`,
  ).toBe(false);
}

/**
 * The page rendered its own subject, so what follows is a measurement of the
 * page and not of whatever stood in for it.
 *
 * §1 gives every screen exactly one heading, which makes "no heading" a usable
 * arrival signal — and it has to be an arrival check rather than a finding.
 * Measured: a context whose data calls fail renders the shell and an error
 * body, has no heading, and produced "app /assistants: 0 <h1>" for a page that
 * has one and always had. A finding that is really a non-arrival sends someone
 * to fix code that is not broken.
 */
async function assertRendered(page: Page, where: string, headings: number) {
  expect(
    headings,
    `${where}: expected one page heading, saw ${headings}. Either the page never ` +
      `rendered (a failed fetch, a route that did not change) — in which case ` +
      `nothing here was measured — or the page really states its subject zero ` +
      `times or twice (§1). Open it before believing either.`,
  ).toBe(1);
}


/** Measure wherever the walk has landed, and file whatever is off. */
async function auditHere(
  page: Page,
  where: string,
  width: number,
  findings: Finding[],
  options: { requestsMayRemainOpen?: boolean } = {},
) {
  await assertArrived(page, where);
  // Counts and lists land after their fetch; a measurement taken mid-flight
  // reports the skeleton's type, not the page's.
  // A rendered conversation can still own the GET ai-stream that resumes its
  // active turn. That request is meant to remain open, so its caller waits for
  // `run-view` instead and says here that network silence is not its contract.
  if (!options.requestsMayRemainOpen) {
    await page.waitForLoadState('networkidle');
  }
  // Wait for the subject to appear rather than sampling once and calling its
  // absence a defect: request readiness does not itself say that React has
  // committed the render it was waiting on. Sampling immediately reported no
  // <h1> for a page that has one, on two separate runs — the second after the
  // first had already been shown to be a false alarm.
  await page
    .waitForFunction(() => document.querySelectorAll('h1').length === 1, undefined, { timeout: 5_000 })
    .catch(() => undefined);
  const m = await measure(page);
  await assertRendered(page, where, m.headingCount);
  const add = (what: string, items: string[] | boolean, detail = '') => {
    if (Array.isArray(items) ? items.length : items) {
      findings.push({ where, what, detail: detail || (items as string[]).join(', ') });
    }
  };
  add('fractional font-size', m.fractionalSizes);
  add('fractional radius', m.fractionalRadii);
  add('uppercase chrome', m.uppercase);
  add('mono on something nobody types', m.monoNonIdentity);
  add('a sentence in the terminal voice', m.proseInMono);
  add('an ARIA reference pointing at nothing', m.danglingAria);
  add('message blocks that do not share a right edge', m.raggedColumn);
  add('internal spelling on screen', m.keyLeaks);
  add('horizontal body scroll', m.hScroll, `${width}px`);
  add('class present but inert', m.inert);
  add('indicator detached from what it marks', m.detached);
  add('adjacent rows collide', m.collisions);
  add('text clipped past the window', m.clipped);
  add('a label that runs out of its control', m.overflowingLabels);
  return JSON.stringify(m.chrome);
}

/**
 * The shell does not change because the page under it changed.
 *
 * One distinct fingerprint means every page agreed; more means at least one of
 * them dressed the shell differently, and the diff names which.
 */
function shellDrift(chromeByRoute: Map<string, string>, label: string): Finding | null {
  const distinct = new Map<string, string[]>();
  for (const [route, fp] of chromeByRoute) {
    distinct.set(fp, [...(distinct.get(fp) ?? []), route]);
  }
  if (distinct.size <= 1) return null;
  return {
    where: label,
    what: 'the shell renders differently between pages',
    detail: [...distinct.entries()].map(([fp, rs]) => `${rs.join(' ')} → ${fp}`).join('  |  '),
  };
}

async function useTheme(page: Page, theme: string, width: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.addInitScript((t) => {
    try {
      window.localStorage.setItem('astrabox-theme', t as string);
    } catch {
      /* a context without storage still renders the default theme */
    }
  }, theme);
}

/** Give one app audit a conversation with both sides of a real turn to measure. */
async function visualConversation(request: APIRequestContext): Promise<string> {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessions.push(created.session_id);
  await api.waitForSessionReady(created.session_id);

  const turn = await api.sendTurn(
    created.session_id,
    'Reply with one short sentence and do not use tools.',
  );
  expect(turn.errorText, 'the visual-audit fixture turn must not fail').toBeNull();
  expect(turn.text.trim(), 'the visual-audit fixture must render an assistant message').not.toBe('');
  await api.waitForSessionReady(created.session_id);
  return created.session_id;
}

const report = (findings: Finding[]) =>
  expect(
    findings.map((f) => `${f.where}: ${f.what} — ${f.detail}`),
    'every finding is a value nobody chose, or the same element rendered two ways',
  ).toEqual([]);

for (const width of WIDTHS) {
  for (const theme of THEMES) {
    /**
     * The product surface: the agent picker, the assistant picker, and a real
     * conversation. It is where a user spends their time and it was outside
     * this walk until it wasn't — the console was audited first only because
     * it was the surface being rebuilt.
     */
    test(`the app obeys the visual grammar at ${width}px in ${theme}`, async ({ page, request }) => {
      await useTheme(page, theme, width);
      const sessionId = await visualConversation(request);
      await page.goto(appPath('/'));
      await expect(page.locator('h1, h2').first()).toBeVisible();
      await expect(
        page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`),
        'the app rail must list the conversation this audit owns',
      ).toBeVisible();

      const findings: Finding[] = [];
      const chrome = new Map<string, string>();

      chrome.set('agents', await auditHere(page, 'app /agents', width, findings));

      // The user menu is shell too — its theme and language controls are the
      // one place the app sets a segmented control inside a popup, and a popup
      // is exactly what the page walk above never opens. Measured there: the
      // zh theme labels ran out of their equal-split segments.
      const userMenu = page.locator('[data-slot="sidebar-footer"] button').first();
      if (await userMenu.count()) {
        await userMenu.click();
        const menu = page.getByRole('menu');
        await expect(menu, 'the user menu must open from the rail foot').toBeVisible();
        // Let the open transition settle: a popup measured mid-scale reports
        // every width 5% short.
        await page.waitForTimeout(400);
        const m = await measure(page);
        if (m.overflowingLabels.length) {
          findings.push({ where: 'app user menu', what: 'a label that runs out of its control', detail: m.overflowingLabels.join(', ') });
        }
        await page.keyboard.press('Escape');
        await expect(menu).toHaveCount(0);
      }

      const assistants = page.getByRole('tab', { name: /assistants/i })
        .or(page.getByRole('button', { name: /^assistants$/i }))
        .first();
      if (await assistants.count()) {
        await assistants.click();
        // Wait for the route AND for the panel to have mounted its heading.
        // Clicking a tab and measuring is the same race as clicking a nav item
        // and counting rows: the click returns before React has committed, and
        // a walk that measures there reports an empty page for a full one.
        await page.waitForFunction(() => window.location.pathname === '/assistants').catch(() => undefined);
        await page
          .waitForFunction(() => document.querySelectorAll('h1').length === 1, undefined, { timeout: 8_000 })
          .catch(() => undefined);
        chrome.set('assistants', await auditHere(page, 'app /assistants', width, findings));
      }

      // A conversation is the surface with the most type on it. This test owns
      // one with a completed real turn, so parallel cleanup elsewhere cannot
      // turn the most important half of the audit into an announced false pass.
      await openSessionView(page, sessionId);
      // `run-view` is the frame; the transcript arrives with its own fetch.
      // Counting on arrival reported 0 for a conversation whose two messages
      // were in the DOM a moment later, so wait for the first block first.
      await expect(page.locator('[data-testid$="-message"]').first()).toBeVisible({ timeout: 30_000 });
      const blocks = await page.locator('[data-testid$="-message"]').count();
      expect(blocks, 'the owned conversation must render both sides of its turn')
        .toBeGreaterThanOrEqual(2);
      // eslint-disable-next-line no-console
      console.log(`visual-grammar: conversation walked has ${blocks} message blocks`);
      chrome.set('session', await auditHere(
        page,
        'app /sessions/:id',
        width,
        findings,
        { requestsMayRemainOpen: true },
      ));

      const drift = shellDrift(chrome, 'app shell');
      if (drift) findings.push(drift);
      report(findings);
    });

    test(`console obeys the visual grammar at ${width}px in ${theme}`, async ({ page }) => {
      await useTheme(page, theme, width);

      await page.goto(appPath(CONSOLE_ENTRY));
      await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

      const routes = await consoleRoutes(page);
      expect(routes.length, 'the console rail must expose its sections').toBeGreaterThan(3);

      const findings: Finding[] = [];
      const chromeByRoute = new Map<string, string>();
      const recordCardType = new Map<string, string>();
      // Where the records themselves live, which is not always the list they
      // were opened from: the errors list opens the session an error came from,
      // and /manage/errors/<id> is not a route at all.
      const recordRoutes = new Set<string>();

      for (const route of routes) {
        await page.locator(`[data-slot="sidebar-content"] a[href="${route}"]`).click();
        await page.waitForFunction((r) => window.location.pathname === r, route);
        chromeByRoute.set(route, await auditHere(page, route, width, findings));

        // And one record behind it. A list and the page it opens are different
        // surfaces with different content, and every defect this spec was
        // written for lives on both — the one that prompted the clipped-text
        // check was on a record page the walk could not reach.
        // Wait for a row rather than sampling for one: the table is a skeleton
        // until its fetch lands, and skeleton rows carry no tabindex. Counting
        // straight after the navigation found zero rows on every list except
        // the one that happened to be loaded before the walk started — so the
        // walk reported success having opened exactly one record.
        const row = page.locator('tr[tabindex], [role="row"][tabindex]').first();
        await row.waitFor({ state: 'visible', timeout: 4_000 }).catch(() => undefined);
        if (!(await row.count())) continue;
        await row.click();
        await page.waitForFunction((r) => window.location.pathname !== r, route).catch(() => undefined);
        if (new URL(page.url()).pathname === route) continue;
        // A record is /manage/<section>/<key> and nothing else is: some rows
        // open a section rather than a record, and taking their URL apart the
        // same way names a route that does not exist.
        const opened = new URL(page.url()).pathname.split('/').filter(Boolean);
        if (opened.length === 3) recordRoutes.add(`/${opened[0]}/${opened[1]}`);
        // Wait for the record to arrive before measuring it. "There is a
        // heading" is not an arrival signal — the loading state has one too —
        // so the record page names its own in-flight state and this waits for
        // that name to leave the DOM.
        //
        // Not the trail's last segment: an environment's URL segment IS its
        // name, so waiting for the crumb to stop matching the URL key never
        // finishes there, and swallowing that timeout hands the walk a page
        // still mid-load, whose placeholder id it files as an escaped key.
        await page
          .locator('[data-slot="loading-state"]')
          .waitFor({ state: 'detached', timeout: 15_000 });
        chromeByRoute.set(`${route}/:id`, await auditHere(page, `${route}/:id`, width, findings));
        const cardType = (await measure(page)).cardType;
        if (cardType) recordCardType.set(`${route}/:id`, cardType);
        await page.goBack();
        await page.waitForFunction((r) => window.location.pathname === r, route).catch(() => undefined);
      }

      // A walk that opened no record measured no record page, and every check
      // below it would agree with everything by agreeing with nothing.
      expect(
        [...recordRoutes],
        'the walk opened no record at all — the record-page half of this audit ' +
          'measured nothing, and its silence is not a pass',
      ).not.toEqual([]);

      // Which record pages this actually reached. A section with no records has
      // no row to open, so the walk never sees its record page — and on a fresh
      // deployment that is most of them. Said out loud, because a check that
      // covered two of seven and a check that covered seven of seven print the
      // same tick.
      // eslint-disable-next-line no-console
      console.log(`visual-grammar: record pages reached — ${[...recordRoutes].join(', ')}`);

      // The create surface behind each section, where there is one.
      const created: string[] = [];
      for (const route of recordRoutes) {
        if (!(await createSurface(page, route))) continue;
        created.push(route);
        chromeByRoute.set(`${route}/new`, await auditHere(page, `${route}/new`, width, findings));
      }
      // eslint-disable-next-line no-console
      console.log(`visual-grammar: create pages reached — ${created.join(', ') || 'none'}`);

      const named = new Map<string, string[]>();
      for (const route of recordRoutes) {
        const heading = await missingRecordHeading(page, route, MISSING_KEY);
        expect(
          heading,
          `${route}/${MISSING_KEY}: the heading is the key from the URL. The trail's ` +
            `last segment already falls back to that same segment, so the page says ` +
            `the opaque thing twice and names its subject zero times (§1).`,
        ).not.toBe(MISSING_KEY);
        named.set(heading, [...(named.get(heading) || []), route]);
      }
      expect(
        [...named.entries()]
          .filter(([, routes]) => routes.length > 1)
          .map(([heading, routes]) => `${routes.join(' and ')} both call themselves “${heading}”`),
        'a record page with no record to name names its own type, so no two record ' +
          'types can share a heading — a shared one is a placeholder standing where ' +
          'the subject belongs (§1)',
      ).toEqual([]);

      const drift = shellDrift(chromeByRoute, 'console shell');
      if (drift) findings.push(drift);
      const typeDrift = shellDrift(recordCardType, 'record cards');
      if (typeDrift) findings.push(typeDrift);
      report(findings);
    });
  }
}

// Before anything is measured, prove this product is behind the page. The walk
// itself cannot tell: it drives a frontend, and a frontend renders whatever it
// is given.
test.beforeAll(async ({ request }) => {
  await refuseIfNotTheDeployment(async (url, headers) => ({
    status: (await request.get(url, { failOnStatusCode: false, headers })).status(),
  }));
});

/**
 * The two shells wear the same brand.
 *
 * `shellDrift` compares routes WITHIN a walk — the app against itself, the
 * console against itself — so a difference between the two was invisible to
 * it by construction, and each shell reported as internally consistent while
 * the corner of the screen twitched on every crossing. Nobody sees both at the
 * same instant, which is exactly why a machine has to.
 *
 * One pass, outside the width/theme loops: a brand block that differs between
 * shells differs at every width and in either theme.
 */
test('the app and the console are one shell', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const read = async (route: string) => {
    await page.goto(appPath(route));
    await page.waitForLoadState('networkidle');
    await page
      .waitForFunction(() => document.querySelectorAll('h1').length === 1, undefined, {
        timeout: 10_000,
      })
      .catch(() => undefined);
    return (await measure(page)).chrome;
  };

  const app = await read('/');
  const console_ = await read(CONSOLE_ENTRY);

  expect(app.railFound && console_.railFound, 'a shell with no rail was not measured').toBe(true);
  expect(
    {
      mark: console_.mark,
      markAt: console_.markAt,
      wordmark: console_.wordmark,
      railItem: console_.railItem,
    },
    'the brand block and the rail are one shell wearing two names; whatever a ' +
      'shell varies around them must not move them',
  ).toEqual({
    mark: app.mark,
    markAt: app.markAt,
    wordmark: app.wordmark,
    railItem: app.railItem,
  });
});
/**
 * A control's size belongs to the BAND it stands in.
 *
 * The console has three: the page's heading with its actions, a card's foot,
 * and a row inside a table. A reader crossing from a list to the record it
 * opens sees the same band twice in a row, so a size that changes between them
 * is the most visible inconsistency the surface can have — and it is exactly
 * what happened: `New trigger` on the list stood 32px and `Delete` on the
 * record it opened stood 28, because the record pages and the create pages
 * asked for `size="sm"` while the lists did not.
 *
 * Measured across every console route in one pass, because the defect only
 * exists BETWEEN pages: each page was internally consistent.
 */
test('the heading band is one band on every page', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.goto(appPath(CONSOLE_ENTRY));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

  // The app's pages sit in the same band as the console's, and were the ones
  // out: a page-body heading was 24px in the console and on the login page,
  // 20px on the agent home and 14px — one step over body text — on the
  // assistant picker.
  const surfaces = [...(await routesFromRail(page)), '/', '/assistants'];
  const heights = new Map<string, string[]>();
  const headings = new Map<string, string[]>();

  for (const route of surfaces) {
    await page.goto(appPath(route));
    await page.waitForTimeout(2600);
    await page
      .locator('[data-slot="loading-state"]')
      .waitFor({ state: 'detached', timeout: 10_000 })
      .catch(() => undefined);
    const found = await page.evaluate(() => {
      const heading = document.querySelector('h1');
      if (!heading) return [];
      const band = heading.getBoundingClientRect().top;
      return [
        ...new Set(
          [...document.querySelectorAll('button, a[data-slot=button]')]
            .filter((el) => {
              const r = el.getBoundingClientRect();
              // Beside the heading, to its right: the page's own actions.
              return r.height > 8 && Math.abs(r.top - band) < 60 && r.left > 400;
            })
            .map((el) => `${Math.round(el.getBoundingClientRect().height)}px`),
        ),
      ];
    });
    for (const h of found) heights.set(h, [...(heights.get(h) || []), route]);

    // A heading in a top bar is a different band — a 24px line does not belong
    // in a 56px strip — so the two are counted apart rather than forced level.
    const heading = await page.evaluate(() => {
      const h = document.querySelector('h1');
      if (!h || h.closest('header')) return null;
      return getComputedStyle(h).fontSize;
    });
    if (heading) headings.set(heading, [...(headings.get(heading) || []), route]);
  }

  // eslint-disable-next-line no-console
  console.log(`heading band: ${[...heights.keys()].join(', ') || 'no actions found'}`);
  expect(
    [...heights.keys()].length,
    `a page's actions sit in the same band on every page, so they are one height. ` +
      `Saw ${[...heights.entries()].map(([h, r]) => `${h} on ${r.join(', ')}`).join(' | ')}`,
  ).toBeLessThan(2);

  // eslint-disable-next-line no-console
  console.log(`page heading: ${[...headings.keys()].join(', ')}`);
  expect(
    [...headings.keys()].length,
    `a page states its subject once (§1), in one size wherever it does it. ` +
      `Saw ${[...headings.entries()].map(([h, r]) => `${h} on ${r.join(', ')}`).join(' | ')}`,
  ).toBeLessThan(2);
});
