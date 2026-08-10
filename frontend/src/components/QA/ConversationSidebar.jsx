import { memo, useState } from "react";
import { Button, Dropdown, Empty, Input, Modal, Tooltip, Typography } from "antd";
import {
  PlusOutlined,
  MoreOutlined,
  PushpinOutlined,
  PushpinFilled,
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
} from "@ant-design/icons";

const { Text } = Typography;

/**
 * 對話串側邊欄（V6）。
 *
 * 刻意做成受控元件（清單與當前選取都由父層持有）：切換對話要連帶換掉
 * 訊息列表與正在串流的狀態，那些狀態本來就在父層，放在這裡會變成兩份真相。
 *
 * memo 化的理由與 FollowupInput 相同 —— 串流生成時父層每收到一個 token 就
 * 重繪一次，側邊欄沒有理由跟著重畫。
 */
const ConversationSidebar = memo(function ConversationSidebar({
  conversations,
  activeId,
  loading,
  onSelect,
  onCreate,
  onRename,
  onTogglePin,
  onDelete,
}) {
  const [renaming, setRenaming] = useState(null); // { id, title }

  const confirmDelete = (item) => {
    Modal.confirm({
      title: "刪除這條對話？",
      // 明講會刪掉幾則，避免誤刪長對話 —— 標題看起來都差不多時特別容易搞錯
      content: `「${item.title}」共 ${item.message_count} 則訊息，刪除後無法復原。`,
      okText: "刪除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => onDelete(item.id),
    });
  };

  const submitRename = () => {
    const title = (renaming?.title || "").trim();
    if (title) onRename(renaming.id, title);
    setRenaming(null);
  };

  return (
    <div className="conv-sidebar">
      <Button
        type="default"
        icon={<PlusOutlined />}
        block
        onClick={onCreate}
        style={{ marginBottom: 12 }}
      >
        新對話
      </Button>

      {conversations.length === 0 && !loading && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<Text type="secondary" style={{ fontSize: 12 }}>還沒有對話</Text>}
          style={{ marginTop: 32 }}
        />
      )}

      <div className="conv-list">
        {conversations.map((item) => {
          const active = item.id === activeId;
          const isRenaming = renaming?.id === item.id;

          if (isRenaming) {
            return (
              <div key={item.id} className="conv-item">
                <Input
                  size="small"
                  autoFocus
                  value={renaming.title}
                  onChange={(e) => setRenaming({ ...renaming, title: e.target.value })}
                  onPressEnter={submitRename}
                  onBlur={submitRename}
                  // Esc 取消：改名改到一半想反悔時，不該只能靠改回原字
                  onKeyDown={(e) => { if (e.key === "Escape") setRenaming(null); }}
                />
              </div>
            );
          }

          return (
            <div
              key={item.id}
              className={`conv-item${active ? " conv-item-active" : ""}`}
              onClick={() => !active && onSelect(item.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter" && !active) onSelect(item.id); }}
            >
              <span className="conv-icon">
                {item.is_pinned ? <PushpinFilled /> : <MessageOutlined />}
              </span>
              <span className="conv-text">
                <Tooltip title={item.preview || item.title} placement="right">
                  <span className="conv-title">{item.title}</span>
                </Tooltip>
                <span className="conv-meta">{item.message_count} 則</span>
              </span>
              <Dropdown
                trigger={["click"]}
                placement="bottomRight"
                menu={{
                  items: [
                    { key: "rename", icon: <EditOutlined />, label: "重新命名" },
                    {
                      key: "pin",
                      icon: item.is_pinned ? <PushpinFilled /> : <PushpinOutlined />,
                      label: item.is_pinned ? "取消釘選" : "釘選",
                    },
                    { type: "divider" },
                    { key: "delete", icon: <DeleteOutlined />, label: "刪除", danger: true },
                  ],
                  onClick: ({ key, domEvent }) => {
                    domEvent.stopPropagation();
                    if (key === "rename") setRenaming({ id: item.id, title: item.title });
                    if (key === "pin") onTogglePin(item.id, !item.is_pinned);
                    if (key === "delete") confirmDelete(item);
                  },
                }}
              >
                <span
                  className="conv-more"
                  onClick={(e) => e.stopPropagation()}
                  role="button"
                  tabIndex={-1}
                >
                  <MoreOutlined />
                </span>
              </Dropdown>
            </div>
          );
        })}
      </div>
    </div>
  );
});

export default ConversationSidebar;
