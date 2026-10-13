import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ConsoleState } from "../api";

function post(message: Record<string, unknown>) {
  if (window.parent !== window) window.parent.postMessage(message, "*");
}

/** A widget's state: the console snapshot (every 500 ms), commands that refresh it, and the
 * widget-ready / widget-size messages to the page that frames it (sized by `root`). */
export function useWidget() {
  const [snapshot, setSnapshot] = useState<ConsoleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const ready = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await api.console());
    } catch (err) {
      setError(err instanceof Error ? err.message : "mixer unreachable");
    }
  }, []);

  const command = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "command failed");
    }
  };

  const postSize = () => {
    const rect = root.current?.getBoundingClientRect();
    if (rect) post({ type: "widget-size", w: Math.round(rect.width), h: Math.round(rect.height) });
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // Ready once the first state is shown; the size whenever it changes.
  useEffect(() => {
    if (!snapshot || ready.current) return;
    ready.current = true;
    post({ type: "widget-ready" });
    postSize();
  }, [snapshot]);
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (ready.current) postSize();
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return { snapshot, error, setError, command, root };
}
