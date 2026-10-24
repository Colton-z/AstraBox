# Public-client desktop login and a running DSH session

This main-process example pins DSH **0.2.0-rc.2**, the version in the reported
Windows PoC. It uses the supplier's full `sdk` profile, native credential store,
`credentials/reference-updated` notification and SDK session protocol. The
`sdk-minimal` profile omits managed credentials and cannot implement this flow.
It does not implement a renderer, installer or OS keychain.

```bash
cd examples/oidc-dsh
npm ci
```

The repository's Node version is in `.nvmrc`. In an application, import:

```javascript
import { signIn, refresh } from './login.mjs';
import { DesktopSession } from './desktop-session.mjs';

const { config, tokens } = await signIn({
  issuer: 'https://login.example.com',
  clientId: 'desktop-assistant',
  redirectURI: 'http://127.0.0.1:43127/callback',
});
const session = new DesktopSession({
  // A NEW application-owned private directory; this example refuses reuse.
  home: '/your/private/application-data/dsh-session',
  cwd: '/your/task/workspace',
  modelBaseURL: 'https://models.example.com',
  model: 'company-chat',
});
try {
  await session.start(tokens.access_token);
  const first = await session.ask('Remember the label maple. Reply briefly.');
  console.log(first.finalResponse);
  // Store/rotate refresh tokens using your OS credential manager. Keep model
  // submissions gated until this operation resolves. Never print tokens.
  const nextTokens = await refresh(config, tokens.refresh_token, session);
  // Persist nextTokens.refresh_token when the IdP rotates it.
  const second = await session.ask('What label did I ask you to remember?');
  console.log(second.finalResponse);
} finally {
  await session.close();
}
```

`signIn` opens the system browser, listens only on the registered loopback
address, and delegates PKCE S256, state, nonce and OIDC token verification to
`openid-client`. No Client Secret is sent by the desktop. Registration, consent,
secure refresh-token storage and refresh scheduling belong to the desktop main
process. Do not forward the token response to an untrusted renderer.

`DesktopSession` owns one DSH process and one SDK session across both prompts.
It atomically replaces its private native `.credentials.yaml`; DSH's own watcher
reloads it. The small `credential-ack.mjs` plugin observes that native update,
resolves the actual in-process credential and writes only its hash and process
ID. The parent waits for that hash and the **same PID** before allowing the next
prompt. A ten-second missing acknowledgement fails visibly; it does not silently
reuse the old token or restart the child. Do not run `ask` concurrently with a
refresh. An already running inference operation keeps the credential it resolved
when it started; an update applies to the next operation.

The parent must not launch `DESKTOP_MODEL_ACCESS_TOKEN` as an environment value:
in this pinned DSH release inherited values override the writable native store.
Changing the parent's environment never updates an already running child. The
example deletes that reference from the child environment explicitly.

The native credential file is plaintext and must remain private to the OS user.
The example creates a new mode-0700 directory and mode-0600 files on Unix. On
Windows, use the app's user-private application-data directory and verify its
ACL before launching; POSIX mode bits do not establish Windows ACLs. Integrate
refresh-token storage with the Windows Credential Manager or the desktop
framework's OS-backed secure storage. Remove the owned temporary DSH home only
after closing the process; do not delete a user's existing DSH home.

For Casdoor 3.128.0 keep the default client-ID audience. That version aliases its
ID Token and Access Token; setting `resource` to another audience also changes
the ID Token, which a conformant OIDC client will reject. The model gateway
checks an actual active Access Token record, not a made-up token-purpose claim.

The example disables the native `plugin-package-inventory-deepseek` contribution
with its supported `enabled: false` setting. Its `dsh_plugin_packages` field is
for the official DeepSeek service and is outside the standard gateway Messages
contract. Managed credentials, tools and native session persistence remain in
the full SDK profile.

The pinned DSH API-key transport sends the Access Token in `x-api-key` for
Messages. LiteLLM accepts this native protocol header and validates the same
OIDC credential. Ordinary Chat Completions clients use `Authorization: Bearer`.
The header name does not issue or require a separate Virtual Key.

The example uses text Messages. The independent inference grant does not include
DSH image Files uploads. Configuration/source review is not acceptance on a new
Windows version or a different DSH release. Keep deployment receipts and tested
platform/version boundaries explicit.
