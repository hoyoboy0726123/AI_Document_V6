import axios from 'axios';
import useAuthStore from '../stores/authStore';
import { message } from 'antd';

const apiClient = axios.create({
  baseURL: '/api/v1/',
});

// Flag to prevent multiple simultaneous refresh requests
let isRefreshing = false;
let failedQueue = [];

const processQueue = (error, token = null) => {
  failedQueue.forEach(prom => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve(token);
    }
  });

  failedQueue = [];
};

/**
 * Refresh access token using refresh token
 * @returns {Promise<string>} New access token
 */
const refreshAccessToken = async () => {
  const { refreshToken, updateTokens, logout } = useAuthStore.getState();

  if (!refreshToken) {
    throw new Error('No refresh token available');
  }

  try {
    const response = await axios.post('/api/v1/auth/refresh', {
      refresh_token: refreshToken
    });

    const { access_token, refresh_token, expires_in } = response.data;

    // Update tokens in store
    updateTokens(access_token, refresh_token, expires_in);

    return access_token;
  } catch (error) {
    // Refresh failed - logout user
    logout();
    message.error('登入已過期，請重新登入');
    window.location.href = '/login';
    throw error;
  }
};

// Request interceptor - Add auth token and handle refresh
apiClient.interceptors.request.use(
  async (config) => {
    // Skip token check for auth endpoints
    if (config.url?.includes('/auth/login') || config.url?.includes('/auth/register')) {
      return config;
    }

    // Audit H10：呼叫端若已自帶 Authorization（例如登入成功後用「新 token」呼叫 /auth/me），
    // 攔截器絕不覆蓋，否則會用 store/localStorage 的「舊 token」蓋掉 → 拿到前一位使用者的身分。
    if (config.headers?.Authorization) {
      return config;
    }

    let { token, refreshToken, isTokenExpiringSoon } = useAuthStore.getState();

    // Try to get token from localStorage if not in memory
    if (!token) {
      try {
        const persisted = JSON.parse(window.localStorage.getItem('auth-storage') || '{}');
        token = persisted?.state?.token;
        refreshToken = persisted?.state?.refreshToken;
      } catch {
        token = null;
        refreshToken = null;
      }
    }

    // If token is expiring soon and we have a refresh token, refresh it
    if (token && refreshToken && isTokenExpiringSoon && isTokenExpiringSoon(60)) {
      if (!isRefreshing) {
        isRefreshing = true;

        try {
          const newToken = await refreshAccessToken();
          isRefreshing = false;
          processQueue(null, newToken);
          config.headers.Authorization = `Bearer ${newToken}`;
        } catch (error) {
          isRefreshing = false;
          processQueue(error, null);
          return Promise.reject(error);
        }
      } else {
        // Another request is already refreshing - queue this request
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        }).then(token => {
          config.headers.Authorization = `Bearer ${token}`;
          return config;
        }).catch(err => {
          return Promise.reject(err);
        });
      }
    } else if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }

    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor - Handle 401 errors
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;
    const status = error.response?.status;

    // Handle 401 Unauthorized
    if (status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      const { refreshToken } = useAuthStore.getState();

      // Try to refresh token if available
      if (refreshToken && !isRefreshing) {
        isRefreshing = true;

        try {
          const newToken = await refreshAccessToken();
          isRefreshing = false;
          processQueue(null, newToken);

          // Retry original request with new token
          originalRequest.headers.Authorization = `Bearer ${newToken}`;
          return apiClient(originalRequest);
        } catch (refreshError) {
          isRefreshing = false;
          processQueue(refreshError, null);
          return Promise.reject(refreshError);
        }
      } else if (isRefreshing) {
        // Queue request while refreshing
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        }).then(token => {
          originalRequest.headers.Authorization = `Bearer ${token}`;
          return apiClient(originalRequest);
        }).catch(err => {
          return Promise.reject(err);
        });
      }

      // No refresh token - force logout
      const { logout } = useAuthStore.getState();
      logout();
      message.error('登入資訊已失效，請重新登入');
      window.location.href = '/login';
    }

    return Promise.reject(error);
  }
);

export default apiClient;
