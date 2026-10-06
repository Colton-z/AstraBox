/** Enter the owned native Session's plan mode through its command Remote. */
import { expect } from '@playwright/test';

import type { AstraApi } from './astraApi';
import { terminalOutput } from './nativeMcpServer';

export async function enterDshNativePlanMode(
  api: AstraApi, sessionId: string, nativeSessionId: string,
): Promise<Record<string, unknown>> {
  const detail = await api.adminSessionDetail(sessionId);
  const home = String(detail.runtime_identity?.home_dir || '');
  expect(home).toMatch(/^\//);
  expect(nativeSessionId).not.toBe('');
  const config = Buffer.from(JSON.stringify({ home, nativeSessionId })).toString('base64');
  const source = `
import base64,http.cookiejar,json,urllib.parse,urllib.request,uuid
from pathlib import Path
config=json.loads(base64.b64decode('${config}'))
launch=(Path(config['home'])/'.deepseek-harness/web-url').read_text().strip()
parts=urllib.parse.urlsplit(launch)
assert parts.scheme=='http' and parts.hostname=='127.0.0.1' and parts.port
cookies=http.cookiejar.CookieJar()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(cookies))
try:
    with opener.open(launch,timeout=10) as response:
        assert response.status==200 and list(cookies)
    method='commands/execute'
    args={'agentId':config['nativeSessionId'],'line':'/plan','submittedAttachments':[]}
    request=urllib.request.Request(
        f'http://127.0.0.1:{parts.port}/api/{method}',
        headers={'Content-Type':'application/json'},
        data=json.dumps({'type':'client-request','rpcId':uuid.uuid4().hex,
                         'method':method,'payload':{'args':args}}).encode())
    with opener.open(request,timeout=10) as response:
        result=json.load(response)['result']
    assert result['ok'] is True
    value=result['value']
    assert value['result']['kind']=='success'
    print(json.dumps(value))
except Exception as exc:
    raise SystemExit(type(exc).__name__) from None
`;
  const value = JSON.parse(terminalOutput(await api.runTerminalCommand(
    sessionId, `python3 - <<'PY'\n${source}\nPY`, '/tmp', 30_000,
  ))) as Record<string, unknown>;
  expect(value).toMatchObject({ result: { kind: 'success', text: 'Plan mode on. Use /plan off to leave.' } });
  return value;
}
