// Images the composer can hand to the engine, and how they get there.
//
// The engine reads pictures as its own content blocks, so an image pasted
// into the composer travels as one. Everything here is about the two ends of
// that trip: what the clipboard offers, and what the send payload needs.
import type { FileUIPart } from 'ai';
import { COMPOSER_IMAGE_MEDIA_TYPES } from '../types';
import type { ComposerImageMediaType, TurnInputImage } from '../types';

export { COMPOSER_IMAGE_MEDIA_TYPES };
export type { TurnInputImage };

function isSupported(mediaType: string): mediaType is ComposerImageMediaType {
  return (COMPOSER_IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType);
}

/** The images on a clipboard, ignoring anything the engine cannot read. */
export function readPastedImageFiles(clipboard: DataTransfer | null): File[] {
  if (!clipboard) return [];
  const images: File[] = [];
  for (const item of clipboard.items) {
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (file && isSupported(file.type)) images.push(file);
  }
  return images;
}

/**
 * The image blocks for one submit, read out of the attachments the composer
 * is holding.
 *
 * `PromptInput` resolves each attachment to a data URL before it calls
 * submit, so the base64 is already here — no second read of the file, and
 * nothing to await. An attachment that is not a readable image is dropped
 * rather than sent as something the engine would reject; nothing else in
 * this composer can put one there.
 */
export function turnInputImages(files: readonly FileUIPart[]): TurnInputImage[] {
  const images: TurnInputImage[] = [];
  for (const file of files) {
    const mediaType = String(file.mediaType || '');
    if (!isSupported(mediaType)) continue;
    const url = String(file.url || '');
    const prefix = `data:${mediaType};base64,`;
    if (!url.startsWith(prefix)) continue;
    const data = url.slice(prefix.length);
    if (!data) continue;
    images.push({
      type: 'image',
      source: { type: 'base64', media_type: mediaType, data },
    });
  }
  return images;
}

/** The `content` one turn input is sent as: a string, or blocks. */
export function turnInputContent(
  text: string,
  images: readonly TurnInputImage[],
): string | Array<{ type: 'text'; text: string } | TurnInputImage> {
  if (images.length === 0) return text;
  const blocks: Array<{ type: 'text'; text: string } | TurnInputImage> = [];
  if (text) blocks.push({ type: 'text', text });
  blocks.push(...images);
  return blocks;
}
