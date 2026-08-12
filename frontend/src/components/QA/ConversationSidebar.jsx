import { memo, useState } from "react";
import { Button, Checkbox, Dropdown, Empty, Input, Modal, Tooltip, Typography } from "antd";
import {
  PlusOutlined,
  MoreOutlined,
  PushpinOutlined,
  PushpinFilled,
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
  CheckSquareOutlined,
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
  onBatchDelete,
}) {
  const [renaming, setRenaming] = useState(null); // { id, title }
  // 多選刪除模式。選取集合放本地就好 —— 它是純 UI 暫態，退出模式即消失，
  // 提到父層只會讓串流時的重繪雪上加霜（本元件 memo 化的初衷）。
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState(() => new Set());

  const exitSelectMode = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  const toggleOne = (id) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  const allSelected = conversations.length > 0 && selected.size === conversations.length;

  const confirmBatchDelete = () => {
    const ids = [...selected];
    const totalMsgs = conversations
      .filter((c) => selected.has(c.id))
      .reduce((sum, c) => sum + (c.message_count || 0), 0);
    Modal.confirm({
      title: `刪除 ${ids.length} 條對話？`,
      content: `共 ${totalMsgs} 則訊息，刪除後無法復原。`,
      okText: "刪除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        await onBatchDelete(ids);   // 失敗會 throw：Modal 停留、選取保留，可重試
        exitSelectMode();
      },
    });
  };

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
      {!selectMode ? (
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <Button
            type="default"
            icon={<PlusOutlined />}
            style={{ flex: 1 }}
            onClick={onCreate}
          >
            新對話
          </Button>
          <Tooltip title="選取多條對話一次刪除">
            <Button
              icon={<CheckSquareOutlined />}
              onClick={() => setSelectMode(true)}
              disabled={conversations.length === 0}
            />
          </Tooltip>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <Checkbox
            checked={allSelected}
            indeterminate={selected.size > 0 && !allSelected}
            onChange={() =>
              setSelected(allSelected ? new Set() : new Set(conversations.map((c) => c.id)))
            }
          >
            全選
          </Checkbox>
          <span style={{ flex: 1, fontSize: 12, color: "#8c8c8c" }}>已選 {selected.size}</span>
          <Button size="small" danger disabled={selected.size === 0} onClick={confirmBatchDelete}>
            刪除
          </Button>
          <Button size="small" onClick={exitSelectMode}>取消</Button>
        </div>
      )}

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
              onClick={() => (selectMode ? toggleOne(item.id) : (!active && onSelect(item.id)))}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                if (selectMode) toggleOne(item.id);
                else if (!active) onSelect(item.id);
              }}
            >
              <span className="conv-icon">
                {selectMode ? (
                  // 純指示器：點擊由整列的 onClick 處理，checkbox 自己不攔事件，
                  // 否則一次點擊會被列與框各切換一次、等於沒選到
                  <Checkbox checked={selected.has(item.id)} style={{ pointerEvents: "none" }} />
                ) : (
                  item.is_pinned ? <PushpinFilled /> : <MessageOutlined />
                )}
              </span>
              <span className="conv-text">
                <Tooltip title={item.preview || item.title} placement="right">
                  <span className="conv-title">{item.title}</span>
                </Tooltip>
                <span className="conv-meta">{item.message_count} 則</span>
              </span>
              {!selectMode && <Dropdown
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
              </Dropdown>}
            </div>
          );
        })}
      </div>
    </div>
  );
});

export default ConversationSidebar;
