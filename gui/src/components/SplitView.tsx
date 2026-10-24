import { Children, CSSProperties, ReactNode, useRef, useState } from "react";

/** Keyboard and pointer operated splits; proportions survive window resizing. */
export default function SplitView({
  children,
  direction = "horizontal",
  storageKey,
  initial,
  compactIndex,
}: {
  children: ReactNode;
  direction?: "horizontal" | "vertical";
  storageKey: string;
  initial: number[];
  compactIndex?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [sizes, setSizes] = useState<number[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) ?? "null");
      if (
        Array.isArray(saved) &&
        saved.length === initial.length &&
        saved.every((n) => Number.isFinite(n) && n > 0)
      )
        return saved;
    } catch {
      /* Use defaults if storage is unavailable. */
    }
    return initial;
  });
  const panes = Children.toArray(children);
  function resize(index: number, delta: number, start = sizes) {
    const total = start[index] + start[index + 1];
    const box = container.current?.getBoundingClientRect();
    const length =
      (direction === "horizontal" ? box?.width : box?.height) ?? 1000;
    const minimum = Math.min(
      ((direction === "horizontal" ? 240 : 200) / length) * 100,
      total / 2.2,
    );
    const nextMinimum = Math.min(
      ((direction === "horizontal" ? 240 : 180) / length) * 100,
      total / 2.2,
    );
    const next = [...start];
    next[index] = Math.max(
      minimum,
      Math.min(total - nextMinimum, start[index] + delta),
    );
    next[index + 1] = total - next[index];
    setSizes(next);
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* Optional persistence. */
    }
  }
  return (
    <div
      ref={container}
      className={`split-view ${direction} ${compactIndex !== undefined ? "compact" : ""}`}
    >
      {panes.map((pane, index) => (
        <div
          key={index}
          className="split-fragment"
          style={{ display: "contents" }}
        >
          <div
            className="split-pane"
            style={
              {
                flex: `${sizes[index]} 1 0`,
                display:
                  compactIndex !== undefined && compactIndex !== index
                    ? "none"
                    : undefined,
              } as CSSProperties
            }
          >
            {pane}
          </div>
          {index < panes.length - 1 && compactIndex === undefined && (
            <div
              className="split-handle"
              role="separator"
              tabIndex={0}
              aria-label={`Resize ${direction === "horizontal" ? "columns" : "rows"} ${index + 1} and ${index + 2}`}
              aria-orientation={
                direction === "horizontal" ? "vertical" : "horizontal"
              }
              aria-valuenow={Math.round(sizes[index])}
              aria-valuemin={0}
              aria-valuemax={100}
              onDoubleClick={() => {
                setSizes(initial);
                localStorage.removeItem(storageKey);
              }}
              onKeyDown={(event) => {
                const negative =
                  direction === "horizontal" ? "ArrowLeft" : "ArrowUp";
                const positive =
                  direction === "horizontal" ? "ArrowRight" : "ArrowDown";
                if ([negative, positive].includes(event.key)) {
                  event.preventDefault();
                  resize(index, event.key === negative ? -2 : 2);
                }
              }}
              onPointerDown={(event) => {
                event.preventDefault();
                const handle = event.currentTarget;
                handle.setPointerCapture(event.pointerId);
                const start = [...sizes];
                const origin =
                  direction === "horizontal" ? event.clientX : event.clientY;
                const box = container.current!.getBoundingClientRect();
                const length =
                  direction === "horizontal" ? box.width : box.height;
                const move = (e: PointerEvent) =>
                  resize(
                    index,
                    (((direction === "horizontal" ? e.clientX : e.clientY) -
                      origin) /
                      length) *
                      100,
                    start,
                  );
                const stop = () => {
                  handle.removeEventListener("pointermove", move);
                  handle.removeEventListener("lostpointercapture", stop);
                  handle.classList.remove("dragging");
                };
                handle.classList.add("dragging");
                handle.addEventListener("pointermove", move);
                handle.addEventListener("lostpointercapture", stop);
              }}
            >
              <span />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
