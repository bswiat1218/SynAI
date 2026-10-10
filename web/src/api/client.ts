import type { ErrorResponse } from "./contracts";

export const SESSION_EXPIRED_EVENT = "synai:session-expired";

export function notifySessionExpired() {
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  csrfToken?: string,
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "include",
  });
  if (response.status === 401 && path !== "/api/v1/auth/login") {
    notifySessionExpired();
  }
  if (!response.ok) {
    let error: ErrorResponse | undefined;
    try {
      error = (await response.json()) as ErrorResponse;
    } catch {
      throw new ApiError("invalid_response", "The service returned an invalid response.", response.status);
    }
    throw new ApiError(
      error.error.code,
      error.error.message,
      response.status,
    );
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
