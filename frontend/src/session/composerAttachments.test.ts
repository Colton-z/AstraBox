// What the composer is allowed to send, and what it refuses to invent.
//
// The engine reads an image as a base64 content block. Every step between a
// clipboard and that block can lose the picture or produce one the server
// will reject, and both failures look the same to a user: they pasted
// something and the agent never saw it.
import { describe, expect, it } from 'vitest';
import type { FileUIPart } from 'ai';

import {
  readPastedImageFiles,
  turnInputContent,
  turnInputImages,
} from './composerAttachments';

const PNG_DATA = 'iVBORw0KGgo=';

function filePart(overrides: Partial<FileUIPart> = {}): FileUIPart {
  return {
    type: 'file',
    filename: 'slide.png',
    mediaType: 'image/png',
    url: `data:image/png;base64,${PNG_DATA}`,
    ...overrides,
  } as FileUIPart;
}

function clipboard(entries: Array<{ kind: string; type: string; file?: File }>): DataTransfer {
  return {
    items: entries.map((entry) => ({
      kind: entry.kind,
      type: entry.type,
      getAsFile: () => entry.file ?? null,
    })),
  } as unknown as DataTransfer;
}

describe('readPastedImageFiles', () => {
  it('takes the images a slide paste carries', () => {
    const png = new File([new Uint8Array([1])], 'slide.png', { type: 'image/png' });
    const files = readPastedImageFiles(clipboard([
      { kind: 'string', type: 'text/plain' },
      { kind: 'string', type: 'text/html' },
      { kind: 'file', type: 'image/png', file: png },
    ]));
    expect(files).toEqual([png]);
  });

  it('leaves a file the engine cannot read alone', () => {
    // Adding it would show an attachment for something the send would then
    // refuse — worse than not offering to carry it at all.
    const pdf = new File([new Uint8Array([1])], 'deck.pdf', { type: 'application/pdf' });
    expect(readPastedImageFiles(clipboard([
      { kind: 'file', type: 'application/pdf', file: pdf },
    ]))).toEqual([]);
  });

  it('reads nothing off a clipboard that is not there', () => {
    expect(readPastedImageFiles(null)).toEqual([]);
  });
});

describe('turnInputImages', () => {
  it('reads the base64 out of the data URL the composer resolved', () => {
    expect(turnInputImages([filePart()])).toEqual([{
      type: 'image',
      source: { type: 'base64', media_type: 'image/png', data: PNG_DATA },
    }]);
  });

  it('drops an attachment whose URL never became base64', () => {
    // A blob URL means the conversion did not happen. Sending the URL would
    // hand the engine a string that resolves to nothing in the box.
    expect(turnInputImages([filePart({ url: 'blob:http://localhost/abc' })])).toEqual([]);
  });

  it('drops an unsupported media type rather than letting the server refuse it', () => {
    expect(turnInputImages([filePart({
      mediaType: 'image/bmp',
      url: 'data:image/bmp;base64,Qk0=',
    })])).toEqual([]);
  });

  it('drops a data URL whose declared type is not the one it carries', () => {
    expect(turnInputImages([filePart({
      url: `data:image/jpeg;base64,${PNG_DATA}`,
    })])).toEqual([]);
  });
});

describe('turnInputContent', () => {
  it('stays a plain string when the message is only words', () => {
    expect(turnInputContent('hello', [])).toBe('hello');
  });

  it('puts the text before the images it was pasted with', () => {
    const [image] = turnInputImages([filePart()]);
    expect(turnInputContent('look at this', [image])).toEqual([
      { type: 'text', text: 'look at this' },
      image,
    ]);
  });

  it('sends an image with no caption as blocks without an empty text block', () => {
    const [image] = turnInputImages([filePart()]);
    expect(turnInputContent('', [image])).toEqual([image]);
  });
});
