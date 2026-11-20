"use client";

import { ArrowUp, ImagePlus, Square, X } from "lucide-react";
import { useRef, useState } from "react";

import { imageOutline } from "@/components/chat/image-thumbnails";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { QueuedMessage, QueueKind } from "@/lib/api/use-agent-run";
import { cn } from "@/lib/utils";

// The server's limits (ADR-015), checked here too so the user hears about them before sending.
const MAX_IMAGES = 5;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

// A file as a `data:` URL, the form the API takes and an <img> can show.
function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * Prompt input. Idle: Enter sends, Shift+Enter adds a line. While a run is going,
 * Enter steers it (read after the current turn) and Alt+Enter queues a follow-up
 * (read once it would stop); the button stops the run. Images can be pasted, dropped or
 * attached when the model can see them (ADR-015); they go with the next prompt, not with steering.
 */
export function Composer({
  value,
  onChange,
  running,
  canAttach,
  queued,
  onSend,
  onQueue,
  onStop,
}: {
  value: string;
  onChange: (value: string) => void;
  running: boolean;
  /** False when the catalog says the session's model can't see images. */
  canAttach: boolean;
  queued: QueuedMessage[];
  onSend: (content: string, images: string[]) => void;
  onQueue: (kind: QueueKind, content: string) => void;
  onStop: () => void;
}) {
  const [images, setImages] = useState<string[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const attachable = canAttach && !running;
  const canSend = !running && (value.trim().length > 0 || images.length > 0);

  // Adds the image files among `files`, within the server's count and size limits.
  async function addFiles(files: File[]) {
    const picked = files.filter((file) => file.type.startsWith("image/"));
    if (picked.length === 0) return;
    const fitting = picked.filter((file) => file.size <= MAX_IMAGE_BYTES).slice(0, MAX_IMAGES - images.length);
    const urls = await Promise.all(fitting.map(readAsDataUrl));
    setImages((current) => [...current, ...urls].slice(0, MAX_IMAGES));
    setAttachError(
      fitting.length < picked.length ? `Up to ${MAX_IMAGES} images, ${MAX_IMAGE_BYTES / 1024 / 1024} MB each.` : null,
    );
  }

  // Sends the text and images, or queues the text alone while a run is going.
  function submit(kind: QueueKind) {
    const content = value.trim();
    if (running) {
      if (!content) return;
      onQueue(kind, content);
    } else {
      if (!content && images.length === 0) return;
      onSend(content, images);
      setImages([]);
      setAttachError(null);
    }
    onChange("");
  }

  return (
    <div className="flex flex-col gap-2">
      {queued.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Queued messages">
          {queued.map((q, i) => (
            <li key={i} className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
              <Badge variant="outline">{q.kind === "steer" ? "After this turn" : "When done"}</Badge>
              <span className="truncate">{q.content}</span>
            </li>
          ))}
        </ul>
      )}
      {images.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label="Attached images">
          {images.map((src, index) => (
            <li key={index} className="relative">
              {/* eslint-disable-next-line @next/next/no-img-element -- a local data: URL; next/image can't optimize it. */}
              <img src={src} alt={`Attachment ${index + 1}`} className={cn("size-16 rounded-lg object-cover", imageOutline)} />
              <Button
                type="button"
                size="icon-xs"
                variant="secondary"
                className="absolute -top-1.5 -right-1.5 rounded-full shadow-sm after:absolute after:-inset-2"
                aria-label={`Remove attachment ${index + 1}`}
                onClick={() => setImages((current) => current.filter((_, i) => i !== index))}
              >
                <X />
              </Button>
            </li>
          ))}
        </ul>
      )}
      {attachError && <p className="text-sm text-destructive">{attachError}</p>}
      <form
        className="relative"
        onSubmit={(event) => {
          event.preventDefault();
          submit("steer");
        }}
        onDragOver={(event) => {
          if (attachable) event.preventDefault();
        }}
        onDrop={(event) => {
          if (!attachable) return;
          event.preventDefault();
          void addFiles([...event.dataTransfer.files]);
        }}
      >
        <input
          ref={fileInput}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          hidden
          onChange={(event) => {
            void addFiles([...(event.target.files ?? [])]);
            event.target.value = ""; // so picking the same file again still fires
          }}
        />
        <Textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onPaste={(event) => {
            const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
            // Only an image paste is taken over; pasting text works as usual.
            if (!attachable || files.length === 0) return;
            event.preventDefault();
            void addFiles(files);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit(event.altKey ? "follow_up" : "steer");
            }
          }}
          placeholder={
            running
              ? "Steer the agent: Enter adds it after this turn, Alt+Enter when it's done…"
              : "Ask the agent to read or write files…"
          }
          aria-label="Message"
          rows={3}
          className="max-h-60 resize-none pr-24"
          autoFocus
        />
        {/* A span keeps the tooltip working: a disabled button gets no hover events. */}
        <span
          className="absolute right-12 bottom-2"
          title={canAttach ? "Attach images" : "This model can't see images"}
        >
          <Button
            type="button"
            size="icon"
            variant="ghost"
            disabled={!attachable}
            onClick={() => fileInput.current?.click()}
            aria-label="Attach images"
          >
            <ImagePlus />
          </Button>
        </span>
        {running ? (
          <Button
            type="button"
            size="icon"
            variant="secondary"
            className="absolute right-2 bottom-2"
            onClick={onStop}
            aria-label="Stop"
          >
            <Square className="fill-current" />
          </Button>
        ) : (
          <Button type="submit" size="icon" className="absolute right-2 bottom-2" disabled={!canSend} aria-label="Send">
            <ArrowUp />
          </Button>
        )}
      </form>
    </div>
  );
}
