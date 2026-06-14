import axios from 'axios';

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
});

// Attach token from sessionStorage on every request
apiClient.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401: clear token and redirect to login.
// Exception: /auth/login itself returns 401 on bad credentials — let the
// login form handle that error rather than triggering a page reload.
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const requestUrl: string = error.config?.url ?? '';
    const isLoginEndpoint = requestUrl.includes('/auth/login');

    if (error.response?.status === 401 && !isLoginEndpoint) {
      sessionStorage.removeItem('access_token');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

export default apiClient;
