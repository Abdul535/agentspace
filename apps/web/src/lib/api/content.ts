import type { components } from "./schema";

/** An image block in a user message or tool result, inline as base64 (ADR-015). */
export type ImageContent = components["schemas"]["ImageContent"];
type UserContent = components["schemas"]["UserContent"];

// A user message is a plain string (text only, and every message from before images) or a list of blocks.
type Content = string | UserContent[];

/** The text of a message's content, without its images. */
export function textOf(content: Content): string {
  if (typeof content === "string") return content;
  return content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("");
}

/** The image blocks of a message's content, in order. */
export function imagesOf(content: Content): ImageContent[] {
  if (typeof content === "string") return [];
  return content.filter((block): block is ImageContent => block.type === "image");
}

/** An image block as a `data:` URL an `<img>` can show. */
export function imageSrc(image: ImageContent): string {
  return `data:${image.mime_type};base64,${image.data}`;
}
