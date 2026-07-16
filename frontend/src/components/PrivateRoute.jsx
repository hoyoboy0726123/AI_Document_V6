import { Navigate } from 'react-router-dom';
import useAuthStore from '../stores/authStore';

const PrivateRoute = ({ children, adminOnly = false }) => {
  // Audit L：細粒度 selector —— 不訂閱整個 store，token refresh 才不會觸發全頁 re-render
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const user = useAuthStore((s) => s.user);

  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }

  if (adminOnly && user?.role !== 'admin') {
    return <Navigate to="/" />;
  }

  return children;
};

export default PrivateRoute;
