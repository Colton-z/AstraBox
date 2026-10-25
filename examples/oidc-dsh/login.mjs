/** Authorization Code + PKCE for a public desktop client. */
import { createServer } from 'node:http';
import * as oidc from 'openid-client';
import open from 'open';

export async function signIn({ issuer, clientId, redirectURI, openBrowser = open }) {
  const redirect = new URL(redirectURI);
  if (redirect.protocol !== 'http:' || redirect.hostname !== '127.0.0.1' || !redirect.port) {
    throw new Error('Register an explicit http://127.0.0.1:<port>/callback redirect');
  }
  const config = await oidc.discovery(new URL(issuer), clientId, undefined, oidc.None());
  const verifier = oidc.randomPKCECodeVerifier();
  const state = oidc.randomState();
  const nonce = oidc.randomNonce();
  const authorization = oidc.buildAuthorizationUrl(config, {
    redirect_uri: redirect.href, scope: 'openid profile offline_access',
    code_challenge: await oidc.calculatePKCECodeChallenge(verifier),
    code_challenge_method: 'S256', state, nonce,
  });
  let finish;
  const callback = new Promise((resolve, reject) => { finish = { resolve, reject }; });
  const server = createServer((request, response) => {
    const current = new URL(request.url, redirect.origin);
    if (request.method !== 'GET' || current.pathname !== redirect.pathname) {
      response.writeHead(404).end();
      return;
    }
    // Neither code nor tokens are rendered in the page or printed to logs.
    response.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
    response.end('Return to the desktop application.');
    finish.resolve(current);
  });
  server.on('error', (error) => finish.reject(error));
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(Number(redirect.port), '127.0.0.1', resolve);
    });
    await openBrowser(authorization.href);
    const returned = await Promise.race([
      callback,
      new Promise((_, reject) => {
        const timer = setTimeout(() => reject(new Error('Desktop sign-in timed out')), 120_000);
        timer.unref();
      }),
    ]);
    const tokens = await oidc.authorizationCodeGrant(config, returned, {
      pkceCodeVerifier: verifier, expectedState: state, expectedNonce: nonce,
    });
    return { config, tokens };
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

export async function refresh(config, refreshToken, desktopSession) {
  const tokens = await oidc.refreshTokenGrant(config, refreshToken);
  await desktopSession.replaceAccessToken(tokens.access_token);
  return tokens;
}
