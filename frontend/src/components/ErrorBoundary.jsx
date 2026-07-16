import React from 'react';
import { Result, Button } from 'antd';

/**
 * Audit H13：全站錯誤邊界。
 * React 18 遇到未捕捉的 render error 會 unmount 整棵樹 → 使用者只看到白屏、
 * 連側邊欄都消失（例如某頁資料異常、KG confidence 為 null 等）。
 * 用一層 ErrorBoundary 包住路由，任一頁 render 出錯時顯示可回復的錯誤畫面，
 * 而不是讓整個 app 白屏。
 */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    // 保留到 console 以便除錯（不外送）
    console.error('[ErrorBoundary] render error:', error, info);
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  handleReload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
          <Result
            status="error"
            title="頁面發生錯誤"
            subTitle={this.state.error?.message || '發生未預期的錯誤，您可以重試或重新載入頁面。'}
            extra={[
              <Button type="primary" key="reload" onClick={this.handleReload}>
                重新載入
              </Button>,
              <Button key="retry" onClick={this.handleReset}>
                重試
              </Button>,
            ]}
          />
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
