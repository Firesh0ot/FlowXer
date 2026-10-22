import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import { streamView, tileCrop, usePreview } from "../preview";

/** One picture of the previews: an ME's Program from its own stream when the map has one, else a
 * tile (a source, an ME's Preview or Program) cropped from the page's mosaic stream; `children`
 * are drawn over it (the box editor); `onMenu` is its context menu (GRAB). */
export function Monitor({
  tileId,
  label,
  tally,
  placeholder,
  children,
  onMenu,
}: {
  tileId: string;
  label: string;
  tally?: "pgm" | "pvw" | "off";
  /** Shown instead of a picture (e.g. Black); else the reason the mosaic has none. */
  placeholder?: string;
  children?: ReactNode;
  onMenu?: (event: MouseEvent) => void;
}) {
  const preview = usePreview();
  const videoRef = useRef<HTMLVideoElement>(null);
  const own = placeholder ? null : streamView(preview.map, tileId);
  const crop = placeholder ? null : own ?? tileCrop(preview.map, tileId);
  const stream = own ? preview.streamOf(tileId) : preview.stream;

  useEffect(() => {
    const video = videoRef.current;
    if (video && video.srcObject !== stream) video.srcObject = stream;
  });

  return (
    <div className={`monitor tally-${tally ?? "off"}`} data-tile={tileId} onContextMenu={onMenu}>
      {crop ? (
        <div className="crop-box" style={crop.box}>
          <video ref={videoRef} autoPlay playsInline muted style={crop.video} />
        </div>
      ) : (
        <div className="monitor-empty">{placeholder ?? preview.map?.reason ?? "No picture"}</div>
      )}
      {children}
      {label ? <div className="monitor-label">{label}</div> : null}
    </div>
  );
}
