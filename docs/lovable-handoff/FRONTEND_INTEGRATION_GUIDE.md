# CollectFlow — Frontend Integration Guide

This guide describes how an independent frontend built on Lovable, Vite, Next.js, or React connects to the existing CollectFlow FastAPI backend.

---

## 1. Network & API Base Configuration

- **API Base Path**: `/api` (or fully qualified origin, e.g. `https://3-6-152-136.sslip.io/api`).
- **Standard Headers**:
  ```http
  Content-Type: application/json
  Authorization: Bearer <token>
  X-Request-ID: <optional-client-uuid>
  ```
- **CORS Configuration**:
  If the replacement frontend is hosted on a different origin (e.g. `https://my-collectflow.lovable.app`), ensure the backend CORS middleware allows that origin, or configure a reverse proxy (e.g. Nginx, Cloudflare, or Vite proxy) to route `/api/*` requests to the FastAPI backend.

---

## 2. Authentication & Session Handling

### 2.1 Login Flow
1. Send credentials to `POST /api/auth/login`:
   ```json
   { "username": "admin", "password": "yourpassword" }
   ```
2. On `HTTP 200`, store the returned bearer token (`data.token`) in secure storage (`localStorage` or `sessionStorage`).
3. Store user profile details (`data.user`) in global state (Zustand, Redux, or React Context).

### 2.2 Authorized Requests
Attach the token to every outgoing HTTP request:
```typescript
const apiClient = axios.create({ baseURL: '/api' });
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('cf_auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});
```

### 2.3 Session Expiration & Auto-Logout
When the backend responds with `HTTP 401 Unauthorized`, intercept the response, clear the local token, and redirect the user to `/login`:
```typescript
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('cf_auth_token');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);
```

---

## 3. Standard Data & Type Definitions (TypeScript)

### 3.1 Currency & Formatting
- All backend calculations are performed in base INR.
- Format currency values using `Intl.NumberFormat`:
  ```typescript
  export const formatINR = (val: number): string =>
    new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 2,
    }).format(val || 0);
  ```

### 3.2 Date Formatting
- Backend stores and expects ISO-8601 strings (`YYYY-MM-DD`).
- Display dates in readable Indian enterprise format (`DD MMM YYYY`, e.g. `15 Apr 2026`).

---

## 4. Secure File Download Handling

Direct browser links cannot attach `Authorization: Bearer <token>` headers. CollectFlow uses a two-step ticket exchange:

1. Request single-use ticket:
   ```typescript
   const { data } = await apiClient.post('/export/token');
   const ticket = data.ticket;
   ```
2. Trigger browser download:
   ```typescript
   window.location.href = `/api/export/excel?fy=${encodeURIComponent(selectedFY)}&ticket=${encodeURIComponent(ticket)}`;
   ```
   The backend validates the one-time ticket, marks it consumed, and streams the binary spreadsheet.

---

## 5. Recommended Lovable Tech Stack

- **Framework**: Vite + React 18 + TypeScript.
- **Routing**: React Router v6 or TanStack Router.
- **Data Fetching & Cache**: TanStack Query (React Query) v5.
- **UI Components**: Tailwind CSS + `shadcn/ui` (Radix UI).
- **Forms & Validation**: `react-hook-form` + `zod`.
- **Notifications**: `sonner` for toast notifications.
- **Icons**: `lucide-react`.
- **Charts**: Recharts or Chart.js.
