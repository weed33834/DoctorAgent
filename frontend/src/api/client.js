// Centralised API client for the console.
// Token is kept in sessionStorage (not localStorage) so it is not persisted
// across browser sessions; theme is the only thing stored long-term.

const TOKEN_KEY = "doctoragent_token";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t) {
  if (t) sessionStorage.setItem(TOKEN_KEY, t);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body) {
  const headers = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(`网络错误: ${e.message}`, 0);
  }

  if (res.status === 401 || res.status === 403) {
    throw new ApiError(
      res.status === 401
        ? "未授权或令牌无效"
        : "权限不足（敏感操作需要配置 DOCTORAGENT_API_TOKEN）",
      res.status
    );
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = String(j.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b ?? {}),
  put: (p, b) => request("PUT", p, b ?? {}),
  delete: (p) => request("DELETE", p),
};
