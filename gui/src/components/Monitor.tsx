import { useEffect, useRef, useState } from "react";
import { jpegUrl, connectWhep } from "../webrtc";

export function Monitor({
  streamId,
  webrtc,
  label,
  tally,
}: {
  streamId: string;
  webrtc: boolean;
  label: string;
  tally?: "pgm" | "pvw" | "off";
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [mode, setMode] = useState<"webrtc" | "jpeg">(webrtc ? "webrtc" : "jpeg");

  useEffect(() => {
    if (!webrtc || !videoRef.current) {
      setMode("jpeg");
      return;
    }
    let closed = false;
    let dispose: (() => void) | undefined;
    connectWhep(videoRef.current, streamId)
      .then((stop) => {
        if (closed) stop();
        else dispose = stop;
      })
      .catch(() => {
        if (!closed) setMode("jpeg");
      });
    return () => {
      closed = true;
      dispose?.();
    };
  }, [streamId, webrtc]);

  return (
    <div className={`monitor tally-${tally ?? "off"}`}>
      {mode === "webrtc" ? (
        <video ref={videoRef} autoPlay playsInline muted />
      ) : (
        <JpegPump streamId={streamId} />
      )}
      <div className="monitor-label">{label}</div>
    </div>
  );
}

function JpegPump({ streamId }: { streamId: string }) {
  const [src, setSrc] = useState(jpegUrl(streamId));
  useEffect(() => {
    const timer = window.setInterval(() => setSrc(jpegUrl(streamId)), 120);
    return () => window.clearInterval(timer);
  }, [streamId]);
  return <img src={src} alt={streamId} />;
}
