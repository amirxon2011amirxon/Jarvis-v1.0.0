import { useRef } from "react";

import { cn } from "../../lib/utils";

type Kind = "preview" | "commit";

interface SplitterProps {
  /** "vertical" drags left/right (column widths); "horizontal" drags up/down
   * (row heights such as the transcript dock and activity monitor). */
  orientation: "vertical" | "horizontal";
  ariaLabel: string;
  title: string;
  ariaValueNow: number;
  ariaValueMin: number;
  ariaValueMax: number;
  onDelta: (delta: number, kind: Kind) => void;
  onReset?: () => void;
  /** Fired while a real drag is in flight; panels typically close overlays. */
  onDragActive?: (active: boolean) => void;
}

/** A grabbable resize handle between panels. Keyboard: arrow keys move in
 * 16px steps and commit; Escape does nothing (drag not persistent). */
export function Splitter({
  orientation,
  ariaLabel,
  title,
  ariaValueNow,
  ariaValueMin,
  ariaValueMax,
  onDelta,
  onReset,
  onDragActive,
}: SplitterProps) {
  const lastCoord = useRef(0);
  const vertical = orientation === "vertical";

  const endDrag = () => {
    onDragActive?.(false);
    lastCoord.current = 0;
  };

  return (
    <button
      type="button"
      className={cn("aze-splitter", vertical ? "aze-splitter--v" : "aze-splitter--h")}
      role="separator"
      aria-orientation={orientation}
      aria-label={ariaLabel}
      aria-valuemin={ariaValueMin}
      aria-valuemax={ariaValueMax}
      aria-valuenow={Math.round(ariaValueNow)}
      title={title}
      onPointerDown={(event) => {
        lastCoord.current = vertical ? event.clientX : event.clientY;
        event.currentTarget.setPointerCapture(event.pointerId);
        onDragActive?.(true);
      }}
      onPointerMove={(event) => {
        if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
        const coord = vertical ? event.clientX : event.clientY;
        const delta = coord - lastCoord.current;
        lastCoord.current = coord;
        if (delta !== 0) onDelta(delta, "preview");
      }}
      onPointerUp={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          onDelta(0, "commit");
        }
        endDrag();
      }}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onDoubleClick={() => onReset?.()}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 48 : 16;
        const positive =
          event.key === (vertical ? "ArrowRight" : "ArrowDown")
            ? 1
            : event.key === (vertical ? "ArrowLeft" : "ArrowUp")
              ? -1
              : 0;
        if (positive === 0) {
          if (event.key === "Enter") onDelta(0, "commit");
          return;
        }
        event.preventDefault();
        onDelta(step * positive, "preview");
        onDelta(0, "commit");
      }}
    />
  );
}
