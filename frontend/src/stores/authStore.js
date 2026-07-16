import { create } from 'zustand';
import { persist } from 'zustand/middleware';

const useAuthStore = create(
  persist(
    (set) => ({
      token: null,
      refreshToken: null,
      tokenExpiresAt: null, // Timestamp when access token expires
      user: null,
      isAuthenticated: false,

      /**
       * Login with access token and refresh token
       * @param {string} token - Access token
       * @param {string} refreshToken - Refresh token (long-lived)
       * @param {number} expiresIn - Token expiration in seconds
       * @param {object} user - User object
       */
      login: (token, refreshToken, expiresIn, user) => {
        const tokenExpiresAt = Date.now() + expiresIn * 1000;
        set({
          token,
          refreshToken,
          tokenExpiresAt,
          user,
          isAuthenticated: true
        });
      },

      /**
       * Update tokens after refresh
       * @param {string} token - New access token
       * @param {string} refreshToken - New refresh token
       * @param {number} expiresIn - Token expiration in seconds
       */
      updateTokens: (token, refreshToken, expiresIn) => {
        const tokenExpiresAt = Date.now() + expiresIn * 1000;
        set({ token, refreshToken, tokenExpiresAt });
      },

      /**
       * Logout and clear all authentication data
       */
      logout: () => {
        // Audit medium：登出時一併清掉可能殘留的背景任務等本機狀態，
        // 避免共用電腦換帳號後看到前一位使用者的任務/設定殘影。
        try {
          window.localStorage.removeItem('activeTasks');
        } catch { /* ignore */ }
        set({
          token: null,
          refreshToken: null,
          tokenExpiresAt: null,
          user: null,
          isAuthenticated: false,
        });
      },

      /**
       * Check if access token is expired or will expire soon
       * @param {number} bufferSeconds - Time buffer before expiration (default: 60 seconds)
       * @returns {boolean}
       */
      isTokenExpiringSoon: (bufferSeconds = 60) => {
        const state = useAuthStore.getState();
        if (!state.tokenExpiresAt) return true;
        const timeUntilExpiry = state.tokenExpiresAt - Date.now();
        return timeUntilExpiry < bufferSeconds * 1000;
      },
    }),
    {
      name: 'auth-storage', // localStorage key
    }
  )
);

export default useAuthStore;
