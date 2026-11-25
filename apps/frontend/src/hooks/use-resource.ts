import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/api/client";

export type Resource<T> =
  | { state: "loading"; data: T | null; error: null }
  | { state: "ready"; data: T; error: null }
  | { state: "error"; data: T | null; error: ApiError };

/** Tiny fetch-on-mount hook with abort, optional polling and manual reload. */
export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  opts: { pollMs?: number } = {},
): Resource<T> & { reload: () => void } {
  const [res, setRes] = useState<Resource<T>>({ state: "loading", data: null, error: null });
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  useEffect(() => {
    const ctrl = new AbortController();
    loadRef
      .current(ctrl.signal)
      .then((data) => setRes({ state: "ready", data, error: null }))
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        const error =
          err instanceof ApiError
            ? err
            : new ApiError({ type: "about:blank", title: "Unexpected error", status: 0, detail: String(err) });
        setRes((prev) => ({ state: "error", data: prev.data, error }));
      });
    let timer: number | undefined;
    if (opts.pollMs) timer = window.setTimeout(() => setTick((t) => t + 1), opts.pollMs);
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, opts.pollMs]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...res, reload };
}
