import { Button, Collapse, Divider, List, Space, Tag, Timeline, Typography } from "antd";
import { BulbOutlined, EyeOutlined, RobotOutlined, ToolOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const { Text } = Typography;

/**
 * 對話訊息的共用渲染零件。
 *
 * 這些原本是 QAConsolePage 內部的函式，搬到模組層級是為了讓它們「每次渲染都是同一個
 * 參考」—— 否則傳給 memo 化的 HistoryMessage 時，每次都是新函式，memo 直接失效。
 * 它們本來就不依賴任何 state，搬出來沒有行為變化。
 */

export const markdownComponents = {
  table: ({ node, ...props }) => (
    <div style={{ overflowX: "auto", marginBottom: 8 }}>
      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 14 }} {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead style={{ background: "#fafafa" }} {...props} />,
  th: ({ node, ...props }) => (
    <th style={{ border: "1px solid #d9d9d9", padding: "6px 12px", fontWeight: 600, textAlign: "left", whiteSpace: "nowrap" }} {...props} />
  ),
  td: ({ node, ...props }) => (
    <td style={{ border: "1px solid #d9d9d9", padding: "6px 12px" }} {...props} />
  ),
  tr: ({ node, ...props }) => <tr style={{ borderBottom: "1px solid #f0f0f0" }} {...props} />,
};

/** 去掉 VL 模型輸出時多包的 ```markdown 圍籬，否則整段會被當成程式碼區塊。 */
export const stripFenceWrapper = (text) => {
  if (!text || !text.includes("```")) return text;
  const src = text.trim();
  const whole = src.match(/^```[ \t]*(?:markdown|md)?[ \t]*\n([\s\S]*?)\n?```$/i);
  if (whole && !whole[1].includes("```")) return whole[1].trim();

  const isWrapper = (line) => /^```[ \t]*(?:markdown|md)[ \t]*$/i.test(line.trim());
  if (!src.split("\n").some(isWrapper)) return src;

  const kept = [];
  let inWrapper = false;
  for (const line of src.split("\n")) {
    const bare = line.trim();
    if (isWrapper(bare)) { inWrapper = true; continue; }
    if (inWrapper && bare === "```") { inWrapper = false; continue; }
    kept.push(line);
  }
  return kept.join("\n").trim();
};

export const renderThinking = (thinking, isLive, thinkingDone) => {
  if (!thinking) return null;
  const labelText = isLive && !thinkingDone ? "思考中..." : "思考過程";
  // 只在串流思考時強制展開；結束後交還使用者控制
  const forceProps = isLive && !thinkingDone ? { activeKey: ["t"] } : {};
  return (
    <Collapse
      size="small"
      style={{ marginBottom: 12, background: "#fffbe6", border: "1px solid #ffe58f" }}
      {...forceProps}
      items={[{
        key: "t",
        label: (
          <Text type="secondary" style={{ fontSize: 12 }}>
            <BulbOutlined style={{ marginRight: 4 }} />{labelText}
          </Text>
        ),
        children: (
          <Text style={{ fontSize: 12, whiteSpace: "pre-wrap", color: "#888" }}>{thinking}</Text>
        ),
      }]}
    />
  );
};

export const renderAgentSteps = (steps, isLive) => {
  if (!steps || steps.length === 0) {
    if (isLive) {
      return (
        <div style={{ padding: "8px 0 12px" }}>
          <Text type="secondary" style={{ fontSize: 12, fontStyle: "italic" }}>
            <RobotOutlined /> Agent 啟動中...
          </Text>
        </div>
      );
    }
    return null;
  }
  const items = steps
    .filter((s) => s.event === "thought" || s.event === "tool_call" || s.event === "observation")
    .map((s) => {
      if (s.event === "thought") {
        return {
          color: "blue",
          dot: <BulbOutlined style={{ fontSize: 14 }} />,
          children: (
            <div>
              <Text strong style={{ fontSize: 12, color: "#1677ff" }}>思考 #{s.step}</Text>
              <div style={{ fontSize: 13, color: "#555" }}>{s.text}</div>
            </div>
          ),
        };
      }
      if (s.event === "tool_call") {
        return {
          color: "purple",
          dot: <ToolOutlined style={{ fontSize: 14 }} />,
          children: (
            <div>
              <Text strong style={{ fontSize: 12, color: "#722ed1" }}>呼叫 {s.tool}</Text>
              <pre style={{ fontSize: 11, background: "#f0f0f0", padding: 6, borderRadius: 4, overflow: "auto", maxHeight: 100, marginTop: 4 }}>
                {JSON.stringify(s.input || {}, null, 2)}
              </pre>
            </div>
          ),
        };
      }
      const outStr = JSON.stringify(s.output || {}, null, 2);
      const preview = outStr.length > 400 ? outStr.slice(0, 400) + "..." : outStr;
      return {
        color: "green",
        dot: <EyeOutlined style={{ fontSize: 14 }} />,
        children: (
          <div>
            <Text strong style={{ fontSize: 12, color: "#52c41a" }}>觀察 ({s.tool})</Text>
            <pre style={{ fontSize: 11, background: "#f6ffed", padding: 6, borderRadius: 4, overflow: "auto", maxHeight: 200, marginTop: 4 }}>
              {preview}
            </pre>
          </div>
        ),
      };
    });
  return (
    <Collapse
      size="small"
      ghost
      defaultActiveKey={isLive ? ["agent"] : []}
      style={{ marginBottom: 8 }}
      items={[
        {
          key: "agent",
          label: (
            <Space>
              <RobotOutlined style={{ color: "#1677ff" }} />
              <Text strong style={{ fontSize: 13 }}>Agent 推理過程</Text>
              <Tag color="blue" style={{ fontSize: 11 }}>{items.length} 步</Tag>
              {isLive && !steps.some((s) => s.event === "final") && (
                <Tag color="processing" style={{ fontSize: 11 }}>進行中</Tag>
              )}
            </Space>
          ),
          children: <Timeline items={items} style={{ marginTop: 8 }} />,
        },
      ]}
    />
  );
};

export const renderSources = (sources, msgIndex, expandedSnippets, onToggle, onPreview) => {
  if (!sources || sources.length === 0) return null;
  return (
    <div style={{ marginTop: 16 }}>
      <Divider orientation="left" style={{ fontSize: 13, marginTop: 16, marginBottom: 12 }}>
        參考來源({sources.length})
      </Divider>
      <List
        size="small"
        dataSource={sources}
        renderItem={(source, idx) => {
          const key = `${msgIndex}-${idx}`;
          const isExpanded = !!expandedSnippets[key];
          const full = stripFenceWrapper(source.snippet || "");
          const needsTruncate = full.length > 200;
          const displaySnippet = needsTruncate && !isExpanded ? full.slice(0, 200) : full;
          return (
            <List.Item key={key}>
              <List.Item.Meta
                title={
                  <Space size={8} wrap>
                    <Tag color="green">來源 {idx + 1}</Tag>
                    <Text>{source.title || "(未命名文件)"}{typeof source.page === "number" ? ` - 第 ${source.page} 頁` : ""}</Text>
                    {source.score != null && (<Text type="secondary">(相似度 {source.score.toFixed(3)})</Text>)}
                    <Button size="small" onClick={() => onPreview(source)}>預覽</Button>
                  </Space>
                }
                description={
                  <div>
                    {(!needsTruncate || isExpanded) ? (
                      // 完整顯示時：以 Markdown 渲染（表格、清單等），比較美觀
                      <div className="markdown-content" style={{ fontSize: 13, color: "#595959" }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                          {full}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      // 收合預覽時：純文字截斷（避免顯示切一半的表格），快速掃視
                      <Text style={{ whiteSpace: "pre-wrap" }}>
                        {displaySnippet}...
                      </Text>
                    )}
                    {needsTruncate && (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: "0 0 0 4px", height: "auto", fontSize: 12 }}
                        onClick={() => onToggle(key)}
                      >
                        {isExpanded ? "收起" : "展開全文"}
                      </Button>
                    )}
                  </div>
                }
              />
            </List.Item>
          );
        }}
      />
    </div>
  );
};
