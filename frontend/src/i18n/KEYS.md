# i18n keys — conventions for AstraBox frontend

This is the contract every component author follows. The
runtime is `react-i18next` over `i18next`, initialized in `src/i18n/index.ts`.

## Namespaces

Translations are split into 7 namespaces. Each has one JSON file per language at
`src/i18n/locales/{en,zh}/<ns>.json`. The ordered, authoritative list (mirrored
by `NAMESPACES` in `index.ts`):

| Namespace   | Scope                                                              |
| ----------- | ----------------------------------------------------------------- |
| `common`    | Shared atoms reused everywhere (buttons, field labels, statuses). |
| `shell`     | App chrome: sidebar, nav, layout, top-level shell.                |
| `chat`      | Session / chat surface (composer, messages, tools, interaction).  |
| `manage`    | Manage area: agents, sessions, sandboxes, assistants, environments. |
| `agents`    | Agent gallery / agent detail (end-user facing agent surfaces).    |
| `misc`      | Anything that doesn't fit a namespace above.                      |

`common` is fully populated. The other five ship as empty `{}` stubs and are
populated as strings are externalized. Adding a namespace means: add the JSON file in BOTH
`en` and `zh`, the static import + `resources` entry in `index.ts`, the
`NAMESPACES` array entry, and a row here.

## Default namespace

`defaultNS` is `common`. A bare key like `t('save')` resolves against `common`.
To read from another namespace use the explicit `ns:key` form (below) — do not
rely on the default for non-common strings.

## Key convention: `t('ns:key')`

Always reference a key with its namespace prefix, e.g.

```ts
const { t } = useTranslation();
t('common:save');        // → "Save"
t('chat:composer.send'); // → namespace `chat`, key `composer.send`
```

Dotted keys are allowed and encouraged for grouping inside a namespace:

```json
{ "composer": { "send": "Send", "placeholder": "Type a message…" } }
```

`t('chat:composer.send')` reads the nested key. Keep nesting shallow (1–2
levels). Within one component you may also do
`const { t } = useTranslation('chat')` and then `t('composer.send')`, but the
`ns:key` form in the call site is the portable default.

## common atoms — DO NOT redefine

`common` holds the strings repeated across the app: `save`, `cancel`, `revert`,
`confirm`, `confirm_delete`, `delete`, `create`, `new`, `refresh`, `loading`,
`close`, `retry`, `all`, `enabled`, `disabled`, `enable`,
`disable`, `yes`, `no`, `copy`, `copied`, `name`, `model`,
`model_deployment_default`, `status`, `session`, `sandbox`, `engine`,
`created_at`, `updated_at`, `saved`, and `files`.

Components MUST reuse `t('common:<atom>')` for these and MUST NOT
re-add the same atom under another namespace. Only add a key to a feature
namespace when it is genuinely feature-specific.

## Interpolation & pluralization

Use `{{var}}` placeholders in JSON; pass values as the second arg to `t`:

```json
{ "selected_count": "{{count}} selected", "greeting": "Hi, {{name}}" }
```

```ts
t('manage:selected_count', { count: 3 }); // → "3 selected"
t('common:greeting', { name: user.name });
```

`count` is special — i18next does plural resolution from it. Provide the plural
variants when English needs them; suffix the key with i18next plural categories:

```json
{
  "item_count_one": "{{count}} item",
  "item_count_other": "{{count}} items"
}
```

```ts
t('manage:item_count', { count: n }); // picks _one / _other automatically
```

Chinese has a single plural form, so `zh` typically defines only the base /
`_other` key. HTML is not interpolated (`escapeValue: false` is safe because
React escapes); never inject markup through `t`.

## Language detection & switching

Init order is `['localStorage', 'navigator']`, cached to `localStorage` under
the key `astrabox-lang`. Net behavior: a Chinese-browser user auto-gets `zh`,
everyone else `en`, and a manual switch persists. The UI control is
`src/components/LanguageSwitcher.tsx` (calls `i18n.changeLanguage('en'|'zh')`).

`fallbackLng` is `en` and `returnNull` is `false`, so a not-yet-extracted key
renders its English fallback (or the key itself) — never a literal `null`.
