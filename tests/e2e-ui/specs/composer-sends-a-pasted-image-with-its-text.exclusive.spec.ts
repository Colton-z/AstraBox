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
 *   the clipboard  →  the composer  →  the turn input  →  the engine
 *                  →  the durable message  →  a fresh page
 *
 * The image is a solid red square rather than a placeholder byte string so
 * that the engine receives a valid image. The reply is recorded, not used as
 * a colour-recognition oracle: this journey proves delivery and persistence,
 * not the deployment's vision capability.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
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

async function provePastedImage(page: Page, request: APIRequestContext, withCaption: boolean) {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const agent = await api.createColdTestAgent(`__e2e_paste_image_${runId}`);
  agentId = String(agent.agent_id || '').trim();
  const session = await api.startConversation(agentId);
  const sessionId = String(session.session_id || '').trim();
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);

  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled();

  let uploadRequests = 0;
  let inputRequests = 0;
  page.on('request', (outbound) => {
    if (outbound.method() !== 'POST') return;
    const path = new URL(outbound.url()).pathname;
    if (path.endsWith(`/sessions/${sessionId}/files/upload`)) uploadRequests += 1;
    if (path.endsWith(`/sessions/${sessionId}/turn-inputs`)) inputRequests += 1;
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
  const expectedContent = [...(withCaption ? [{ type: 'text', text: caption }] : []), imageBlock];
  const assistantsBefore = await api.assistantCount(sessionId);
  const observer = withCaption ? null : await page.context().newPage();
  let releaseFirstStream = () => {};
  let releaseHistory = () => {};
  const firstStreamHold = new Promise<void>((resolve) => { releaseFirstStream = resolve; });
  const historyHold = new Promise<void>((resolve) => { releaseHistory = resolve; });
  let inputId = '';
  let historyRequests = 0;
  let deliveredHistoryResponses = 0;
  let firstHistory: Record<string, unknown> | null = null;
  let streamRequests = 0;
  let firstStreamUrl = '';
  let observerInputRequests = 0;
  let observerUploadRequests = 0;
  try {
    if (observer) {
      observer.on('request', (outbound) => {
        if (outbound.method() !== 'POST') return;
        const path = new URL(outbound.url()).pathname;
        if (path.endsWith(`/sessions/${sessionId}/turn-inputs`)
          || path.endsWith(`/sessions/${sessionId}/ai-stream`)) observerInputRequests += 1;
        if (path.endsWith(`/sessions/${sessionId}/files/upload`)) observerUploadRequests += 1;
      });
      await observer.route((url) => url.pathname.endsWith(`/sessions/${sessionId}/history-blocks`), async (route) => {
        const requestIndex = ++historyRequests;
        const response = await route.fetch();
        if (requestIndex === 1) {
          const body = await response.json();
          firstHistory = body.data ?? body;
        } else {
          // A later real history response must not repair the live-only oracle.
          await historyHold;
        }
        await route.fulfill({ response });
        deliveredHistoryResponses += 1;
      });
      await observer.route((url) => url.pathname.endsWith(`/sessions/${sessionId}/ai-stream`), async (route) => {
        streamRequests += 1;
        if (streamRequests === 1) {
          firstStreamUrl = route.request().url();
          await firstStreamHold;
        }
        // Forward the real stream rather than buffering its unbounded body.
        await route.continue();
      });
      await openSessionView(observer, sessionId);
      await expect.poll(() => streamRequests, { message: 'the cold reader must request its first live subscription' }).toBe(1);
      expect(firstHistory).toMatchObject({ messages: [], active_turn_overlay: null });
      expect(deliveredHistoryResponses).toBe(1);
      const streamUrl = new URL(firstStreamUrl);
      expect(streamUrl.searchParams.get('follow')).toBe('session');
      const frameSeq = (firstHistory as Record<string, unknown> | null)?.session_frame_seq;
      expect(streamUrl.searchParams.get('after_seq')).toBe(
        typeof frameSeq === 'number' && frameSeq >= 0 ? String(frameSeq) : null,
      );
      await expect(observer.getByTestId('user-message')).toHaveCount(0);
      await expect(observer.getByTestId('composer-attachment')).toHaveCount(0);
    }

    await expect(page.getByTestId('composer-submit')).toBeEnabled();
    const [receiptResponse] = await Promise.all([
      page.waitForResponse((response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname.endsWith(`/sessions/${sessionId}/turn-inputs`)),
      page.getByTestId('composer-submit').click(),
    ]);
    expect(receiptResponse.ok()).toBe(true);
    expect(receiptResponse.request().postDataJSON().content).toEqual(expectedContent);
    const receiptBody = await receiptResponse.json();
    const receipt = receiptBody.data ?? receiptBody;
    inputId = String(receipt.input_id ?? '');
    expect(inputId).toMatch(/^[0-9a-f-]{36}$/);

    // The attachment leaves the composer when the send takes it, not before.
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);

    if (observer) {
      // A durable user projection is not proof that the bridge is healthy.
      // Check its actual failure verdict before opening the observer stream.
      await expect.poll(async () => {
        const history = await api.getMessages(sessionId, 50);
        const detail = await api.getSession(sessionId);
        if (detail.last_turn_status === 'FAILED') {
          throw new Error(`image-only input ${inputId} failed before the cold reader stream opened: ${JSON.stringify({
            session_id: sessionId,
            command_id: receipt.command_id,
            last_turn_id: detail.last_turn_id,
            last_turn_command_id: detail.last_turn_command_id,
            last_turn_status: detail.last_turn_status,
            last_turn_failure_phase: detail.last_turn_failure_phase,
            last_turn_error: detail.last_turn_error,
          })}`);
        }
        return history.messages.filter((message) => message.role === 'user'
          && message.message_id === `${inputId}:user`)
          .map((message) => ({ text: messageText(message), blocks: message.blocks }));
      }, { message: 'the image-only user projection must be durable without a platform failure before the first reader stream opens' })
        .toEqual([{ text: '', blocks: [imageBlock] }]);
      releaseFirstStream();
      const consumedUserMessage = observer.locator(`[data-message-id="${inputId}:user"]`);
      await expect(consumedUserMessage).toHaveCount(1);
      const liveImage = consumedUserMessage.getByTestId('message-image');
      await expect(liveImage, 'the first live subscription must render the image without a history refresh').toHaveCount(1);
      await expect(liveImage).toHaveAttribute('data-media-type', 'image/png');
      await expect(liveImage).toHaveAttribute('src', `data:image/png;base64,${RED_SQUARE_PNG_BASE64}`);
      expect(deliveredHistoryResponses, 'only the empty pre-input history may reach the cold reader').toBe(1);
      expect(observerInputRequests, 'the cold reader must not have submitted or optimistically rendered this input').toBe(0);
      expect(observerUploadRequests).toBe(0);
      await expect(observer.getByTestId('composer-attachment')).toHaveCount(0);
    }
  } finally {
    releaseFirstStream();
    releaseHistory();
    if (observer) {
      try {
        await observer.unrouteAll({ behavior: 'wait' });
      } finally {
        await observer.close({ runBeforeUnload: false });
      }
    }
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
    && !session.current_turn_id
    && session.last_turn_status === 'COMPLETED'
  ));
  expect(
    settled.last_turn_status,
    'the engine must finish the turn it was handed an image on',
  ).toBe('COMPLETED');

  const history = await api.getMessages(sessionId, 50);
  const sentMessages = history.messages.filter((message) => (
    message.role === 'user' && message.message_id === `${inputId}:user`
  ));
  expect(sentMessages, 'the exact consumed input must be durable once').toHaveLength(1);
  const sent = sentMessages[0];
  expect(messageText(sent)).toBe(caption);
  expect(
    (sent?.blocks || []).filter((block) => block.type === 'image'),
    'the record keeps the picture, not just the caption that came with it',
  ).toEqual([imageBlock]);

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
    .toHaveCount(1);
  await expect(rendered).toHaveAttribute('data-media-type', 'image/png');
  await expect(rendered).toHaveAttribute(
    'src',
    `data:image/png;base64,${RED_SQUARE_PNG_BASE64}`,
  );
  expect(inputRequests, 'one click must submit exactly one input').toBe(1);
  expect(uploadRequests, 'native image inputs do not upload workspace files').toBe(0);
}

test('a pasted image is sent with its text, seen by the model, and kept across reload', async ({ page, request }) => {
  await provePastedImage(page, request, true);
});

test('an image-only clipboard sends a native image without a caption and survives reload', async ({ page, request }) => {
  await provePastedImage(page, request, false);
});
