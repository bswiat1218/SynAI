import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "./client";

export type Resource<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  stale: boolean;
  reload: () => void;
};

export function useResource<T>(path: string | null): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [dataPath, setDataPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [error, setError] = useState<string | null>(null);
  const [errorPath, setErrorPath] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!path) {
      setLoading(false);
      setData(null);
      setDataPath(null);
      setError(null);
      setErrorPath(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    apiRequest<T>(path, { signal: controller.signal })
      .then((result) => {
        setData(result);
        setDataPath(path);
        setError(null);
        setErrorPath(null);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "Unable to load service data.");
        setErrorPath(path);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path, revision]);

  const reload = useCallback(() => setRevision((current) => current + 1), []);
  const currentData = dataPath === path ? data : null;
  const currentError = errorPath === path ? error : null;
  return {
    data: currentData,
    loading,
    error: currentError,
    stale: currentData !== null && currentError !== null,
    reload,
  };
}
