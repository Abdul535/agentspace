"use client";

import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

// A 1px outline that is pure black or white at low opacity, so it never tints the image edge.
export const imageOutline = "outline outline-1 -outline-offset-1 outline-black/10 dark:outline-white/10";

/** Images in a message as small thumbnails; each opens full size in a dialog. */
export function ImageThumbnails({ srcs, className }: { srcs: string[]; className?: string }) {
  if (srcs.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {srcs.map((src, index) => (
        <Dialog key={index}>
          <DialogTrigger className="rounded-lg focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            {/* eslint-disable-next-line @next/next/no-img-element -- inline data: URLs; next/image can't optimize them. */}
            <img
              src={src}
              alt={`Attached image ${index + 1}`}
              className={cn("size-24 rounded-lg object-cover", imageOutline)}
            />
          </DialogTrigger>
          <DialogContent className="w-auto p-2 sm:max-w-[min(90vw,64rem)]">
            <DialogTitle className="sr-only">Image {index + 1}</DialogTitle>
            {/* eslint-disable-next-line @next/next/no-img-element -- see above. */}
            <img
              src={src}
              alt={`Attached image ${index + 1}, full size`}
              className={cn("max-h-[85vh] w-auto rounded-sm object-contain", imageOutline)}
            />
          </DialogContent>
        </Dialog>
      ))}
    </div>
  );
}
