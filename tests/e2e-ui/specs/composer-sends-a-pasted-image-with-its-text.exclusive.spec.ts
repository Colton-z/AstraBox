/**
 * A picture pasted into the composer reaches the model, and stays in the record.
 *
 * Copying from a slide deck, a document, or a screenshot tool puts an image on
 * the clipboard next to the text that came with it. Everything about that trip
 * has a way to lose one half of it: the paste can be cancelled to take the
 * file and drop the words, the send can carry the words and drop the file, the
 * engine can be handed a shape it does not read, and a reload can rebuild the
 * message from a record that only kept prose.
 *
 * This walks both a mixed clipboard and a caption-free image on real boxes:
 *   the clipboard  →  the composer  →  the input  →  the engine
 *                  →  the session's history  →  another open page, a fresh page
 *
 * The image is a solid red square rather than a placeholder byte string so
 * that the engine receives a valid image. The reply is recorded, not used as
 * a colour-recognition oracle: this journey proves delivery and persistence,
 * not the deployment's vision capability.
 */
import { execFileSync } from 'node:child_process';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { mirrorSseBodies, sessionStreamBodies } from '../fixtures/sseBodies';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// 16×16, every pixel #FF0000. Small enough to paste inline, real enough that a
// vision model has something to answer about.
const RED_SQUARE_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mP4z8BAEmIY1TCqYfhqAACQ+f8B8u7oVwAAAABJRU5ErkJggg==';

const sessions = trackSessions();
let agentId = '';

onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

async function dispatchClipboard(page: Page, options: { text?: string; includeImage: boolean }) {
  return page.getByTestId('composer-prompt').evaluate((element, clipboardOptions) => {
    const textarea = element as HTMLTextAreaElement;
    const clipboard = new DataTransfer();
    if (clipboardOptions.text !== undefined) {
      clipboard.setData('text/plain', clipboardOptions.text);
      clipboard.setData('text/html', `<p>${clipboardOptions.text}</p>`);
    }
    if (clipboardOptions.includeImage) {
      const bytes = Uint8Array.from(atob(clipboardOptions.base64), (c) => c.charCodeAt(0));
      clipboard.items.add(new File([bytes], 'red-square.png', { type: 'image/png' }));
    }
    const event = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: clipboard,
    });
    const accepted = textarea.dispatchEvent(event);
    // Synthetic paste has no browser insertion. Preserve the original
    // fixture's default action, but only when the product did not cancel it.
    if (accepted && !event.defaultPrevented) {
      const text = clipboard.getData('text/plain');
      textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, 'end');
      textarea.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        data: text,
        inputType: 'insertFromPaste',
      }));
    }
    return {
      defaultPrevented: event.defaultPrevented,
      itemKinds: Array.from(clipboard.items, (item) => `${item.kind}:${item.type}`),
      plainText: clipboard.getData('text/plain'),
    };
  }, { ...options, base64: RED_SQUARE_PNG_BASE64 });
}

async function provePastedImage(page: Page, request: APIRequestContext, withCaption: boolean, refuseAndResend = false) {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const agent = await api.createColdTestAgent(`__e2e_paste_image_${runId}`);
  agentId = String(agent.agent_id || '').trim();
  const session = await api.startConversation(agentId);
  const sessionId = String(session.session_id || '').trim();
  sessions.push(sessionId);
  const server = refuseAndResend ? requireServiceContainer(SERVER_CONTAINER_HANDLE) : '';
  const control = (enabled: boolean) => execFileSync('docker', [
    'exec', server, 'python', '-c',
    'import json,sys; from pathlib import Path; '
      + "p=Path('/data/runtime-preparation-user-actions.json'); "
      + 'ids=json.loads(p.read_text()) if p.exists() else []; '
      + 'ids=[sid for sid in ids if sid!=sys.argv[1]]; '
      + "p.write_text(json.dumps(ids+[sys.argv[1]] if sys.argv[2]=='yes' else ids))",
    sessionId, enabled ? 'yes' : 'no',
  ], { timeout: 30_000, stdio: 'pipe' });
  await api.waitForSessionReady(sessionId);
  if (refuseAndResend) {
    test.info().annotations.push({ type: 'deployment_shape', description: 'runtime-preparation provider' });
    await mirrorSseBodies(page);
  }
  await openSessionView(page, sessionId);

  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled();

  let uploadRequests = 0;
  let inputRequests = 0;
  page.on('request', (outbound) => {
    if (outbound.method() !== 'POST') return;
    const path = new URL(outbound.url()).pathname;
    if (path.endsWith(`/sessions/${sessionId}/files/upload`)) uploadRequests += 1;
    if (path.endsWith(`/sessions/${sessionId}/inputs`)) inputRequests += 1;
  });

  const caption = withCaption
    ? `E2E_PASTED_IMAGE_${runId}: 这张图是纯色的。只回答它的颜色英文单词，不要使用任何工具。`
    : '';
  if (withCaption) {
    const textOnlyPaste = await dispatchClipboard(page, { text: caption, includeImage: false });
    expect(textOnlyPaste.defaultPrevented).toBe(false);
    await expect(composer).toHaveValue(caption);
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
    await composer.fill('');
  }
  const paste = await dispatchClipboard(page, {
    ...(withCaption ? { text: caption } : {}),
    includeImage: true,
  });
  expect(paste.defaultPrevented).toBe(!withCaption);
  expect(paste.plainText).toBe(caption);
  expect(paste.itemKinds).toEqual(withCaption
    ? ['string:text/plain', 'string:text/html', 'file:image/png']
    : ['file:image/png']);
  await expect(composer).toHaveValue(caption);
  const attachment = page.getByTestId('composer-attachment');
  await expect(attachment).toHaveCount(1);
  await expect(attachment).toHaveAttribute('data-filename', 'red-square.png');

  const imageBlock = {
    type: 'image',
    source: { type: 'base64', media_type: 'image/png', data: RED_SQUARE_PNG_BASE64 },
  };
  // The picture as an AI SDK file part: what the page renders, live and from history.
  const imagePart = {
    type: 'file',
    mediaType: 'image/png',
    url: `data:image/png;base64,${RED_SQUARE_PNG_BASE64}`,
  };
  const expectedContent = [...(withCaption ? [{ type: 'text', text: caption }] : []), imageBlock];
  const assistantsBefore = await api.assistantCount(sessionId);
  // A second page on the same conversation, opened before the send: it never
  // sends this message, and still shows it once the runtime answers it.
  const observer = withCaption ? null : await page.context().newPage();
  let inputId = '';
  let clientMessageId = '';
  let observerInputRequests = 0;
  let observerUploadRequests = 0;
  try {
    if (observer) {
      observer.on('request', (outbound) => {
        if (outbound.method() !== 'POST') return;
        const path = new URL(outbound.url()).pathname;
        if (path.endsWith(`/sessions/${sessionId}/inputs`)) observerInputRequests += 1;
        if (path.endsWith(`/sessions/${sessionId}/files/upload`)) observerUploadRequests += 1;
      });
      await openSessionView(observer, sessionId);
      await expect(observer.getByTestId('user-message')).toHaveCount(0);
      await expect(observer.getByTestId('composer-attachment')).toHaveCount(0);
    }

    if (refuseAndResend) control(true);
    await expect(page.getByTestId('composer-submit')).toBeEnabled();
    const [receiptResponse] = await Promise.all([
      page.waitForResponse((response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname.endsWith(`/sessions/${sessionId}/inputs`)),
      page.getByTestId('composer-submit').click(),
    ]);
    expect(receiptResponse.ok()).toBe(true);
    const sentBody = receiptResponse.request().postDataJSON();
    expect(sentBody.content).toEqual(expectedContent);
    const receiptBody = await receiptResponse.json();
    const receipt = receiptBody.data ?? receiptBody;
    inputId = String(receipt.input_id ?? '');
    clientMessageId = String(receipt.client_message_id ?? '');
    expect(inputId).toMatch(/^[0-9a-f-]{36}$/);
    expect(clientMessageId, 'the receipt names the message the page sent').toBe(String(sentBody.client_message_id));

    // The attachment leaves the composer when the send takes it, not before,
    // and the picture is in the transcript from the moment it is sent.
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
    const sentImage = page.locator(`[data-message-id="${clientMessageId}"]`).getByTestId('message-image');
    await expect(sentImage).toHaveCount(1);
    await expect(sentImage).toHaveAttribute('src', imagePart.url);

    if (refuseAndResend) {
      const refusedClientId = clientMessageId;
      const refusedInputId = inputId;
      const refusedRow = page.locator(`[data-message-id="${refusedClientId}"]`);
      const refusal = refusedRow.getByTestId('user-message-failure');
      await expect(refusal).toContainText('Authorize this Agent to continue', { timeout: 120_000 });
      const failedHistory = (await api.getMessages(sessionId)).messages;
      expect(failedHistory).toHaveLength(1);
      expect(failedHistory[0].metadata?.state).toBe('failed');
      expect(failedHistory[0].metadata?.failure?.code).toBe('TURN_PREPARATION_USER_ACTION_REQUIRED');
      expect(failedHistory[0].parts.filter((part) => part.type === 'file')).toEqual([imagePart]);
      const authorization = page.waitForResponse((response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/user-action`));
      await page.getByRole('button', { name: 'I have authorized', exact: true }).click();
      const authorized = await authorization;
      expect(authorized.ok()).toBe(true);
      expect(authorized.request().postDataJSON()).toEqual({ action_id: 'authorize' });
      expect((await authorized.json()).data.ready).toBe(true);
      const resentResponse = page.waitForResponse((response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/inputs`));
      await refusal.getByRole('button', { name: /^(Send again|重新发送)$/ }).click();
      const resent = await resentResponse;
      expect(resent.status()).toBe(200);
      const resentBody = resent.request().postDataJSON();
      expect(resentBody.content, 'Send again keeps every original image byte and its media type').toEqual(expectedContent);
      expect(resentBody.client_message_id).not.toBe(refusedClientId);
      const secondReceipt = (await resent.json()).data;
      expect(secondReceipt.duplicate).toBe(false);
      expect(secondReceipt.input_id).not.toBe(refusedInputId);
      inputId = String(secondReceipt.input_id);
      clientMessageId = String(secondReceipt.client_message_id);
      expect(clientMessageId).toBe(resentBody.client_message_id);
      await expect(refusedRow.getByTestId('message-image')).toHaveAttribute('src', imagePart.url);
      await expect(page.locator(`[data-message-id="${clientMessageId}"]`).getByTestId('message-image'))
        .toHaveAttribute('src', imagePart.url);
      await test.info().attach('refused-image-resend', {
        body: JSON.stringify({ failedHistory, original: receipt, resentBody, secondReceipt }),
        contentType: 'application/json',
      });
    }

    if (observer) {
      const observedMessage = observer.locator(`[data-message-id="${clientMessageId}"]`);
      await expect(observedMessage, 'the other page shows the message once its response starts').toHaveCount(1, { timeout: 120_000 });
      const observedImage = observedMessage.getByTestId('message-image');
      await expect(observedImage, 'with its picture').toHaveCount(1);
      await expect(observedImage).toHaveAttribute('data-media-type', 'image/png');
      await expect(observedImage).toHaveAttribute('src', imagePart.url);
      expect(observerInputRequests, 'the other page did not send this input').toBe(0);
      expect(observerUploadRequests).toBe(0);
      await expect(observer.getByTestId('composer-attachment')).toHaveCount(0);
    }
  } finally {
    if (refuseAndResend) {
      control(false);
      await test.info().attach('image-resend-browser-stream', {
        body: JSON.stringify(await sessionStreamBodies(page)), contentType: 'application/json',
      });
    }
    if (observer) await observer.close({ runBeforeUnload: false });
  }

  const answer = await api.waitForAssistantMessageCount(sessionId, assistantsBefore);
  // Whether the model can describe the picture is not asserted here: this
  // deployment's gateway serves one text-only model, so a colour it cannot see
  // would fail for a reason that says nothing about this code. What the turn
  // does prove is that the engine accepted the image and ran to a clean
  // terminal — a content shape it rejected would end the turn, not answer it.
  test.info().annotations.push({
    type: 'assistant_reply_to_image',
    description: messageText(answer),
  });
  const settled = await api.waitForSession(sessionId, (session) => (
    session.state === 'READY'
    && !session.current_responses?.length
    && session.last_response?.status === 'ended'
  ));
  expect(
    settled.last_response?.outcome,
    'the engine must finish the response it was handed an image on',
  ).toBe('completed');

  const history = await api.getMessages(sessionId, 50);
  const sentMessages = history.messages.filter((message) => (
    message.role === 'user' && message.id === clientMessageId
  ));
  expect(sentMessages, 'the input is in history once, under the id the page sent it with').toHaveLength(1);
  const sent = sentMessages[0];
  expect(sent.metadata?.input_id).toBe(inputId);
  expect(messageText(sent)).toBe(caption);
  expect(
    sent.parts.filter((part) => part.type === 'file'),
    'history keeps the picture, not just the caption that came with it',
  ).toEqual([imagePart]);

  const nativeEntries = (await api.adminSessionTranscript(sessionId))
    .split('\n').filter((line) => line.trim()).map((line) => JSON.parse(line));
  // The runner keeps the platform FIFO identity separate from its per-attempt
  // vendor UUID. This fresh conversation submits exactly one image input.
  const nativeInput = nativeEntries.filter((entry) => entry.type === 'user'
    && Array.isArray(entry.message?.content)
    && entry.message.content.some((block: { type: string }) => block.type === 'image'));
  expect(nativeInput, 'the single image input must reach the engine native SessionStore once').toHaveLength(1);
  expect(nativeInput[0].uuid, 'the native input retains its own message identity').toMatch(/^[0-9a-f-]{36}$/);
  expect(nativeEntries.filter((entry) => entry.uuid === nativeInput[0].uuid),
    'the native message identity must occur exactly once').toHaveLength(1);
  expect(nativeInput[0].message.content.filter((block: { type: string }) => block.type === 'image'))
    .toEqual([imageBlock]);

  await page.reload({ waitUntil: 'domcontentloaded' });
  const rendered = page.getByTestId('message-image');
  await expect(rendered, 'a fresh page rebuilds the picture from that record')
    .toHaveCount(refuseAndResend ? 2 : 1);
  for (const image of await rendered.all()) {
    await expect(image).toHaveAttribute('data-media-type', 'image/png');
    await expect(image).toHaveAttribute('src', `data:image/png;base64,${RED_SQUARE_PNG_BASE64}`);
  }
  expect(inputRequests, 'each click submits exactly one input').toBe(refuseAndResend ? 2 : 1);
  expect(uploadRequests, 'native image inputs do not upload workspace files').toBe(0);
}

test('a pasted image is sent with its text, seen by the model, and kept across reload', async ({ page, request }) => {
  await provePastedImage(page, request, true);
});

test('an image-only clipboard sends a native image without a caption and survives reload', async ({ page, request }) => {
  await provePastedImage(page, request, false);
});

test('a refused image-only input sends its original bytes again after authorization', async ({ page, request }) => {
  await provePastedImage(page, request, false, true);
});
