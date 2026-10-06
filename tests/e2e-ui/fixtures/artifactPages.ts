// Page source is supplied verbatim to the Agent. Assertions read the resulting
// DOM inside the opaque-origin frame, never the prompt echoed in a transcript.
export function isolatedPage(marker: string, scriptMarker: string): string {
  return `<!doctype html><html><head><title>Isolation probe</title></head><body>
<p id="marker">${marker}</p><p id="script"></p><p id="cookie"></p><p id="parent"></p>
<script>
document.getElementById('script').textContent = ${JSON.stringify(scriptMarker)};
try { void document.cookie; document.getElementById('cookie').textContent = 'cookie-accessible'; }
catch (error) { document.getElementById('cookie').textContent = 'cookie-blocked:' + error.name; }
try { void parent.document; document.getElementById('parent').textContent = 'parent-accessible'; }
catch (error) { document.getElementById('parent').textContent = 'parent-blocked:' + error.name; }
</script></body></html>`;
}
export function simplePage(marker: string): string {
  return `<!doctype html><html><head><title>Marker</title></head><body><p id="marker">${marker}</p></body></html>`;
}
export function samplePage(prompt: string): string {
  return `<!doctype html><html><head><title>Model request</title></head><body>
<button id="ask" disabled>Ask model</button><p id="answer"></p><p id="error"></p>
<script>(async () => {
const sample = await window.astrabox.use('sample');
if (!sample) throw new Error('sample capability was not granted');
document.getElementById('ask').disabled = false;
document.getElementById('ask').onclick = async () => {
  try { const answer = await sample.sample(${JSON.stringify(prompt)}, {maxTokens: 100});
    document.getElementById('answer').textContent = answer.text; }
  catch (error) { document.getElementById('error').textContent = error.code + ':' + error.message; }
};
})();</script></body></html>`;
}
export function agentPage(prompt: string): string {
  return `<!doctype html><html><head><title>Refresh data</title></head><body>
<button id="refresh" disabled>Refresh</button><p id="value"></p><p id="run"></p><p id="error"></p>
<script>(async () => {
const agent = await window.astrabox.use('agent');
const db = await window.astrabox.use('db');
if (!agent || !db) throw new Error('agent and db capabilities must be granted');
db.doc('live/status').onSnapshot(snapshot => {
  document.getElementById('value').textContent = snapshot.exists ? snapshot.data().value : 'empty';
}, error => { document.getElementById('error').textContent = error.code + ':' + error.message; });
document.getElementById('refresh').disabled = false;
document.getElementById('refresh').onclick = async () => {
  try { const run = await agent.run({prompt: ${JSON.stringify(prompt)}});
    document.getElementById('run').textContent = run.sessionId; }
  catch (error) { document.getElementById('error').textContent = error.code + ':' + error.message; }
};
})();</script></body></html>`;
}

export function livePage(marker: string): string {
  return `<!doctype html><html><head><title>Shared notes</title></head><body>
<p id="marker">${marker}</p><p id="viewer"></p><p id="peers">0</p><p id="ping"></p>
<label>Note <input id="note"></label><button id="add" disabled>Add note</button><ul id="notes"></ul>
<label>Private field <input id="private"></label><button id="save" disabled>Save private</button>
<button id="send" disabled>Send ping</button><p id="result"></p><p id="error"></p>
<script>(async () => {
const db = await window.astrabox.use('db');
const room = await window.astrabox.use('room');
const user = await window.astrabox.use('user');
if (!db || !room || !user) throw new Error('db, room and user must be granted');
document.getElementById('viewer').textContent = user.id() || 'guest';
db.collection('notes').onSnapshot(snapshot => {
  const list = document.getElementById('notes'); list.replaceChildren();
  snapshot.docs.forEach(doc => { const li = document.createElement('li'); li.textContent = doc.data().value; list.appendChild(li); });
}, error => { document.getElementById('error').textContent = error.code + ':' + error.message; });
room.onPeers(peers => { document.getElementById('peers').textContent = String(peers.length); });
room.on('ping', data => { document.getElementById('ping').textContent = data.value; });
await room.presence({viewing: true});
for (const id of ['add', 'save', 'send']) document.getElementById(id).disabled = false;
async function act(fn) {
  try { await fn(); document.getElementById('result').textContent = 'saved'; }
  catch (error) { document.getElementById('result').textContent = error.code + ':' + error.message; }
}
document.getElementById('add').onclick = () => act(() => db.collection('notes').add({value: document.getElementById('note').value}));
document.getElementById('save').onclick = () => act(() => db.doc('data/users/' + user.id() + '/profile').set({value: document.getElementById('private').value}));
document.getElementById('send').onclick = () => act(() => room.emit('ping', {value: document.getElementById('note').value}));
})();</script></body></html>`;
}
